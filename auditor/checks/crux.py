"""Coleta de Core Web Vitals via Chrome UX Report (CrUX) API.

A CrUX API devolve dados de campo reais (CrUX, 28 dias) já agregados, sem
renderizar a página — por isso é rápida (~1 s por URL) frente ao PageSpeed
Insights (~20-30 s). Coleta LCP, INP e CLS no percentil 75 (o que o Google usa
para classificar). Não há TBT nem score de laboratório (fica ``None``).

Requer a mesma chave ``PSI_API_KEY`` do ambiente/.env e que a *Chrome UX Report
API* esteja habilitada no projeto do Google Cloud. A chave nunca é registrada em
relatórios ou logs.

Faixas: ver :mod:`auditor.checks.cwv_bands`.
"""

from __future__ import annotations

import asyncio
import logging
from itertools import count
from time import perf_counter
from typing import Any, Iterable

import httpx

from auditor.checks.cwv_bands import cls_band, inp_band, lcp_band
from auditor.models import CoreWebVitalsResult
from auditor.sitemap import SitemapEntry

logger = logging.getLogger("auditor.cwv")

_CRUX_ENDPOINT = (
    "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
)
_FORM_FACTOR = {"mobile": "PHONE", "desktop": "DESKTOP"}

# A CrUX limita queries por janela de ~100s por chave. Como a janela é longa, um
# 429 esgotou a cota e exige esperar a janela renovar — por isso o backoff é
# longo e cresce bastante, além de respeitar o header Retry-After quando vier.
_MAX_RETRIES = 7
_RETRY_BASE_DELAY = 10.0
_RETRY_MAX_DELAY = 120.0


def _retry_after_seconds(response: httpx.Response, fallback: float) -> float:
    """Lê o header Retry-After (segundos), com teto, ou usa o fallback."""
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            return min(float(raw), _RETRY_MAX_DELAY)
        except ValueError:
            pass
    return min(fallback, _RETRY_MAX_DELAY)


class MissingAPIKeyError(RuntimeError):
    """A chave ``PSI_API_KEY`` não foi fornecida."""


def _p75(metrics: dict[str, Any], key: str) -> float | None:
    metric = metrics.get(key)
    if not isinstance(metric, dict):
        return None
    percentiles = metric.get("percentiles")
    if not isinstance(percentiles, dict):
        return None
    value = percentiles.get("p75")
    # CLS vem como string decimal (ex.: "0.08"); LCP/INP como número em ms.
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def parse_crux_record(
    payload: dict[str, Any],
    *,
    url: str,
    page_type: str,
    origins: tuple[str, ...],
    strategy: str,
) -> CoreWebVitalsResult:
    """Extrai LCP/INP/CLS (p75) de um registro CrUX e classifica as faixas."""
    record = payload.get("record", {}) if isinstance(payload, dict) else {}
    metrics = record.get("metrics", {}) if isinstance(record, dict) else {}

    lcp_ms = _p75(metrics, "largest_contentful_paint")
    inp_ms = _p75(metrics, "interaction_to_next_paint")
    cls = _p75(metrics, "cumulative_layout_shift")

    return CoreWebVitalsResult(
        url=url,
        page_type=page_type,
        origins=origins,
        strategy=strategy,  # type: ignore[arg-type]
        lcp_ms=lcp_ms,
        inp_ms=inp_ms,
        cls=cls,
        tbt_ms=None,  # CrUX não fornece TBT (métrica de laboratório).
        source="field",
        lcp_band=lcp_band(lcp_ms),  # type: ignore[arg-type]
        inp_band=inp_band(inp_ms),  # type: ignore[arg-type]
        cls_band=cls_band(cls),  # type: ignore[arg-type]
    )


class _RateLimiter:
    """Espaça requisições para respeitar um limite de QPS médio.

    Garante um intervalo mínimo entre o início de requisições consecutivas
    (``1 / max_qps``), evitando estourar a cota da CrUX de forma preventiva.
    """

    def __init__(self, max_qps: float) -> None:
        self._min_interval = 1.0 / max_qps if max_qps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = perf_counter()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = perf_counter()
            self._next_allowed = max(now, self._next_allowed) + self._min_interval


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


