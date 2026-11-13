import asyncio

from openpyxl import load_workbook

from auditor import main as main_module
from auditor.models import (
    CoreWebVitalsResult,
    HTTPStatusResult,
    SchemaFinding,
    SchemaResult,
    SEOFinding,
    SEOResult,
)
from auditor.sitemap import SitemapEntry, SitemapInventory


def _inventory():
    article = SitemapEntry(
        url="https://example.test/story",
        lastmod=None,
        origins=("https://example.test/sitemap/articles.xml",),
        occurrences=1,
        page_type="article",
    )
    route = SitemapEntry(
        url="https://example.test/",
        lastmod=None,
        origins=("https://example.test/sitemap/homes.xml",),
        occurrences=1,
        page_type="home",
    )
    return SitemapInventory(
        entries=(article, route),
        articles=(article,),
        galleries=(),
        videos=(),
        main_routes=(article, route),
        duplicates=(),
        accessed_sitemaps=(),
        url_counts_by_sitemap=(),
    )


def test_main_sends_unique_selected_entries_to_checker_and_exports_results(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
sources:
  sitemap_index: https://example.test/sitemap/index.xml
sampling:
  articles: 80
  galleries: 20
  videos: 20
http:
  concurrency: 4
  timeout_seconds: 12
output:
  directory: saida
""",
        encoding="utf-8",
    )
    inventory = _inventory()
    captured = {}

    async def fake_read_sitemaps(sitemap_index_url, **kwargs):
        captured["sitemap_index_url"] = sitemap_index_url
        captured["reader_kwargs"] = kwargs
        return inventory

    async def fake_check_http_status(entries, **kwargs):
        captured["entries"] = tuple(entries)
        captured["checker_kwargs"] = kwargs
        return (
            HTTPStatusResult(
                original_url="https://example.test/",
                status_code=200,
                final_url="https://example.test/",
                elapsed_ms=12.5,
                redirect_count=0,
                redirect_statuses=(),
                outcome="success",
                error_message=None,
                origins=("https://example.test/sitemap/homes.xml",),
                page_type="home",
            ),
            HTTPStatusResult(
                original_url="https://example.test/story",
                status_code=404,
                final_url="https://example.test/story",
                elapsed_ms=8.0,
                redirect_count=0,
                redirect_statuses=(),
                outcome="http_error",
                error_message="HTTP status 404",
                origins=("https://example.test/sitemap/articles.xml",),
                page_type="article",
            ),
        )

    async def fake_check_seo(entries, **kwargs):
        captured["seo_entries"] = tuple(entries)
        captured["seo_kwargs"] = kwargs
        return (
            SEOResult(
                url="https://example.test/",
                final_url="https://example.test/",
                page_type="home",
                origins=("https://example.test/sitemap/homes.xml",),
                title="Home",
                title_length=4,
                meta_description="Descrição",
                meta_description_length=9,
                canonical="https://example.test/",
                has_noindex=False,
                h1_count=1,
                open_graph_tags=("og:title",),
                findings=(),
            ),
            SEOResult(
                url="https://example.test/story",
                final_url="https://example.test/story",
                page_type="article",
                origins=("https://example.test/sitemap/articles.xml",),
                title=None,
                title_length=None,
                meta_description=None,
                meta_description_length=None,
                canonical=None,
                has_noindex=False,
                h1_count=0,
                open_graph_tags=(),
                findings=(
                    SEOFinding("title_present", "error", "title ausente ou vazio"),
                    SEOFinding("h1_single", "error", "nenhum H1 na página"),
                ),
            ),
        )

    async def fake_check_schema(entries, **kwargs):
        captured["schema_entries"] = tuple(entries)
        captured["schema_kwargs"] = kwargs
        return (
            SchemaResult(
                url="https://example.test/",
                page_type="home",
                origins=("https://example.test/sitemap/homes.xml",),
                detected_types=("WebPage", "WebSite", "Organization"),
                findings=(),
            ),
            SchemaResult(
                url="https://example.test/story",
                page_type="article",
                origins=("https://example.test/sitemap/articles.xml",),
                detected_types=("NewsArticle",),
                findings=(
                    SchemaFinding(
                        schema_type="NewsArticle",
                        rule="required_field_missing",
                        field_path="headline",
                        expected="presente (str)",
                        found="ausente",
                    ),
                ),
            ),
        )

    async def fake_check_core_web_vitals(entries, **kwargs):
        captured["cwv_entries"] = tuple(entries)
        captured["cwv_kwargs"] = kwargs
        return (
            CoreWebVitalsResult(
                url="https://example.test/",
                page_type="home",
                origins=("https://example.test/sitemap/homes.xml",),
                strategy="mobile",
                lcp_ms=1200.0,
                inp_ms=150.0,
                cls=0.05,
                tbt_ms=100.0,
                source="field",
                lcp_band="bom",
                inp_band="bom",
                cls_band="bom",
            ),
            CoreWebVitalsResult(
                url="https://example.test/story",
                page_type="article",
                origins=("https://example.test/sitemap/articles.xml",),
                strategy="mobile",
                lcp_ms=5000.0,
                inp_ms=600.0,
                cls=0.4,
                tbt_ms=900.0,
                source="field",
                lcp_band="ruim",
                inp_band="ruim",
                cls_band="ruim",
            ),
        )

    monkeypatch.setattr(main_module, "read_sitemaps", fake_read_sitemaps)
    monkeypatch.setattr(main_module, "check_http_status", fake_check_http_status)
    monkeypatch.setattr(main_module, "check_seo", fake_check_seo)
    monkeypatch.setattr(main_module, "check_schema", fake_check_schema)
    monkeypatch.setattr(
        main_module, "check_core_web_vitals", fake_check_core_web_vitals
    )
    # Método padrão é CrUX; o config de teste não define pagespeed.method.
    monkeypatch.setattr(
        main_module, "check_core_web_vitals_crux", fake_check_core_web_vitals
    )
    output_path = tmp_path / "report.xlsx"

    outputs = asyncio.run(
        main_module.run_audit(
            config_path, output_path=output_path, api_key="FAKE_KEY"
        )
    )

    assert outputs.main_report == output_path
    # O relatório de CWV reprovados vai para um arquivo separado na mesma pasta.
    assert outputs.cwv_failures_report == tmp_path / "cwv_reprovados.xlsx"
    assert captured["sitemap_index_url"] == "https://example.test/sitemap/index.xml"
    assert [entry.url for entry in captured["entries"]] == [
        "https://example.test/story",
        "https://example.test/",
    ]
    assert captured["reader_kwargs"]["articles_limit"] == 80
    assert captured["reader_kwargs"]["galleries_limit"] == 20
    assert captured["reader_kwargs"]["videos_limit"] == 20
    assert captured["checker_kwargs"]["concurrency"] == 4
    # O status HTTP usa o timeout curto dedicado (padrão 4s) e retry, não o
    # timeout geral de 12s (que continua valendo para SEO/schema).
    assert captured["checker_kwargs"]["timeout_seconds"] == 4
    assert captured["checker_kwargs"]["max_attempts"] == 3
    assert [entry.url for entry in captured["seo_entries"]] == [
        "https://example.test/story",
        "https://example.test/",
    ]
    assert captured["seo_kwargs"]["concurrency"] == 4
    assert captured["seo_kwargs"]["timeout_seconds"] == 12
    assert [entry.url for entry in captured["schema_entries"]] == [
        "https://example.test/story",
        "https://example.test/",
    ]
    assert captured["schema_kwargs"]["concurrency"] == 4
    assert captured["schema_kwargs"]["timeout_seconds"] == 12

    worksheet = load_workbook(output_path, data_only=True).active
    headers = [cell.value for cell in worksheet[1]]
    rows = {
        row[0]: dict(zip(headers, row))
        for row in worksheet.iter_rows(min_row=2, values_only=True)
    }
    story = rows["https://example.test/story"]
    assert story["Status HTTP"] == 404
    assert story["Verificação HTTP"] == "http_error"
    assert story["Sitemap(s) de origem"] == "https://example.test/sitemap/articles.xml"
    assert story["Tipo de página"] == "article"
    assert story["Mensagem de erro"] == "HTTP status 404"
    assert story["SEO OK?"] == "Não"
    assert "title ausente ou vazio" in story["Problemas de SEO"]
    assert "nenhum H1 na página" in story["Problemas de SEO"]

    assert story["Schema OK?"] == "Não"
    assert "headline" in story["Problemas de schema"]
    assert "NewsArticle" in story["Tipos de schema detectados"]
    # O Excel principal NÃO tem colunas de CWV (coleta é posterior).
    assert "LCP (ms)" not in headers
    assert "CWV ruim?" not in headers

    home = rows["https://example.test/"]
    assert home["SEO OK?"] == "Sim"
    assert home["Schema OK?"] == "Sim"
    assert "WebSite" in home["Tipos de schema detectados"]

    assert [e.url for e in captured["cwv_entries"]] == [
        "https://example.test/story",
        "https://example.test/",
    ]
    assert captured["cwv_kwargs"]["strategy"] == "mobile"

    # O segundo Excel contém só a URL reprovada (story), não a home.
    cwv_ws = load_workbook(outputs.cwv_failures_report, data_only=True).active
    cwv_headers = [cell.value for cell in cwv_ws[1]]
    cwv_rows = {
        row[0]: dict(zip(cwv_headers, row))
        for row in cwv_ws.iter_rows(min_row=2, values_only=True)
    }
    assert set(cwv_rows) == {"https://example.test/story"}
    reproved = cwv_rows["https://example.test/story"]
    assert reproved["Pior faixa"] == "ruim"
    assert reproved["LCP (ms)"] == 5000.0
    assert reproved["Sitemap(s) de origem"] == "https://example.test/sitemap/articles.xml"

    # --- CSV principal (reescrito na fase 2, já com CWV) ---
    import csv as _csv

    assert outputs.audit_csv == tmp_path / "auditoria.csv"
    assert outputs.schema_detail_csv == tmp_path / "schema_detalhe.csv"
    assert outputs.summary_md == tmp_path / "resumo.md"

    with outputs.audit_csv.open(encoding="utf-8") as handle:
        audit_rows = {r["url"]: r for r in _csv.DictReader(handle)}
    story_csv = audit_rows["https://example.test/story"]
    assert story_csv["tipo_pagina"] == "article"
    assert story_csv["fora_do_ar"] == "true"
    assert story_csv["seo_ok"] == "false"
    assert story_csv["schema_ok"] == "false"
    assert story_csv["cwv_ruim"] == "true"
    assert story_csv["cwv_faixa"] == "ruim"
    assert story_csv["lcp_ms"] == "5000"
    home_csv = audit_rows["https://example.test/"]
    assert home_csv["fora_do_ar"] == "false"
    assert home_csv["cwv_ruim"] == "false"

    # --- schema_detalhe.csv (uma linha por achado) ---
    with outputs.schema_detail_csv.open(encoding="utf-8") as handle:
        detail_rows = list(_csv.DictReader(handle))
    assert any(
        r["url"] == "https://example.test/story" and r["caminho_do_campo"] == "headline"
        for r in detail_rows
    )

    # --- resumo.md ---
    summary_text = outputs.summary_md.read_text(encoding="utf-8")
    assert "# Resumo da auditoria" in summary_text
    assert "Core Web Vitals" in summary_text


# --- Quantidade de URLs por tipo e modo "só URLs novas" ---------------------

import json as _json

import pytest

_BASE_CONFIG = {
    "sampling": {"articles": 80, "galleries": 20, "videos": 20},
}


def test_resolve_limits_precedence_and_defaults():
    # Sem argumentos: usa o config.
    assert main_module.resolve_limits(_BASE_CONFIG) == {
        "articles": 80,
        "galleries": 20,
        "videos": 20,
    }
    # Argumento sobrescreve o config; 0 é aceito (pula o tipo).
    assert main_module.resolve_limits(
        _BASE_CONFIG, articles=150, galleries=0
    ) == {"articles": 150, "galleries": 0, "videos": 20}
    # --all vence tudo e ignora o teto.
    assert main_module.resolve_limits(
        _BASE_CONFIG, articles=5, audit_all=True
    ) == {"articles": None, "galleries": None, "videos": None}


def test_resolve_limits_rejects_negative_non_int_and_over_cap():
    with pytest.raises(ValueError, match="negativa"):
        main_module.resolve_limits(_BASE_CONFIG, videos=-1)
    with pytest.raises(ValueError, match="inteiro"):
        main_module.resolve_limits(_BASE_CONFIG, articles=True)
    with pytest.raises(ValueError, match="teto"):
        main_module.resolve_limits(_BASE_CONFIG, articles=500)  # 500+20+20
    capped = {**_BASE_CONFIG, "limits": {"max_urls_per_run": 1000}}
    assert main_module.resolve_limits(capped, articles=900)["articles"] == 900


def _write_min_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
sources:
  sitemap_index: https://example.test/sitemap/index.xml
sampling:
  articles: 80
  galleries: 20
  videos: 20
http:
  concurrency: 2
  timeout_seconds: 5
output:
  directory: {(tmp_path / "saida").as_posix()}
pagespeed:
  enabled: false
""",
        encoding="utf-8",
    )
    return config_path


