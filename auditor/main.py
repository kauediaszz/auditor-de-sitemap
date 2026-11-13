"""Application flow for sitemap inventory, HTTP checks, and Excel export."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from dataclasses import dataclass

from auditor.config import get_psi_api_key
from auditor.dashboard import build_dashboard, serve_dashboard
from auditor.history import load_history, record_urls
from auditor.checks.core_web_vitals import check_core_web_vitals
from auditor.checks.crux import check_core_web_vitals_crux
from auditor.checks.http_status import check_http_status
from auditor.checks.schema import check_schema
from auditor.checks.schema_rules import load_rules
from auditor.checks.seo import SEOConfig, check_seo
from auditor.models import (
    CoreWebVitalsResult,
    HTTPStatusResult,
    SchemaResult,
    SEOResult,
)
from auditor.checks.inventory import collect_inventory_alerts
from auditor.reports.csv_report import write_audit_csv, write_schema_detail_csv
from auditor.reports.cwv_report import write_cwv_failures_report
from auditor.reports.excel_report import write_sitemap_inventory_report
from auditor.reports.markdown_report import write_summary_markdown
from auditor.sitemap import SitemapEntry, SitemapInventory, read_sitemaps


@dataclass(frozen=True)
class AuditOutputs:
    """Caminhos dos relatórios gerados por uma execução.

    Fase 1 (rápida): ``main_report`` (Excel), ``audit_csv`` (auditoria.csv),
    ``schema_detail_csv`` (schema_detalhe.csv) e ``summary_md`` (resumo.md),
    todos gerados antes de consultar o PageSpeed. Fase 2 (lenta): o CWV é
    coletado, ``audit_csv`` e ``summary_md`` são reescritos já com as métricas e
    ``cwv_failures_report`` (Excel separado) é gravado só com as URLs reprovadas.
    ``cwv_failures_report`` fica ``None`` quando a coleta de CWV não ocorreu.
    """

    main_report: Path
    audit_csv: Path
    schema_detail_csv: Path
    summary_md: Path
    cwv_failures_report: Path | None
    dashboard: Path | None = None
    run_directory: Path | None = None
    # Quantidade pedida x obtida por tipo (None no pedido = sem limite).
    requested: dict[str, int | None] | None = None
    obtained: dict[str, int] | None = None


class NoNewURLsError(RuntimeError):
    """Não há URLs novas (ainda não auditadas) para a rodada pedida."""


DEFAULT_MAX_URLS_PER_RUN = 500
_SAMPLE_KEYS = ("articles", "galleries", "videos")


def resolve_limits(
    config: dict[str, Any],
    *,
    articles: int | None = None,
    galleries: int | None = None,
    videos: int | None = None,
    audit_all: bool = False,
) -> dict[str, int | None]:
    """Resolve a quantidade por tipo. Precedência: --all > argumento > config.

    ``None`` no resultado significa "sem limite" (só acontece com ``audit_all``).
    Valida inteiros não negativos e o teto ``limits.max_urls_per_run`` (que não
    se aplica a ``audit_all``, pedido explicitamente pelo usuário).
    """
    if audit_all:
        return {key: None for key in _SAMPLE_KEYS}

    sampling = config["sampling"]
    given = {"articles": articles, "galleries": galleries, "videos": videos}
    resolved: dict[str, int | None] = {}
    for key in _SAMPLE_KEYS:
        value = given[key] if given[key] is not None else sampling[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"A quantidade de '{key}' deve ser um inteiro")
        if value < 0:
            raise ValueError(f"A quantidade de '{key}' não pode ser negativa")
        resolved[key] = value

    cap = (config.get("limits") or {}).get(
        "max_urls_per_run", DEFAULT_MAX_URLS_PER_RUN
    )
    total = sum(v for v in resolved.values() if v is not None)
    if total > cap:
        raise ValueError(
            f"Total de {total} URLs excede o teto de {cap} por execução "
            "(limits.max_urls_per_run)"
        )
    return resolved


def _selected_entries(inventory: SitemapInventory) -> tuple[SitemapEntry, ...]:
    selected: dict[str, SitemapEntry] = {}
    for entries in (
        inventory.articles,
        inventory.galleries,
        inventory.videos,
        inventory.main_routes,
    ):
        for entry in entries:
            selected.setdefault(entry.url, entry)
    return tuple(selected.values())


def _category_of(url: str) -> str:
    """Primeiro segmento do path da URL (a 'categoria' da matéria).

    Ex.: ``/news/some-story.html`` -> ``news``;
    ``/world-cup/x.html`` -> ``world-cup``.
    """
    from urllib.parse import urlsplit

    path = urlsplit(url).path.strip("/")
    return path.split("/")[0] if path else ""


def _cwv_sample_by_category(
    inventory: SitemapInventory,
) -> tuple[SitemapEntry, ...]:
    """Amostra de CWV: todas as rotas de homes + 1 matéria por categoria.

    Foca em URLs com chance real de ter dados de campo no CrUX (páginas de
    seção e uma matéria representativa por editoria), em vez das ~80 matérias
    individuais que em geral não têm dados. Usado apenas no Core Web Vitals; os
    demais checks continuam sobre a amostra completa.
    """
    sample: dict[str, SitemapEntry] = {}
    # 1) Todas as rotas de homes (categorias, tabelas e rotas funcionais).
    for entry in inventory.main_routes:
        sample.setdefault(entry.url, entry)
    # 2) A matéria mais recente de cada categoria (primeiro segmento da URL).
    #    inventory.articles já vem ordenado da mais recente para a mais antiga.
    seen_categories: set[str] = set()
    for entry in inventory.articles:
        category = _category_of(entry.url)
        if category and category not in seen_categories:
            seen_categories.add(category)
            sample.setdefault(entry.url, entry)
    return tuple(sample.values())


def _load_config(config_path: str | Path) -> dict[str, Any]:
    with Path(config_path).open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    if not isinstance(config, dict):
        raise ValueError("Configuration file must contain a YAML mapping")

    required_sections = ("sources", "sampling", "http", "output")
    for section in required_sections:
        if not isinstance(config.get(section), dict):
            raise ValueError(f"Configuration section '{section}' is required")
    if not config["sources"].get("sitemap_index"):
        raise ValueError("Configuration must define sources.sitemap_index")
    return config


def _source_paths(config: dict[str, Any]) -> dict[str, tuple[str, ...]] | None:
    """Lê ``sources.sitemaps`` (papel -> sufixos de caminho). ``None`` = padrão."""
    raw = config["sources"].get("sitemaps")
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise ValueError("sources.sitemaps must be a non-empty mapping")
    result: dict[str, tuple[str, ...]] = {}
    for role, suffixes in raw.items():
        if isinstance(suffixes, str):
            suffixes = [suffixes]
        if not isinstance(suffixes, list) or not all(
            isinstance(item, str) and item for item in suffixes
        ):
            raise ValueError(
                f"sources.sitemaps.{role} must be a path suffix or a list of them"
            )
        result[str(role)] = tuple(suffixes)
    return result


def _page_type_rules(config: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Lê ``page_types``: lista de ``{prefix, type}`` aplicada em ordem."""
    raw = config.get("page_types") or []
    if not isinstance(raw, list):
        raise ValueError("page_types must be a list of {prefix, type}")
    rules: list[tuple[str, str]] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("prefix"), str)
            or not isinstance(item.get("type"), str)
        ):
            raise ValueError("Each page_types item needs string 'prefix' and 'type'")
        rules.append((item["prefix"], item["type"]))
    return tuple(rules)


