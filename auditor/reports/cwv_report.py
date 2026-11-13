"""Relatório Excel separado com as URLs reprovadas no Core Web Vitals.

Reprovada = qualquer métrica (LCP, INP ou CLS) na faixa ruim do Google
(``cwv_ruim``). URLs sem dado ou com erro de coleta não entram como reprovadas,
mas o motivo fica registrado quando há erro.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from auditor.models import CoreWebVitalsResult

_HEADERS = (
    "URL",
    "Tipo de página",
    "Sitemap(s) de origem",
    "Estratégia",
    "LCP (ms)",
    "INP (ms)",
    "CLS",
    "TBT (ms)",
    "Fonte do dado",
    "Faixa LCP",
    "Faixa INP",
    "Faixa CLS",
    "Pior faixa",
)


def write_cwv_failures_report(
    cwv_results: Iterable[CoreWebVitalsResult],
    output_path: str | Path,
) -> Path:
    """Escreve só as URLs reprovadas no CWV (``cwv_ruim``) em uma planilha.

    Mantém a relação de cada URL com seus sitemaps de origem. O arquivo é
    sempre gravado, mesmo quando não há reprovadas (apenas o cabeçalho), para
    deixar explícito que a verificação ocorreu.
    """
    failures = [result for result in cwv_results if result.is_poor]

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "CWV reprovados"
    worksheet.append(_HEADERS)

    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="9C0006")
        cell.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )

    for result in failures:
        worksheet.append(
            [
                result.url,
                result.page_type,
                ", ".join(result.origins),
                result.strategy,
                result.lcp_ms,
                result.inp_ms,
                result.cls,
                result.tbt_ms,
                result.source,
                result.lcp_band,
                result.inp_band,
                result.cls_band,
                result.worst_band,
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
