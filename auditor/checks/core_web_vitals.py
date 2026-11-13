"""Coleta de Core Web Vitals via API do PageSpeed Insights.

Para cada URL, consulta a API do PageSpeed Insights e coleta LCP, INP e CLS
(dados de campo reais de usuários, via CrUX, quando existirem) e TBT (sempre de
laboratório, via Lighthouse). A chave da API vem da variável de ambiente
``PSI_API_KEY`` e nunca é registrada em relatórios ou logs.

Estratégia ``mobile`` por padrão; ``desktop`` quando solicitado.

Faixas do Google:

- LCP: bom <= 2500 ms, precisa melhorar <= 4000 ms, ruim > 4000 ms
- INP: bom <= 200 ms, precisa melhorar <= 500 ms, ruim > 500 ms
- CLS: bom <= 0.1, precisa melhorar <= 0.25, ruim > 0.25

Uma URL sem dado de campo usa o laboratório (Lighthouse) e fica marcada como
tal (``source == "lab"``). O filtro ``cwv_ruim`` pega qualquer métrica na faixa
ruim.
"""

from __future__ import annotations

import asyncio
import logging
from itertools import count
from time import perf_counter
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

from auditor.checks.cwv_bands import cls_band, inp_band, lcp_band
from auditor.models import CoreWebVitalsResult
from auditor.sitemap import SitemapEntry

logger = logging.getLogger("auditor.cwv")

_PSI_ENDPOINT = (
    "https://pagespeedonline.googleapis.com/pagespeedonline/v5/runPagespeed"
)


class MissingAPIKeyError(RuntimeError):
    """A chave ``PSI_API_KEY`` não foi fornecida."""


def _field_metric(metrics: dict[str, Any], key: str) -> float | None:
    metric = metrics.get(key)
    if isinstance(metric, dict):
        percentile = metric.get("percentile")
        if isinstance(percentile, (int, float)):
            return float(percentile)
    return None


def _lab_numeric(audits: dict[str, Any], key: str) -> float | None:
    audit = audits.get(key)
    if isinstance(audit, dict):
        value = audit.get("numericValue")
        if isinstance(value, (int, float)):
            return float(value)
    return None


def parse_psi_response(
    payload: dict[str, Any],
    *,
    url: str,
    page_type: str,
    origins: tuple[str, ...],
    strategy: str,
) -> CoreWebVitalsResult:
    """Extrai métricas da resposta do PSI e classifica cada faixa.

    Usa dados de campo (CrUX) quando houver as três métricas; caso contrário,
    recorre ao laboratório (Lighthouse). TBT é sempre de laboratório.
    """
    loading = payload.get("loadingExperience", {})
    field_metrics = loading.get("metrics", {}) if isinstance(loading, dict) else {}
    lighthouse = payload.get("lighthouseResult", {})
    audits = lighthouse.get("audits", {}) if isinstance(lighthouse, dict) else {}

    field_lcp = _field_metric(field_metrics, "LARGEST_CONTENTFUL_PAINT_MS")
    field_inp = _field_metric(field_metrics, "INTERACTION_TO_NEXT_PAINT")
    field_cls_raw = _field_metric(field_metrics, "CUMULATIVE_LAYOUT_SHIFT_SCORE")
    # No PSI o CLS de campo vem multiplicado por 100 (ex.: 12 == 0.12).
    field_cls = field_cls_raw / 100.0 if field_cls_raw is not None else None

    has_field = (
        field_lcp is not None and field_inp is not None and field_cls is not None
    )

    # TBT é sempre de laboratório.
    tbt_ms = _lab_numeric(audits, "total-blocking-time")

    if has_field:
        source = "field"
        lcp_ms = field_lcp
        inp_ms = field_inp
        cls = field_cls
    else:
        source = "lab"
        lcp_ms = _lab_numeric(audits, "largest-contentful-paint")
        # Lighthouse não reporta INP de laboratório; TBT é o proxy, mas não
        # substitui INP, então INP fica sem dado no modo laboratório.
        inp_ms = None
        cls = _lab_numeric(audits, "cumulative-layout-shift")

    return CoreWebVitalsResult(
        url=url,
        page_type=page_type,
        origins=origins,
        strategy=strategy,  # type: ignore[arg-type]
        lcp_ms=lcp_ms,
        inp_ms=inp_ms,
        cls=cls,
        tbt_ms=tbt_ms,
        source=source,  # type: ignore[arg-type]
        lcp_band=lcp_band(lcp_ms),  # type: ignore[arg-type]
        inp_band=inp_band(inp_ms),  # type: ignore[arg-type]
        cls_band=cls_band(cls),  # type: ignore[arg-type]
    )