def _patch_empty_checks(monkeypatch, inventory, captured):
    async def fake_read_sitemaps(sitemap_index_url, **kwargs):
        captured["reader_kwargs"] = kwargs
        return inventory

    async def empty_check(entries, **kwargs):
        return ()

    monkeypatch.setattr(main_module, "read_sitemaps", fake_read_sitemaps)
    monkeypatch.setattr(main_module, "check_http_status", empty_check)
    monkeypatch.setattr(main_module, "check_seo", empty_check)
    monkeypatch.setattr(main_module, "check_schema", empty_check)


def test_run_audit_passes_custom_limits_and_records_history(tmp_path, monkeypatch):
    config_path = _write_min_config(tmp_path)
    captured = {}
    _patch_empty_checks(monkeypatch, _inventory(), captured)

    outputs = asyncio.run(
        main_module.run_audit(
            config_path, articles_limit=7, galleries_limit=0, videos_limit=3
        )
    )

    kwargs = captured["reader_kwargs"]
    assert (kwargs["articles_limit"], kwargs["galleries_limit"]) == (7, 0)
    assert kwargs["videos_limit"] == 3
    assert kwargs["exclude_urls"] == frozenset()
    assert outputs.requested == {"articles": 7, "galleries": 0, "videos": 3}
    assert outputs.obtained == {"articles": 1, "galleries": 0, "videos": 0}
    # Só a matéria entra no histórico; a rota de homes não.
    history = _json.loads(
        (tmp_path / "saida" / "urls_auditadas.json").read_text(encoding="utf-8")
    )
    assert set(history["urls"]) == {"https://example.test/story"}


