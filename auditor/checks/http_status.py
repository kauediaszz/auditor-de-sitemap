"""HTTP status checks for the selected sitemap entries."""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from typing import Iterable
from urllib.parse import urlsplit

import httpx

from auditor.models import HTTPStatusResult
from auditor.sitemap import SitemapEntry

logger = logging.getLogger("auditor.http")

_USER_AGENT = "SitemapAuditor/0.1 (HTTP status check)"

# Uma URL só é considerada "fora do ar" depois de esgotar as tentativas abaixo.
# Falhas transitórias (timeout curto, queda de conexão, reset) são retentadas
# com um backoff curto, porque o servidor às vezes recusa uma conexão
# isolada sob concorrência mesmo quando a página responde 200 ao ser aberta.
_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.75
_RETRY_MAX_DELAY = 3.0


async def check_http_status(
    entries: Iterable[SitemapEntry],
    *,
    concurrency: int = 10,
    timeout_seconds: float = 4,
    transport: httpx.AsyncBaseTransport | None = None,
    max_attempts: int = _MAX_ATTEMPTS,
) -> tuple[HTTPStatusResult, ...]:
    """Check each supplied sitemap entry, following HTTP redirects.

    Cada tentativa tem um limite de ``timeout_seconds`` (4 s por padrão) para
    ler o status da página. Falhas transitórias (timeout, erro de conexão ou de
    rede) são retentadas até ``max_attempts`` vezes com um backoff curto; só se
    todas falharem a URL é marcada como fora do ar. Respostas 4xx/5xx são erro
    real e não são retentadas.

    The recorded status is the final response status. Intermediate 3xx statuses
    are available in ``redirect_statuses``. A final HTTP error takes precedence
    over a redirect outcome; successful redirect chains remain ``redirect``.
    ``error_message`` is reserved for failures, so it is empty for 304 and
    successful responses. ``elapsed_ms`` measures the time until the final
    response headers are received, including redirect requests, and excludes
    downloading the response body.
    """
    if concurrency < 1:
        raise ValueError("Concurrency must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("HTTP timeout must be greater than zero")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

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

        async def check_one(entry: SitemapEntry) -> HTTPStatusResult:
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
                    return HTTPStatusResult(
                        original_url=entry.url,
                        status_code=None,
                        final_url=None,
                        elapsed_ms=0.0,
                        redirect_count=None,
                        redirect_statuses=(),
                        outcome="request_error",
                        error_message="Invalid request URL",
                        origins=entry.origins,
                        page_type=entry.page_type,
                    )

                # Retry só para falhas transitórias; um 4xx/5xx é erro real e
                # retorna na hora. A última tentativa é a que "vale".
                last_transient: HTTPStatusResult | None = None
                for attempt in range(1, max_attempts + 1):
                    start_time = perf_counter()
                    try:
                        async with client.stream("GET", entry.url) as response:
                            elapsed_ms = (perf_counter() - start_time) * 1000
                            statuses = tuple(
                                previous.status_code
                                for previous in response.history
                            )
                            redirect_count = len(response.history)
                            status_code = response.status_code
                            final_url = str(response.url)
                    except httpx.TimeoutException as exc:
                        last_transient = HTTPStatusResult(
                            original_url=entry.url,
                            status_code=None,
                            final_url=None,
                            elapsed_ms=(perf_counter() - start_time) * 1000,
                            redirect_count=None,
                            redirect_statuses=(),
                            outcome="timeout",
                            error_message=f"Timeout ({type(exc).__name__})",
                            origins=entry.origins,
                            page_type=entry.page_type,
                        )
                    except httpx.InvalidURL as exc:
                        return HTTPStatusResult(
                            original_url=entry.url,
                            status_code=None,
                            final_url=None,
                            elapsed_ms=(perf_counter() - start_time) * 1000,
                            redirect_count=None,
                            redirect_statuses=(),
                            outcome="request_error",
                            error_message=f"Invalid request URL ({type(exc).__name__})",
                            origins=entry.origins,
                            page_type=entry.page_type,
                        )
                    except httpx.ConnectError as exc:
                        last_transient = HTTPStatusResult(
                            original_url=entry.url,
                            status_code=None,
                            final_url=None,
                            elapsed_ms=(perf_counter() - start_time) * 1000,
                            redirect_count=None,
                            redirect_statuses=(),
                            outcome="connection_error",
                            error_message=f"Connection failure ({type(exc).__name__})",
                            origins=entry.origins,
                            page_type=entry.page_type,
                        )
                    except httpx.RequestError as exc:
                        last_transient = HTTPStatusResult(
                            original_url=entry.url,
                            status_code=None,
                            final_url=None,
                            elapsed_ms=(perf_counter() - start_time) * 1000,
                            redirect_count=None,
                            redirect_statuses=(),
                            outcome="request_error",
                            error_message=f"Request failure ({type(exc).__name__})",
                            origins=entry.origins,
                            page_type=entry.page_type,
                        )
                    else:
                        # Teve resposta: resolve o resultado e retorna (sem
                        # retry, inclusive para 4xx/5xx, que são erro real).
                        return _resolve_result(
                            entry,
                            status_code=status_code,
                            final_url=final_url,
                            elapsed_ms=elapsed_ms,
                            redirect_count=redirect_count,
                            statuses=statuses,
                        )

                    # Falha transitória: espera um pouco e tenta de novo.
                    if attempt < max_attempts:
                        delay = min(
                            _RETRY_BASE_DELAY * attempt, _RETRY_MAX_DELAY
                        )
                        logger.debug(
                            "Falha transitória em %s (tentativa %d/%d): %s; "
                            "retry em %.2fs",
                            entry.url,
                            attempt,
                            max_attempts,
                            last_transient.error_message,
                            delay,
                        )
                        await asyncio.sleep(delay)

                # Esgotou as tentativas: a URL está realmente fora do ar.
                assert last_transient is not None
                return last_transient

        return tuple(
            await asyncio.gather(
                *(check_one(entry) for entry in selected_entries)
            )
        )


def _resolve_result(
    entry: SitemapEntry,
    *,
    status_code: int,
    final_url: str,
    elapsed_ms: float,
    redirect_count: int,
    statuses: tuple[int, ...],
) -> HTTPStatusResult:
    """Classifica uma resposta HTTP bem-sucedida em um ``HTTPStatusResult``."""
    if status_code == 304:
        outcome = "other_http_status"
        error_message = None
    elif status_code >= 400:
        outcome = "http_error"
        error_message = f"HTTP status {status_code}"
    elif redirect_count or (300 <= status_code < 400 and status_code != 304):
        outcome = "redirect"
        error_message = None
    elif 200 <= status_code < 300:
        outcome = "success"
        error_message = None
    elif 300 <= status_code < 400:
        outcome = "other_http_status"
        error_message = None
    else:
        outcome = "other_http_status"
        error_message = f"HTTP status {status_code}"

    return HTTPStatusResult(
        original_url=entry.url,
        status_code=status_code,
        final_url=final_url,
        elapsed_ms=elapsed_ms,
        redirect_count=redirect_count,
        redirect_statuses=statuses,
        outcome=outcome,
        error_message=error_message,
        origins=entry.origins,
        page_type=entry.page_type,
    )
