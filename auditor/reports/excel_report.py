"""Export the selected sitemap inventory to an Excel workbook."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from auditor.models import HTTPStatusResult, SchemaResult, SEOResult
from auditor.sitemap import SitemapEntry, SitemapInventory

_HEADERS = (
    "URL original",
    "Seleção do leitor",
    "Sitemap(s) de origem",
    "Tipo de página",
    "lastmod",
    "Ocorrências",
    "Duplicada?",
    "Status HTTP",
    "Verificação HTTP",
    "URL final",
    "Tempo de resposta (ms)",
    "Quantidade de redirecionamentos",
    "Histórico de redirecionamentos",
    "Mensagem de erro",
    "SEO OK?",
    "Problemas de SEO",
    "Mensagem de erro SEO",
    "Schema OK?",
    "Tipos de schema detectados",
    "Problemas de schema",
    "Mensagem de erro Schema",
)
_HTTP_COLUMN_COUNT = 7
_SEO_COLUMN_COUNT = 3
_SCHEMA_COLUMN_COUNT = 4
_NOT_VERIFIED = "Não verificado"


def write_sitemap_inventory_report(
    inventory: SitemapInventory,
    output_path: str | Path,
    *,
    http_results: Iterable[HTTPStatusResult] | None = None,
    seo_results: Iterable[SEOResult] | None = None,
    schema_results: Iterable[SchemaResult] | None = None,
) -> Path:
    """Write selected URLs, sitemap metadata, HTTP and SEO results.

    HTTP and SEO results are associated by their original URL. Missing results
    are explicitly marked as not verified, and failures without an HTTP response
    leave the status cell empty rather than fabricating a code.
    """
    selections = (
        ("Matérias", inventory.articles),
        ("Galerias", inventory.galleries),
        ("Vídeos", inventory.videos),
        ("Rotas principais", inventory.main_routes),
    )
    entries: dict[str, SitemapEntry] = {}
    selection_labels: dict[str, list[str]] = {}
    for label, selected_entries in selections:
        for entry in selected_entries:
            entries.setdefault(entry.url, entry)
            selection_labels.setdefault(entry.url, []).append(label)
    results_by_url = {
        result.original_url: result for result in (http_results or ())
    }
    seo_by_url = {result.url: result for result in (seo_results or ())}
    schema_by_url = {result.url: result for result in (schema_results or ())}

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Por URL"
    worksheet.append(_HEADERS)

    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
        )

    for url, entry in entries.items():
        result = results_by_url.get(url)
        if result is None:
            http_values = [_NOT_VERIFIED] * _HTTP_COLUMN_COUNT
        else:
            http_values = [
                result.status_code,
                result.outcome,
                result.final_url,
                result.elapsed_ms,
                result.redirect_count,
                ", ".join(map(str, result.redirect_statuses)),
                result.error_message,
            ]

        seo = seo_by_url.get(url)
        if seo is None:
            seo_values = [_NOT_VERIFIED] * _SEO_COLUMN_COUNT
        else:
            seo_values = [
                "Sim" if seo.seo_ok else "Não",
                "\n".join(seo.problems),
                seo.error_message,
            ]

        schema = schema_by_url.get(url)
        if schema is None:
            schema_values = [_NOT_VERIFIED] * _SCHEMA_COLUMN_COUNT
        else:
            schema_values = [
                "Sim" if schema.schema_ok else "Não",
                ", ".join(schema.detected_types),
                "\n".join(schema.problems),
                schema.error_message,
            ]

        worksheet.append(
            [
                url,
                ", ".join(selection_labels[url]),
                ", ".join(entry.origins),
                entry.page_type,
                entry.lastmod.isoformat() if entry.lastmod is not None else "",
                entry.occurrences,
                "Sim" if entry.is_duplicate else "Não",
                *http_values,
                *seo_values,
                *schema_values,
            ]
        )

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    worksheet.row_dimensions[1].height = 32

    for column_index, header in enumerate(_HEADERS, start=1):
        values = [
            str(worksheet.cell(row=row, column=column_index).value or "")
            for row in range(1, worksheet.max_row + 1)
        ]
        width = min(
            max(max(map(len, values), default=len(header)) + 2, len(header) + 2),
            60,
        )
        worksheet.column_dimensions[get_column_letter(column_index)].width = width

    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(destination)
    return destination
