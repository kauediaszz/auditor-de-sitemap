from datetime import datetime, timezone

from openpyxl import load_workbook

from auditor.models import HTTPStatusResult
from auditor.reports.excel_report import write_sitemap_inventory_report
from auditor.sitemap import SitemapEntry, SitemapInventory


def test_excel_report_includes_selected_urls_and_marks_http_unverified(tmp_path):
    duplicate = SitemapEntry(
        url="https://example.test/story",
        lastmod=datetime(2026, 10, 27, tzinfo=timezone.utc),
        origins=("https://example.test/sitemap/articles.xml", "https://example.test/sitemap/homes.xml"),
        occurrences=2,
        page_type="article",
    )
    route = SitemapEntry(
        url="https://example.test/tables/league",
        lastmod=None,
        origins=("https://example.test/sitemap/homes.xml",),
        occurrences=1,
        page_type="table",
    )
    inventory = SitemapInventory(
        entries=(duplicate, route),
        articles=(duplicate,),
        galleries=(),
        videos=(),
        main_routes=(duplicate, route),
        duplicates=(duplicate,),
        accessed_sitemaps=("https://example.test/sitemap/index.xml",),
        url_counts_by_sitemap=(),
    )
    report_path = tmp_path / "auditoria.xlsx"

    written_path = write_sitemap_inventory_report(inventory, report_path)

    assert written_path == report_path
    worksheet = load_workbook(report_path, read_only=False).active
    assert worksheet.title == "Por URL"
    assert worksheet.freeze_panes == "A2"
    assert worksheet.auto_filter.ref == worksheet.dimensions
    assert worksheet.max_row == 3

    headers = [cell.value for cell in worksheet[1]]
    rows = {
        row[0]: dict(zip(headers, row))
        for row in worksheet.iter_rows(min_row=2, values_only=True)
    }
    story = rows["https://example.test/story"]
    assert story["Seleção do leitor"] == "Matérias, Rotas principais"
    assert story["Sitemap(s) de origem"] == (
        "https://example.test/sitemap/articles.xml, "
        "https://example.test/sitemap/homes.xml"
    )
    assert story["lastmod"] == "2026-10-27T00:00:00+00:00"
    assert story["Ocorrências"] == 2
    assert story["Duplicada?"] == "Sim"
    assert story["Tipo de página"] == "article"

    for column in (
        "Status HTTP",
        "URL final",
        "Tempo de resposta (ms)",
        "Mensagem de erro",
        "Verificação HTTP",
    ):
        assert story[column] == "Não verificado"

    assert rows["https://example.test/tables/league"]["lastmod"] is None


def test_excel_report_exports_http_results_and_leaves_missing_status_empty(tmp_path):
    urls = (
        "https://example.test/success",
        "https://example.test/not-found",
        "https://example.test/server-error",
        "https://example.test/redirected",
        "https://example.test/connection-failure",
        "https://example.test/timed-out",
        "https://example.test/not-checked",
    )
    entries = tuple(
        SitemapEntry(
            url=url,
            lastmod=None,
            origins=("https://example.test/sitemap/articles.xml",),
            occurrences=1,
            page_type="article",
        )
        for url in urls
    )
    results = (
        HTTPStatusResult(
            original_url=urls[0],
            status_code=200,
            final_url=urls[0],
            elapsed_ms=10.0,
            redirect_count=0,
            redirect_statuses=(),
            outcome="success",
            error_message=None,
            origins=entries[0].origins,
            page_type=entries[0].page_type,
        ),
        HTTPStatusResult(
            original_url=urls[1],
            status_code=404,
            final_url=urls[1],
            elapsed_ms=11.0,
            redirect_count=0,
            redirect_statuses=(),
            outcome="http_error",
            error_message="HTTP status 404",
            origins=entries[1].origins,
            page_type=entries[1].page_type,
        ),
        HTTPStatusResult(
            original_url=urls[2],
            status_code=500,
            final_url=urls[2],
            elapsed_ms=12.0,
            redirect_count=0,
            redirect_statuses=(),
            outcome="http_error",
            error_message="HTTP status 500",
            origins=entries[2].origins,
            page_type=entries[2].page_type,
        ),
        HTTPStatusResult(
            original_url=urls[3],
            status_code=200,
            final_url="https://example.test/final",
            elapsed_ms=13.0,
            redirect_count=2,
            redirect_statuses=(302, 301),
            outcome="redirect",
            error_message=None,
            origins=entries[3].origins,
            page_type=entries[3].page_type,
        ),
        HTTPStatusResult(
            original_url=urls[4],
            status_code=None,
            final_url=None,
            elapsed_ms=14.0,
            redirect_count=None,
            redirect_statuses=(),
            outcome="connection_error",
            error_message="Connection failure (ConnectError)",
            origins=entries[4].origins,
            page_type=entries[4].page_type,
        ),
        HTTPStatusResult(
            original_url=urls[5],
            status_code=None,
            final_url=None,
            elapsed_ms=15.0,
            redirect_count=None,
            redirect_statuses=(),
            outcome="timeout",
            error_message="Timeout (ReadTimeout)",
            origins=entries[5].origins,
            page_type=entries[5].page_type,
        ),
    )
    inventory = SitemapInventory(
        entries=entries,
        articles=entries,
        galleries=(),
        videos=(),
        main_routes=(),
        duplicates=(),
        accessed_sitemaps=(),
        url_counts_by_sitemap=(),
    )
    report_path = tmp_path / "http-results.xlsx"

    write_sitemap_inventory_report(
        inventory,
        report_path,
        http_results=results,
    )

    worksheet = load_workbook(report_path, data_only=True).active
    headers = [cell.value for cell in worksheet[1]]
    rows = {
        row[0]: dict(zip(headers, row))
        for row in worksheet.iter_rows(min_row=2, values_only=True)
    }
    assert rows[urls[0]]["Status HTTP"] == 200
    assert rows[urls[1]]["Status HTTP"] == 404
    assert rows[urls[2]]["Status HTTP"] == 500
    redirected = rows[urls[3]]
    assert redirected["URL original"] == urls[3]
    assert redirected["Status HTTP"] == 200
    assert redirected["URL final"] == "https://example.test/final"
    assert redirected["Quantidade de redirecionamentos"] == 2
    assert redirected["Histórico de redirecionamentos"] == "302, 301"
    assert rows[urls[4]]["Status HTTP"] is None
    assert rows[urls[4]]["Verificação HTTP"] == "connection_error"
    assert rows[urls[5]]["Status HTTP"] is None
    assert rows[urls[5]]["Verificação HTTP"] == "timeout"
    assert rows[urls[6]]["Status HTTP"] == "Não verificado"
    assert rows[urls[6]]["Verificação HTTP"] == "Não verificado"
    assert rows[urls[1]]["Sitemap(s) de origem"] == entries[1].origins[0]
    assert rows[urls[1]]["Tipo de página"] == "article"