async def run_audit(
    config_path: str | Path = "config/config.yaml",
    *,
    output_path: str | Path | None = None,
    strategy_override: str | None = None,
    audit_all: bool = False,
    articles_limit: int | None = None,
    galleries_limit: int | None = None,
    videos_limit: int | None = None,
    only_new: bool = False,
    sitemap_transport: httpx.AsyncBaseTransport | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    seo_transport: httpx.AsyncBaseTransport | None = None,
    schema_transport: httpx.AsyncBaseTransport | None = None,
    cwv_transport: httpx.AsyncBaseTransport | None = None,
    api_key: str | None = None,
) -> AuditOutputs:
    """Executa a auditoria em duas fases.

    Fase 1 (rápida): inventário, status HTTP, SEO e schema em paralelo; o Excel
    principal é gravado em seguida, sem esperar o PageSpeed. Fase 2 (lenta):
    coleta de Core Web Vitals via PSI e, por fim, um Excel separado só com as
    URLs reprovadas (``cwv_ruim``).
    """
    config = _load_config(config_path)
    http_config = config["http"]
    seo_config = SEOConfig.from_mapping(config.get("seo"))
    schema_config = config.get("schema") or {}
    rules_directory = schema_config.get("rules_directory", "config/schema_rules")
    schema_rules = load_rules(rules_directory)
    pagespeed_config = config.get("pagespeed") or {}
    # Com audit_all, ignora os limites de amostragem (None = todas as URLs do
    # tipo). As rotas principais já entram todas, independentemente disso.
    limits = resolve_limits(
        config,
        articles=articles_limit,
        galleries=galleries_limit,
        videos=videos_limit,
        audit_all=audit_all,
    )
    if audit_all:
        print(
            "Modo --all: auditando TODAS as URLs de matérias, galerias e "
            "vídeos do sitemap (pode demorar bastante)."
        )
    output_root = Path(config["output"]["directory"])
    exclude_urls: frozenset[str] = frozenset()
    if only_new:
        exclude_urls = load_history(output_root)
        print(f"Modo URLs novas: ignorando {len(exclude_urls)} URL(s) já auditadas.")
    inventory = await read_sitemaps(
        config["sources"]["sitemap_index"],
        articles_limit=limits["articles"],
        galleries_limit=limits["galleries"],
        videos_limit=limits["videos"],
        exclude_urls=exclude_urls,
        source_paths=_source_paths(config),
        page_type_rules=_page_type_rules(config),
        concurrency=http_config["concurrency"],
        timeout_seconds=http_config["timeout_seconds"],
        transport=sitemap_transport,
    )
    obtained = {
        "articles": len(inventory.articles),
        "galleries": len(inventory.galleries),
        "videos": len(inventory.videos),
    }
    for key in _SAMPLE_KEYS:
        wanted = limits[key]
        if wanted is not None and obtained[key] < wanted:
            print(
                f"Aviso: pedidas {wanted} URL(s) de '{key}', "
                f"mas só há {obtained[key]} disponível(is)."
            )
    if only_new and sum(obtained.values()) == 0 and any(
        (limits[key] is None or limits[key] > 0) for key in _SAMPLE_KEYS
    ):
        raise NoNewURLsError("Não há URLs novas para auditar.")
    selected = _selected_entries(inventory)
    http_results: tuple[HTTPStatusResult, ...]
    seo_results: tuple[SEOResult, ...]
    schema_results: tuple[SchemaResult, ...]
    http_results, seo_results, schema_results = await asyncio.gather(
        check_http_status(
            selected,
            concurrency=http_config["concurrency"],
            # Status HTTP tem seu próprio limite curto (padrão 4s) por
            # tentativa, com retry; não usa o timeout de 30s do SEO/schema,
            # que precisam baixar a página inteira.
            timeout_seconds=http_config.get("status_timeout_seconds", 4),
            max_attempts=http_config.get("status_retries", 3),
            transport=http_transport,
        ),
        check_seo(
            selected,
            concurrency=http_config["concurrency"],
            timeout_seconds=http_config["timeout_seconds"],
            config=seo_config,
            transport=seo_transport,
        ),
        check_schema(
            selected,
            rules=schema_rules,
            concurrency=http_config["concurrency"],
            timeout_seconds=http_config["timeout_seconds"],
            transport=schema_transport,
        ),
    )

    # --- Fase 1: Excel principal, gerado antes de consultar o PageSpeed. ---
    if output_path is not None:
        main_destination = Path(output_path)
        run_directory = main_destination.parent
    else:
        run_directory = (
            Path(config["output"]["directory"])
            / datetime.now().strftime("%Y-%m-%d_%H%M%S")
        )
        main_destination = run_directory / "auditoria.xlsx"

    inventory_config = config.get("inventory") or {}
    inventory_alerts = collect_inventory_alerts(
        inventory,
        require_www=bool(inventory_config.get("require_www", False)),
        legacy_slugs=tuple(inventory_config.get("legacy_slugs") or ()),
    )

    main_report = write_sitemap_inventory_report(
        inventory,
        main_destination,
        http_results=http_results,
        seo_results=seo_results,
        schema_results=schema_results,
    )
    schema_detail_csv = write_schema_detail_csv(
        run_directory / "schema_detalhe.csv",
        schema_results=schema_results,
    )
    audit_csv = write_audit_csv(
        inventory,
        run_directory / "auditoria.csv",
        http_results=http_results,
        seo_results=seo_results,
        schema_results=schema_results,
    )
    summary_md = write_summary_markdown(
        inventory,
        run_directory / "resumo.md",
        http_results=http_results,
        seo_results=seo_results,
        schema_results=schema_results,
        inventory_alerts=inventory_alerts,
    )
    print(f"Relatório principal gerado: {main_report}")
    print(f"CSV e resumo gerados em: {run_directory}")

    # --- Fase 2: Core Web Vitals (lento) + Excel só com reprovados. ---
    cwv_failures_report: Path | None = None
    cwv_enabled = pagespeed_config.get("enabled", True)
    if cwv_enabled:
        strategy = strategy_override or pagespeed_config.get("strategy", "mobile")
        method = (pagespeed_config.get("method") or "crux").lower()
        # O CWV roda somente nas homes (rotas de categoria) + 1 matéria por
        # categoria — URLs com chance real de ter dados de campo no CrUX.
        # Matérias individuais fora dessa amostra não são consultadas e também
        # não aparecem no relatório de CWV (em vez de virarem linhas vazias).
        cwv_entries = _cwv_sample_by_category(inventory)
        resolved_key = api_key if api_key is not None else get_psi_api_key()
        if resolved_key:
            print(
                f"CWV: {len(cwv_entries)} URL(s) (homes + 1 matéria por "
                f"categoria) de {len(selected)} auditadas."
            )
            if method == "psi":
                print(
                    "Coletando Core Web Vitals via PageSpeed Insights "
                    "(isso pode demorar)..."
                )
                cwv_results = await check_core_web_vitals(
                    cwv_entries,
                    api_key=resolved_key,
                    strategy=strategy,
                    concurrency=pagespeed_config.get("concurrency", 4),
                    timeout_seconds=pagespeed_config.get("timeout_seconds", 90),
                    transport=cwv_transport,
                )
            else:
                print("Coletando Core Web Vitals via Chrome UX Report (CrUX)...")
                cwv_results = await check_core_web_vitals_crux(
                    cwv_entries,
                    api_key=resolved_key,
                    strategy=strategy,
                    concurrency=pagespeed_config.get("concurrency", 4),
                    timeout_seconds=pagespeed_config.get("timeout_seconds", 30),
                    max_qps=pagespeed_config.get("max_qps", 3.0),
                    transport=cwv_transport,
                )
            cwv_failures_report = write_cwv_failures_report(
                cwv_results,
                run_directory / "cwv_reprovados.xlsx",
            )
            # Reescreve auditoria.csv e resumo.md já com as métricas de CWV.
            audit_csv = write_audit_csv(
                inventory,
                run_directory / "auditoria.csv",
                http_results=http_results,
                seo_results=seo_results,
                schema_results=schema_results,
                cwv_results=cwv_results,
            )
            summary_md = write_summary_markdown(
                inventory,
                run_directory / "resumo.md",
                http_results=http_results,
                seo_results=seo_results,
                schema_results=schema_results,
                cwv_results=cwv_results,
                inventory_alerts=inventory_alerts,
            )
            poor = sum(1 for result in cwv_results if result.is_poor)
            print(
                f"Core Web Vitals coletado para {len(cwv_results)} URL(s); "
                f"{poor} reprovada(s). Relatório: {cwv_failures_report}"
            )
        else:
            print(
                "Aviso: PSI_API_KEY não configurada; "
                "Core Web Vitals não foi coletado."
            )

    # --- Histórico: registra matérias/galerias/vídeos auditados. ---
    # Só depois dos relatórios gravados, para uma rodada que falha antes não
    # consumir URLs. Execuções com --output explícito não mexem no histórico.
    if output_path is None:
        sampled_urls = [
            entry.url
            for entry in (
                *inventory.articles,
                *inventory.galleries,
                *inventory.videos,
            )
        ]
        try:
            record_urls(output_root, sampled_urls)
        except OSError as error:
            logging.getLogger(__name__).warning(
                "Falha ao gravar o histórico de URLs: %s", error
            )

    # --- Dashboard: lê os relatórios da execução e monta o painel estático. ---
    dashboard_index: Path | None = None
    try:
        dashboard_index = build_dashboard(run_directory)
        print(f"Dashboard gerado: {dashboard_index}")
    except Exception as error:  # não derruba a auditoria por falha no painel
        logging.getLogger(__name__).warning(
            "Falha ao gerar o dashboard: %s", error
        )

    return AuditOutputs(
        main_report=main_report,
        audit_csv=audit_csv,
        schema_detail_csv=schema_detail_csv,
        summary_md=summary_md,
        cwv_failures_report=cwv_failures_report,
        dashboard=dashboard_index,
        run_directory=run_directory,
        requested=dict(limits),
        obtained=obtained,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sitemap Auditor")
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to the YAML configuration file",
    )
    parser.add_argument(
        "--output",
        help="Optional explicit path for the generated Excel workbook",
    )
    parser.add_argument(
        "--all",
        dest="audit_all",
        action="store_true",
        help=(
            "Auditar TODAS as URLs do sitemap (ignora os limites de amostragem "
            "de matérias/galerias/vídeos). Pode demorar bastante."
        ),
    )
    for flag, label in (
        ("--articles", "matérias"),
        ("--galleries", "galerias"),
        ("--videos", "vídeos"),
    ):
        parser.add_argument(
            flag,
            type=int,
            metavar="N",
            help=(
                f"Quantidade de {label} mais recentes a auditar "
                "(sobrescreve sampling no config; 0 pula o tipo)"
            ),
        )
    parser.add_argument(
        "--only-new",
        action="store_true",
        help="Ignora URLs já auditadas em execuções anteriores",
    )
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="Coletar Core Web Vitals com estratégia desktop (padrão: mobile)",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Ao final, sobe um servidor local e abre o dashboard no navegador",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Porta inicial do servidor do dashboard (padrão: 8000)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Com --serve, não abre o navegador automaticamente",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Logs detalhados (nível DEBUG), inclui cada consulta ao PSI",
    )
    arguments = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # O httpx loga a URL completa da requisição, que inclui a chave da API do
    # PSI (?key=...). Silenciar para não vazar a credencial no terminal/logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        outputs = asyncio.run(
            run_audit(
                arguments.config,
                output_path=arguments.output,
                strategy_override="desktop" if arguments.desktop else None,
                audit_all=arguments.audit_all,
                articles_limit=arguments.articles,
                galleries_limit=arguments.galleries,
                videos_limit=arguments.videos,
                only_new=arguments.only_new,
            )
        )
    except (ValueError, NoNewURLsError) as error:
        parser.error(str(error))
    print(f"Relatório principal (Excel): {outputs.main_report}")
    print(f"auditoria.csv: {outputs.audit_csv}")
    print(f"schema_detalhe.csv: {outputs.schema_detail_csv}")
    print(f"resumo.md: {outputs.summary_md}")
    if outputs.cwv_failures_report is not None:
        print(f"CWV reprovados (Excel): {outputs.cwv_failures_report}")
    if outputs.dashboard is not None:
        print(f"Dashboard (HTML): {outputs.dashboard}")
        if arguments.serve:
            serve_dashboard(
                outputs.dashboard.parent,
                port=arguments.port,
                open_browser=not arguments.no_browser,
                config_path=arguments.config,
            )
        else:
            print(
                "Para ver o dashboard no navegador, rode: "
                "python -m auditor.dashboard"
            )


if __name__ == "__main__":
    main()