def _error_result(
    entry: SitemapEntry, strategy: str, message: str
) -> CoreWebVitalsResult:
    return CoreWebVitalsResult(
        url=entry.url,
        page_type=entry.page_type,
        origins=entry.origins,
        strategy=strategy,  # type: ignore[arg-type]
        lcp_ms=None,
        inp_ms=None,
        cls=None,
        tbt_ms=None,
        source=None,
        lcp_band="sem_dado",
        inp_band="sem_dado",
        cls_band="sem_dado",
        error_message=message,
    )


async def check_core_web_vitals(
    entries: Iterable[SitemapEntry],
    *,
    api_key: str,
    strategy: str = "mobile",
    concurrency: int = 4,
    timeout_seconds: float = 90,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[CoreWebVitalsResult, ...]:
    """Consulta o PageSpeed Insights para cada entrada e classifica as faixas.

    A chave nunca é incluída em mensagens de erro. Falhas de rede, HTTP de erro
    ou resposta inesperada viram um resultado com ``error_message``, sem derrubar
    as demais URLs.
    """
    if not api_key:
        raise MissingAPIKeyError(
            "PSI_API_KEY não configurada; defina-a no ambiente ou no .env"
        )
    if strategy not in {"mobile", "desktop"}:
        raise ValueError("strategy deve ser 'mobile' ou 'desktop'")
    if concurrency < 1:
        raise ValueError("Concurrency must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("HTTP timeout must be greater than zero")

    selected_entries = tuple(entries)
    total = len(selected_entries)
    semaphore = asyncio.Semaphore(concurrency)
    timeout = httpx.Timeout(timeout_seconds)
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )
    # Contador de progresso (incrementado à medida que cada URL termina).
    finished = count(1)

    logger.info(
        "Iniciando coleta de Core Web Vitals: %d URL(s), estratégia=%s, "
        "concorrência=%d, timeout=%.0fs",
        total,
        strategy,
        concurrency,
        timeout_seconds,
    )

    def _log_done(result: CoreWebVitalsResult, elapsed: float) -> None:
        position = next(finished)
        if result.error_message:
            logger.warning(
                "[%d/%d] FALHA em %.1fs: %s — %s",
                position,
                total,
                elapsed,
                result.url,
                result.error_message,
            )
        else:
            logger.info(
                "[%d/%d] OK em %.1fs: %s — fonte=%s faixa=%s "
                "(LCP=%s INP=%s CLS=%s)",
                position,
                total,
                elapsed,
                result.url,
                result.source,
                result.worst_band,
                result.lcp_ms,
                result.inp_ms,
                result.cls,
            )

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        transport=transport,
    ) as client:

        async def check_one(entry: SitemapEntry) -> CoreWebVitalsResult:
            async with semaphore:
                start = perf_counter()
                logger.debug("Consultando PSI: %s", entry.url)
                result = await _query_one(entry)
                _log_done(result, perf_counter() - start)
                return result

        async def _query_one(entry: SitemapEntry) -> CoreWebVitalsResult:
            try:
                parsed = urlsplit(entry.url)
                valid = parsed.scheme in {"http", "https"} and parsed.hostname
            except ValueError:
                valid = False
            if not valid:
                return _error_result(entry, strategy, "Invalid request URL")

            params = {
                "url": entry.url,
                "strategy": strategy,
                "category": "performance",
                "key": api_key,
            }
            try:
                response = await client.get(_PSI_ENDPOINT, params=params)
            except httpx.TimeoutException as exc:
                return _error_result(
                    entry, strategy, f"Timeout ({type(exc).__name__})"
                )
            except httpx.RequestError as exc:
                return _error_result(
                    entry, strategy, f"Request failure ({type(exc).__name__})"
                )

            if response.status_code != 200:
                # Não incluir corpo/URL (podem conter a chave) na mensagem.
                return _error_result(
                    entry, strategy, f"PSI HTTP {response.status_code}"
                )

            try:
                payload = response.json()
            except ValueError:
                return _error_result(entry, strategy, "PSI resposta inválida")

            try:
                return parse_psi_response(
                    payload,
                    url=entry.url,
                    page_type=entry.page_type,
                    origins=entry.origins,
                    strategy=strategy,
                )
            except Exception as exc:  # noqa: BLE001 - parsing defensivo
                return _error_result(
                    entry, strategy, f"PSI parse failure ({type(exc).__name__})"
                )

        results = tuple(
            await asyncio.gather(
                *(check_one(entry) for entry in selected_entries)
            )
        )

    ok = sum(1 for r in results if r.error_message is None)
    poor = sum(1 for r in results if r.is_poor)
    failed = sum(1 for r in results if r.error_message is not None)
    logger.info(
        "Coleta de Core Web Vitals concluída: %d OK, %d com falha, "
        "%d reprovada(s).",
        ok,
        failed,
        poor,
    )
    return results
