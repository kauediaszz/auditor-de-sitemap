from openpyxl import load_workbook

from auditor.models import CoreWebVitalsResult
from auditor.reports.cwv_report import write_cwv_failures_report


def _result(url, *, poor, origins=("https://example.test/sitemap/articles.xml",)):
    if poor:
        return CoreWebVitalsResult(
            url=url, page_type="article", origins=origins, strategy="mobile",
            lcp_ms=5000.0, inp_ms=600.0, cls=0.4, tbt_ms=900.0, source="field",
            lcp_band="ruim", inp_band="ruim", cls_band="ruim",
        )
    return CoreWebVitalsResult(
        url=url, page_type="home", origins=origins, strategy="mobile",
        lcp_ms=1200.0, inp_ms=150.0, cls=0.05, tbt_ms=100.0, source="field",
        lcp_band="bom", inp_band="bom", cls_band="bom",
    )


def test_report_includes_only_poor_urls(tmp_path):
    results = [
        _result("https://example.test/ok", poor=False),
        _result("https://example.test/bad", poor=True),
    ]
    path = write_cwv_failures_report(results, tmp_path / "cwv.xlsx")

    worksheet = load_workbook(path, data_only=True).active
    assert worksheet.title == "CWV reprovados"
    headers = [c.value for c in worksheet[1]]
    rows = {
        row[0]: dict(zip(headers, row))
        for row in worksheet.iter_rows(min_row=2, values_only=True)
    }
    assert set(rows) == {"https://example.test/bad"}
    assert rows["https://example.test/bad"]["Pior faixa"] == "ruim"


def test_report_with_no_failures_writes_header_only(tmp_path):
    results = [_result("https://example.test/ok", poor=False)]
    path = write_cwv_failures_report(results, tmp_path / "cwv.xlsx")

    worksheet = load_workbook(path, data_only=True).active
    assert worksheet.max_row == 1  # só o cabeçalho


def test_report_preserves_origins(tmp_path):
    results = [
        _result(
            "https://example.test/bad",
            poor=True,
            origins=("https://example.test/a.xml", "https://example.test/b.xml"),
        )
    ]
    path = write_cwv_failures_report(results, tmp_path / "cwv.xlsx")
    worksheet = load_workbook(path, data_only=True).active
    headers = [c.value for c in worksheet[1]]
    row = dict(zip(headers, next(worksheet.iter_rows(min_row=2, values_only=True))))
    assert row["Sitemap(s) de origem"] == (
        "https://example.test/a.xml, https://example.test/b.xml"
    )
