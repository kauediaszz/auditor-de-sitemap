"""Geração dos relatórios CSV da auditoria .

Dois arquivos, com colunas fixas:

- ``auditoria.csv``: uma linha por URL.
- ``schema_detalhe.csv``: uma linha por problema de schema.

Os valores ausentes ficam em branco; não se inventa código HTTP ou métrica.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from auditor.models import (
    CoreWebVitalsResult,
    HTTPStatusResult,
    SchemaResult,
    SEOResult,
)
from auditor.sitemap import SitemapEntry, SitemapInventory

# Colunas de auditoria.csv .
_AUDIT_COLUMNS = (
    "url",
    "tipo_pagina",
    "sitemap_origem",
    "lastmod",
    "status_final",
    "redirects",
    "tempo_ms",
    "fora_do_ar",
    "seo_ok",
    "seo_problemas",
    "schema_ok",
    "schema_problemas",
    "schema_observacoes",
    "lcp_ms",
    "inp_ms",
    "cls",
    "tbt_ms",
    "cwv_fonte",
    "cwv_faixa",
    "cwv_ruim",
)

# Colunas de schema_detalhe.csv .
_SCHEMA_DETAIL_COLUMNS = (
    "url",
    "tipo_pagina",
    "tipo_schema",
    "regra",
    "caminho_do_campo",
    "esperado",
    "encontrado",
)


def _bool(value: bool | None) -> str:
    if value is None:
        return ""
    return "true" if value else "false"


def _join(values: Iterable[str]) -> str:
    return " | ".join(v for v in values if v)


def _selected_entries(inventory: SitemapInventory) -> list[SitemapEntry]:
    selected: dict[str, SitemapEntry] = {}
    for entries in (
        inventory.articles,
        inventory.galleries,
        inventory.videos,
        inventory.main_routes,
    ):
        for entry in entries:
            selected.setdefault(entry.url, entry)
    return list(selected.values())


def write_audit_csv(
    inventory: SitemapInventory,
    output_path: str | Path,
    *,
    http_results: Iterable[HTTPStatusResult] | None = None,
    seo_results: Iterable[SEOResult] | None = None,
    schema_results: Iterable[SchemaResult] | None = None,
    cwv_results: Iterable[CoreWebVitalsResult] | None = None,
) -> Path:
    """Escreve ``auditoria.csv`` (uma linha por URL)."""
    http_by_url = {r.original_url: r for r in (http_results or ())}
    seo_by_url = {r.url: r for r in (seo_results or ())}
    schema_by_url = {r.url: r for r in (schema_results or ())}
    cwv_by_url = {r.url: r for r in (cwv_results or ())}

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_AUDIT_COLUMNS)
        writer.writeheader()
        for entry in _selected_entries(inventory):
            http = http_by_url.get(entry.url)
            seo = seo_by_url.get(entry.url)
            schema = schema_by_url.get(entry.url)
            cwv = cwv_by_url.get(entry.url)

            writer.writerow(
                {
                    "url": entry.url,
                    "tipo_pagina": entry.page_type,
                    "sitemap_origem": _join(entry.origins),
                    "lastmod": entry.lastmod.isoformat()
                    if entry.lastmod is not None
                    else "",
                    "status_final": http.status_code
                    if http and http.status_code is not None
                    else "",
                    "redirects": ", ".join(map(str, http.redirect_statuses))
                    if http
                    else "",
                    "tempo_ms": f"{http.elapsed_ms:.0f}"
                    if http and http.elapsed_ms is not None
                    else "",
                    "fora_do_ar": _bool(http.is_down) if http else "",
                    "seo_ok": _bool(seo.seo_ok) if seo else "",
                    "seo_problemas": _join(seo.problems) if seo else "",
                    "schema_ok": _bool(schema.schema_ok) if schema else "",
                    "schema_problemas": _join(schema.problems) if schema else "",
                    "schema_observacoes": _join(schema.observations)
                    if schema
                    else "",
                    "lcp_ms": f"{cwv.lcp_ms:.0f}"
                    if cwv and cwv.lcp_ms is not None
                    else "",
                    "inp_ms": f"{cwv.inp_ms:.0f}"
                    if cwv and cwv.inp_ms is not None
                    else "",
                    "cls": f"{cwv.cls:.3f}"
                    if cwv and cwv.cls is not None
                    else "",
                    "tbt_ms": f"{cwv.tbt_ms:.0f}"
                    if cwv and cwv.tbt_ms is not None
                    else "",
                    "cwv_fonte": cwv.source if cwv and cwv.source else "",
                    "cwv_faixa": cwv.worst_band if cwv else "",
                    "cwv_ruim": _bool(cwv.is_poor) if cwv else "",
                }
            )
    return destination


def write_schema_detail_csv(
    output_path: str | Path,
    *,
    schema_results: Iterable[SchemaResult] | None = None,
) -> Path:
    """Escreve ``schema_detalhe.csv`` (uma linha por problema de schema)."""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_SCHEMA_DETAIL_COLUMNS)
        writer.writeheader()
        for result in schema_results or ():
            for finding in result.findings:
                writer.writerow(
                    {
                        "url": result.url,
                        "tipo_pagina": result.page_type,
                        "tipo_schema": finding.schema_type,
                        "regra": finding.rule,
                        "caminho_do_campo": finding.field_path,
                        "esperado": finding.expected,
                        "encontrado": finding.found,
                    }
                )
    return destination