def test_run_audit_only_new_excludes_history_and_raises_when_empty(
    tmp_path, monkeypatch
):
    config_path = _write_min_config(tmp_path)
    saida = tmp_path / "saida"
    saida.mkdir()
    (saida / "urls_auditadas.json").write_text(
        _json.dumps({"versao": 1, "urls": {"https://example.test/old": "x"}}),
        encoding="utf-8",
    )
    captured = {}
    _patch_empty_checks(monkeypatch, _inventory(), captured)

    asyncio.run(main_module.run_audit(config_path, only_new=True))
    assert captured["reader_kwargs"]["exclude_urls"] == frozenset(
        {"https://example.test/old"}
    )

    # Inventário sem nenhuma URL amostrada => nada novo.
    route = _inventory().main_routes[1]
    empty = SitemapInventory(
        entries=(route,),
        articles=(),
        galleries=(),
        videos=(),
        main_routes=(route,),
        duplicates=(),
        accessed_sitemaps=(),
        url_counts_by_sitemap=(),
    )
    _patch_empty_checks(monkeypatch, empty, captured)
    with pytest.raises(main_module.NoNewURLsError):
        asyncio.run(main_module.run_audit(config_path, only_new=True))


# --- Leitura de sources.sitemaps / page_types do config ---------------------


