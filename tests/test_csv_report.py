import csv

from auditor.models import (
    CoreWebVitalsResult,
    HTTPStatusResult,
    SchemaFinding,
    SchemaResult,
    SEOFinding,
    SEOResult,
)
from auditor.reports.csv_report import (
    _AUDIT_COLUMNS,
    _SCHEMA_DETAIL_COLUMNS,
    write_audit_csv,
    write_schema_detail_csv,
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
    home = SitemapEntry(
        url="https://example.test/",
        lastmod=None,
        origins=("https://example.test/sitemap/homes.xml",),
        occurrences=1,
        page_type="home",
    )
    return SitemapInventory(
        entries=(article, home),
        articles=(article,),
        galleries=(),
        videos=(),
        main_routes=(home,),
        duplicates=(),
        accessed_sitemaps=(),
        url_counts_by_sitemap=(),
    )


def test_audit_csv_has_spec_columns_and_values(tmp_path):
    inv = _inventory()
    http = (
        HTTPStatusResult(
            "https://example.test/story", 404, "https://example.test/story", 8.0,
            0, (), "http_error", "HTTP status 404",
            ("https://example.test/sitemap/articles.xml",), "article",
        ),
    )
    seo = (
        SEOResult(
            url="https://example.test/story", final_url=None, page_type="article",
            origins=(), title=None, title_length=None, meta_description=None,
            meta_description_length=None, canonical=None, has_noindex=False,
            h1_count=0, open_graph_tags=(),
            findings=(SEOFinding("title_present", "error", "title ausente"),),
        ),
    )
    schema = (
        SchemaResult(
            url="https://example.test/story", page_type="article", origins=(),
            detected_types=("NewsArticle",),
            findings=(
                SchemaFinding("NewsArticle", "required_field_missing", "headline",
                              "presente", "ausente"),
            ),
        ),
    )
    cwv = (
        CoreWebVitalsResult(
            url="https://example.test/story", page_type="article", origins=(),
            strategy="mobile", lcp_ms=5000.0, inp_ms=600.0, cls=0.4, tbt_ms=900.0,
            source="field", lcp_band="ruim", inp_band="ruim", cls_band="ruim",
        ),
    )
    path = write_audit_csv(
        inv, tmp_path / "auditoria.csv",
        http_results=http, seo_results=seo, schema_results=schema, cwv_results=cwv,
    )
    with path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(_AUDIT_COLUMNS)
        rows = {r["url"]: r for r in reader}

    story = rows["https://example.test/story"]
    assert story["status_final"] == "404"
    assert story["fora_do_ar"] == "true"
    assert story["seo_ok"] == "false"
    assert story["schema_ok"] == "false"
    assert "schema_observacoes" in story
    assert story["lcp_ms"] == "5000"
    assert story["cls"] == "0.400"
    assert story["cwv_ruim"] == "true"
    assert story["cwv_faixa"] == "ruim"
    # Home sem resultados -> colunas em branco, sem inventar valores.
    home = rows["https://example.test/"]
    assert home["status_final"] == ""
    assert home["seo_ok"] == ""


def test_schema_detail_csv_one_row_per_finding(tmp_path):
    schema = (
        SchemaResult(
            url="https://example.test/story", page_type="article", origins=(),
            detected_types=("NewsArticle",),
            findings=(
                SchemaFinding("NewsArticle", "required_field_missing", "headline",
                              "presente", "ausente"),
                SchemaFinding("NewsArticle", "publisher_organization_same_id",
                              "publisher.@id", "x", "y"),
            ),
        ),
    )
    path = write_schema_detail_csv(
        tmp_path / "schema_detalhe.csv", schema_results=schema
    )
    with path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(_SCHEMA_DETAIL_COLUMNS)
        rows = list(reader)
    assert len(rows) == 2
    assert rows[0]["caminho_do_campo"] == "headline"
    assert rows[1]["regra"] == "publisher_organization_same_id"