async def check_core_web_vitals_crux(
    entries: Iterable[SitemapEntry],
    *,
    api_key: str,
    strategy: str = "mobile",
    concurrency: int = 4,
    timeout_seconds: float = 30,
    max_qps: float = 3.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[CoreWebVitalsResult, ...]:
    """Consulta a CrUX API para cada URL e classifica as faixas.

    URLs sem dados de campo no CrUX (comum em páginas de baixo tráfego) retornam
    HTTP 404 e viram um resultado com ``error_message`` (métricas ``sem_dado``),
    sem derrubar as demais. A chave nunca aparece em mensagens de erro.

    ``max_qps`` espaça as requisições de forma preventiva para não estourar a
    cota da CrUX; 429/403 (rate limit) são retentados com backoff longo que
    respeita o header ``Retry-After``.
    """
    if not api_key:
        raise MissingAPIKeyError(
            "PSI_API_KEY não configurada; defina-a no ambiente ou no .env"
        )
    if strategy not in _FORM_FACTOR:
        raise ValueError("strategy deve ser 'mobile' ou 'desktop'")
    if concurrency < 1:
        raise ValueError("Concurrency must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("HTTP timeout must be greater than zero")
    if max_qps <= 0:
        raise ValueError("max_qps deve ser maior que zero")

    selected_entries = tuple(entries)
    total = len(selected_entries)
    form_factor = _FORM_FACTOR[strategy]
    semaphore = asyncio.Semaphore(concurrency)
    rate_limiter = _RateLimiter(max_qps)
    timeout = httpx.Timeout(timeout_seconds)
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )
    finished = count(1)

    logger.info(
        "Iniciando coleta de Core Web Vitals (CrUX): %d URL(s), "
        "estratégia=%s, concorrência=%d, max_qps=%.1f, timeout=%.0fs",
        total,
        strategy,
        concurrency,
        max_qps,
        timeout_seconds,
    )

    def _log_done(result: CoreWebVitalsResult, elapsed: float) -> None:
        position = next(finished)
        if result.error_message:
            logger.warning(
                "[%d/%d] SEM DADO em %.1fs: %s — %s",
                position, total, elapsed, result.url, result.error_message,
            )
        else:
            logger.info(
                "[%d/%d] OK em %.1fs: %s — faixa=%s "
                "(LCP=%s INP=%s CLS=%s)",
                position, total, elapsed, result.url, result.worst_band,
                result.lcp_ms, result.inp_ms, result.cls,
            )

    async with httpx.AsyncClient(
        timeout=timeout, limits=limits, transport=transport
    ) as client:

        async def check_one(entry: SitemapEntry) -> CoreWebVitalsResult:
            async with semaphore:
                start = perf_counter()
                result = await _query_one(entry)
                _log_done(result, perf_counter() - start)
                return result

        async def _query_one(entry: SitemapEntry) -> CoreWebVitalsResult:
            body = {"url": entry.url, "formFactor": form_factor}
            # A CrUX limita a ~400 queries/100s por chave. Picos de concorrência
            # disparam 429/403 de rate limit mesmo havendo dados — nesses casos
            # tentamos de novo com backoff, em vez de descartar a URL.
            delay = _RETRY_BASE_DELAY
            for attempt in range(_MAX_RETRIES + 1):
                await rate_limiter.acquire()
                try:
                    response = await client.post(
                        _CRUX_ENDPOINT, params={"key": api_key}, json=body
                    )
                except httpx.TimeoutException as exc:
                    return _error_result(
                        entry, strategy, f"Timeout ({type(exc).__name__})"
                    )
                except httpx.RequestError as exc:
                    return _error_result(
                        entry, strategy, f"Request failure ({type(exc).__name__})"
                    )

                status = response.status_code
                if status == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        return _error_result(entry, strategy, "CrUX resposta inválida")
                    try:
                        return parse_crux_record(
                            payload,
                            url=entry.url,
                            page_type=entry.page_type,
                            origins=entry.origins,
                            strategy=strategy,
                        )
                    except Exception as exc:  # noqa: BLE001 - parsing defensivo
                        return _error_result(
                            entry, strategy, f"CrUX parse failure ({type(exc).__name__})"
                        )

                if status == 404:
                    # 404 = realmente não há dados de campo para esta URL.
                    return _error_result(entry, strategy, "Sem dados de campo no CrUX")

                if status in (429, 403) and attempt < _MAX_RETRIES:
                    # Rate limit: a cota é por janela longa (~100s), então
                    # espera o Retry-After (se houver) ou um backoff longo.
                    wait = _retry_after_seconds(response, delay)
                    logger.debug(
                        "Rate limit em %s (tentativa %d/%d); aguardando %.0fs",
                        entry.url, attempt + 1, _MAX_RETRIES, wait,
                    )
                    await asyncio.sleep(wait)
                    delay = min(delay * 2, _RETRY_MAX_DELAY)
                    continue

                # Não ecoar corpo/URL (podem conter a chave).
                if status in (429, 403):
                    return _error_result(
                        entry, strategy,
                        f"Rate limit CrUX (HTTP {status}) após {_MAX_RETRIES} tentativas",
                    )
                return _error_result(entry, strategy, f"CrUX HTTP {status}")

            return _error_result(entry, strategy, "CrUX sem resposta")

        results = tuple(
            await asyncio.gather(
                *(check_one(entry) for entry in selected_entries)
            )
        )

    ok = sum(1 for r in results if r.error_message is None)
    without_data = sum(1 for r in results if r.error_message is not None)
    poor = sum(1 for r in results if r.is_poor)
    logger.info(
        "Coleta CrUX concluída: %d com dados, %d sem dados, %d reprovada(s).",
        ok, without_data, poor,
    )
    return results