def test_source_paths_default_and_normalization():
    base = {"sources": {"sitemap_index": "https://example.test/i.xml"}}
    assert main_module._source_paths(base) is None  # usa o padrão do leitor

    config = {
        "sources": {
            "sitemap_index": "https://example.test/i.xml",
            "sitemaps": {"articles": "/a.xml", "homes": ["/h.xml", "/h2.xml"]},
        }
    }
    assert main_module._source_paths(config) == {
        "articles": ("/a.xml",),
        "homes": ("/h.xml", "/h2.xml"),
    }


@pytest.mark.parametrize("bad", [{}, [], {"articles": 3}, {"articles": [""]}])
def test_source_paths_rejects_invalid_values(bad):
    config = {"sources": {"sitemap_index": "x", "sitemaps": bad}}
    with pytest.raises(ValueError):
        main_module._source_paths(config)


def test_page_type_rules_parsing():
    assert main_module._page_type_rules({}) == ()
    config = {"page_types": [{"prefix": "/a/", "type": "x"}, {"prefix": "/b", "type": "y"}]}
    assert main_module._page_type_rules(config) == (("/a/", "x"), ("/b", "y"))
    with pytest.raises(ValueError):
        main_module._page_type_rules({"page_types": [{"prefix": "/a/"}]})
    with pytest.raises(ValueError):
        main_module._page_type_rules({"page_types": "nope"})


def test_shipped_example_config_is_valid():
    config = main_module._load_config("config/config.yaml")
    assert config["sources"]["sitemap_index"].startswith("https://www.example.com/")
    assert set(main_module._source_paths(config)) == {
        "articles",
        "galleries",
        "videos",
        "homes",
    }
    assert ("/videos/", "video") in main_module._page_type_rules(config)
