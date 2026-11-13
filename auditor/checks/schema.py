"""Verificação de dados estruturados (JSON-LD) das páginas selecionadas.

Lê o HTML de cada URL, extrai os blocos ``application/ld+json`` com a biblioteca
``extruct``, achata o ``@graph`` e valida cada tipo contra as regras externas em
``config/schema_rules/`` (ver :mod:`auditor.checks.schema_rules`).

Páginas sem regra para o seu tipo de página são deixadas sem achados (apenas os
tipos detectados são registrados). Falhas de rede, HTTP de erro ou JSON-LD
inválido viram um ``SchemaResult`` com ``error_message``, sem derrubar as demais
URLs.
"""

from __future__ import annotations

import asyncio
from typing import Iterable
from urllib.parse import urlsplit

import extruct
import httpx

from auditor.checks.schema_rules import (
    RuleSet,
    evaluate_page,
    flatten_graph,
)
from auditor.models import SchemaResult
from auditor.sitemap import SitemapEntry

_USER_AGENT = "SitemapAuditor/0.1 (JSON-LD schema check)"


def _detected_types(nodes: list[dict]) -> tuple[str, ...]:
    seen: list[str] = []
    for node in nodes:
        raw = node.get("@type")
        values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
        for value in values:
            if isinstance(value, str) and value not in seen:
                seen.append(value)
    return tuple(seen)


def _error_result(entry: SitemapEntry, message: str) -> SchemaResult:
    return SchemaResult(
        url=entry.url,
        page_type=entry.page_type,
        origins=entry.origins,
        detected_types=(),
        findings=(),
        error_message=message,
    )


def analyze_schema(
    content: bytes | str,
    *,
    url: str,
    page_type: str,
    origins: tuple[str, ...],
    rules: RuleSet,
) -> SchemaResult:
    """Extrai o JSON-LD do HTML e avalia contra as regras do tipo de página."""
    data = extruct.extract(content, syntaxes=["json-ld"])
    blocks = data.get("json-ld", [])
    nodes = flatten_graph(blocks)
    detected = _detected_types(nodes)

    page_rules = rules.for_page_type(page_type)
    findings = (
        tuple(
            evaluate_page(
                nodes,
                page_rules,
                page_url=url,
                raw_blocks=blocks,
            )
        )
        if page_rules
        else ()
    )

    return SchemaResult(
        url=url,
        page_type=page_type,
        origins=origins,
        detected_types=detected,
        findings=findings,
    )


async def check_schema(
    entries: Iterable[SitemapEntry],
    *,
    rules: RuleSet,
    concurrency: int = 10,
    timeout_seconds: float = 30,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[SchemaResult, ...]:
    """Baixa o HTML de cada entrada e valida seus dados estruturados."""
    if concurrency < 1:
        raise ValueError("Concurrency must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("HTTP timeout must be greater than zero")

    selected_entries = tuple(entries)
    semaphore = asyncio.Semaphore(concurrency)
    timeout = httpx.Timeout(timeout_seconds)
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )

    async with httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT},
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
        transport=transport,
    ) as client:

        async def check_one(entry: SitemapEntry) -> SchemaResult:
            async with semaphore:
                try:
                    parsed_url = urlsplit(entry.url)
                    valid_url = (
                        parsed_url.scheme in {"http", "https"}
                        and parsed_url.hostname is not None
                    )
                except ValueError:
                    valid_url = False
                if not valid_url:
                    return _error_result(entry, "Invalid request URL")

                try:
                    response = await client.get(entry.url)
                except httpx.TimeoutException as exc:
                    return _error_result(entry, f"Timeout ({type(exc).__name__})")
                except httpx.RequestError as exc:
                    return _error_result(
                        entry, f"Request failure ({type(exc).__name__})"
                    )

                if response.status_code >= 400:
                    return _error_result(
                        entry, f"HTTP status {response.status_code}"
                    )

                try:
                    return analyze_schema(
                        response.content,
                        url=entry.url,
                        page_type=entry.page_type,
                        origins=entry.origins,
                        rules=rules,
                    )
                except Exception as exc:  # noqa: BLE001 - extração defensiva
                    return _error_result(
                        entry, f"JSON-LD parse failure ({type(exc).__name__})"
                    )

        return tuple(
            await asyncio.gather(
                *(check_one(entry) for entry in selected_entries)
            )
        )
