from auditor.checks.inventory import InventoryAlert
from auditor.models import (
    CoreWebVitalsResult,
    HTTPStatusResult,
    SchemaResult,
    SEOResult,
)
from auditor.reports.markdown_report import write_summary_markdown
from auditor.sitemap import SitemapEntry, SitemapInventory


def _inventory():
    home = SitemapEntry(
        url="https://example.test/", lastmod=None,
        origins=("https://example.test/sitemap/homes.xml",), occurrences=1,
        page_type="home",
    )
    return SitemapInventory(
        entries=(home,), articles=(), galleries=(), videos=(),
        main_routes=(home,), duplicates=(), accessed_sitemaps=("x",),
        url_counts_by_sitemap=(),
    )


def test_summary_has_sections_and_counts(tmp_path):
    http = (
        HTTPStatusResult(
            "https://example.test/down", 500, "https://example.test/down", 5.0,
            0, (), "http_error", "HTTP status 500", (), "article",
        ),
    )
    cwv = (
        CoreWebVitalsResult(
            url="https://example.test/down", page_type="article", origins=(),
            strategy="mobile", lcp_ms=5000.0, inp_ms=600.0, cls=0.4, tbt_ms=900.0,
            source="field", lcp_band="ruim", inp_band="ruim", cls_band="ruim",
        ),
    )
    alerts = (
        InventoryAlert("duplicate_url", "https://example.test/dup", (), "2 ocorrências"),
    )
    path = write_summary_markdown(
        _inventory(), tmp_path / "resumo.md",
        http_results=http, cwv_results=cwv, inventory_alerts=alerts,
    )
    text = path.read_text(encoding="utf-8")
    assert "# Resumo da auditoria" in text
    assert "## Status HTTP" in text
    assert "Fora do ar" in text
    assert "## Core Web Vitals" in text
    assert "Reprovadas (cwv_ruim): 1" in text
    assert "## Alertas de inventário" in text
    assert "URL repetida no sitemap: 1" in text
    assert "https://example.test/down" in text


def test_summary_without_alerts_says_none(tmp_path):
    path = write_summary_markdown(_inventory(), tmp_path / "resumo.md")
    text = path.read_text(encoding="utf-8")
    assert "Nenhum alerta de inventário." in text
