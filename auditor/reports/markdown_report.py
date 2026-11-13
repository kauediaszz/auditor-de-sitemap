"""Geração do resumo em Markdown (``resumo.md``) da auditoria.

Reúne contagens, piores casos e a lista de achados, além dos
alertas de inventário. Cada URL mantém a relação com seus sitemaps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from auditor.checks.inventory import InventoryAlert
from auditor.models import (
    CoreWebVitalsResult,
    HTTPStatusResult,
    SchemaResult,
    SEOResult,
)
from auditor.sitemap import SitemapInventory

_ALERT_LABELS = {
    "duplicate_url": "URL repetida no sitemap",
    "url_with_fragment": "URL com fragmento (#)",
    "host_without_www": "Host sem www",
    "legacy_slug": "Slug legado",
}


def _count(items: Iterable[bool]) -> int:
    return sum(1 for item in items if item)


def write_summary_markdown(
    inventory: SitemapInventory,
    output_path: str | Path,
    *,
    http_results: Iterable[HTTPStatusResult] | None = None,
    seo_results: Iterable[SEOResult] | None = None,
    schema_results: Iterable[SchemaResult] | None = None,
    cwv_results: Iterable[CoreWebVitalsResult] | None = None,
    inventory_alerts: Iterable[InventoryAlert] | None = None,
) -> Path:
    """Escreve o resumo da auditoria em Markdown."""
    http = tuple(http_results or ())
    seo = tuple(seo_results or ())
    schema = tuple(schema_results or ())
    cwv = tuple(cwv_results or ())
    alerts = tuple(inventory_alerts or ())

    lines: list[str] = []
    lines.append("# Resumo da auditoria do sitemap")
    lines.append("")

    # --- Inventário ---
    lines.append("## Inventário")
    lines.append("")
    lines.append(f"- URLs únicas no inventário: {len(inventory.entries)}")
    lines.append(f"- Matérias selecionadas: {len(inventory.articles)}")
    lines.append(f"- Galerias selecionadas: {len(inventory.galleries)}")
    lines.append(f"- Vídeos selecionados: {len(inventory.videos)}")
    lines.append(f"- Rotas principais: {len(inventory.main_routes)}")
    lines.append(f"- Sitemaps acessados: {len(inventory.accessed_sitemaps)}")
    lines.append("")

    # --- Status HTTP ---
    down = [r for r in http if r.is_down]
    redirects = [r for r in http if r.is_sitemap_redirect]
    lines.append("## Status HTTP")
    lines.append("")
    lines.append(f"- URLs verificadas: {len(http)}")
    lines.append(f"- Fora do ar (4xx/5xx, erro de rede ou timeout): {len(down)}")
    lines.append(f"- Redirecionamentos em URL do sitemap (alerta): {len(redirects)}")
    lines.append("")
    if down:
        lines.append("### Fora do ar")
        lines.append("")
        for r in down:
            detail = (
                f"HTTP {r.status_code}" if r.status_code else r.error_message or r.outcome
            )
            lines.append(f"- `{r.original_url}` — {detail}")
        lines.append("")

    # --- SEO ---
    seo_fail = [r for r in seo if not r.seo_ok]
    lines.append("## SEO")
    lines.append("")
    lines.append(f"- URLs verificadas: {len(seo)}")
    lines.append(f"- Com erro de SEO: {len(seo_fail)}")
    lines.append("")
    if seo_fail:
        lines.append("### URLs com erro de SEO")
        lines.append("")
        for r in seo_fail:
            problems = "; ".join(r.problems) or r.error_message or "erro"
            lines.append(f"- `{r.url}` — {problems}")
        lines.append("")

    # --- Schema ---
    # URL "com erro" = tem achado de severidade error (ou falha de coleta).
    # Observações (warning) não reprovam a URL, mas são listadas à parte.
    schema_fail = [r for r in schema if not r.schema_ok]
    schema_obs = [r for r in schema if r.warnings]
    total_errors = sum(len(r.errors) for r in schema)
    total_obs = sum(len(r.warnings) for r in schema)
    lines.append("## Schema (JSON-LD)")
    lines.append("")
    lines.append(f"- URLs verificadas: {len(schema)}")
    lines.append(f"- Com erro de schema (reprovadas): {len(schema_fail)}")
    lines.append(f"- Total de erros de schema: {total_errors}")
    lines.append(f"- Total de observações (não reprovam): {total_obs}")
    lines.append("")
    if schema_fail:
        lines.append("### URLs com erro de schema")
        lines.append("")
        for r in schema_fail:
            problems = "; ".join(r.problems) or r.error_message or "erro"
            lines.append(f"- `{r.url}` — {problems}")
        lines.append("")
    if schema_obs:
        lines.append("### Observações de schema (não reprovam a URL)")
        lines.append("")
        for r in schema_obs:
            obs = "; ".join(r.observations)
            lines.append(f"- `{r.url}` — obs: {obs}")
        lines.append("")

    # --- Core Web Vitals ---
    poor = [r for r in cwv if r.is_poor]
    lines.append("## Core Web Vitals")
    lines.append("")
    lines.append(f"- URLs verificadas: {len(cwv)}")
    lines.append(f"- Reprovadas (cwv_ruim): {len(poor)}")
    lines.append("")
    if poor:
        lines.append("### URLs reprovadas no CWV")
        lines.append("")
        for r in poor:
            lines.append(
                f"- `{r.url}` — pior faixa: {r.worst_band} "
                f"(LCP={r.lcp_ms}, INP={r.inp_ms}, CLS={r.cls}; fonte: {r.source})"
            )
        lines.append("")

    # --- Alertas de inventário ---
    lines.append("## Alertas de inventário")
    lines.append("")
    if not alerts:
        lines.append("- Nenhum alerta de inventário.")
        lines.append("")
    else:
        by_kind: dict[str, list[InventoryAlert]] = {}
        for alert in alerts:
            by_kind.setdefault(alert.kind, []).append(alert)
        for kind, label in _ALERT_LABELS.items():
            group = by_kind.get(kind, [])
            lines.append(f"- {label}: {len(group)}")
        lines.append("")
        for kind, label in _ALERT_LABELS.items():
            group = by_kind.get(kind, [])
            if not group:
                continue
            lines.append(f"### {label}")
            lines.append("")
            for alert in group:
                lines.append(f"- `{alert.url}` — {alert.detail}")
            lines.append("")

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination
