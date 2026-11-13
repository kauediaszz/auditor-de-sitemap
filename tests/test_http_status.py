import asyncio

import httpx
import pytest

from auditor.checks.http_status import check_http_status
from auditor.sitemap import SitemapEntry


def _entry(
    url: str,
    *,
    origins: tuple[str, ...] = ("https://example.test/sitemap/source.xml",),
    page_type: str = "article",
) -> SitemapEntry:
    return SitemapEntry(
        url=url,
        lastmod=None,
        origins=origins,
        occurrences=len(origins),
        page_type=page_type,
    )


def _run(handler, entries, **kwargs):
    return asyncio.run(
        check_http_status(
            entries,
            transport=httpx.MockTransport(handler),
            **kwargs,
        )
    )


@pytest.mark.parametrize(
    ("status_code", "outcome", "error_message"),
    [
        (200, "success", None),
        (304, "other_http_status", None),
        (404, "http_error", "HTTP status 404"),
        (500, "http_error", "HTTP status 500"),
    ],
)
def test_http_statuses_are_recorded_without_synthetic_codes(
    status_code, outcome, error_message
):
    original_url = "https://example.test/article?edition=mobile#content"
    result = _run(
        lambda request: httpx.Response(status_code),
        [_entry(original_url)],
    )[0]

    assert result.original_url == original_url
    assert result.status_code == status_code
    assert result.final_url == original_url
    assert result.redirect_count == 0
    assert result.redirect_statuses == ()
    assert result.outcome == outcome
    assert result.error_message == error_message
    assert result.elapsed_ms is not None and result.elapsed_ms >= 0


def test_redirect_records_chain_final_url_and_entry_metadata():
    original_url = "https://example.test/old-path?keep=1#fragment"
    final_url = "https://example.test/new-path?keep=1#fragment"

    def handler(request):
        if request.url.path == "/old-path":
            return httpx.Response(301, headers={"Location": "/new-path?keep=1#fragment"})
        return httpx.Response(200)

    entry = _entry(
        original_url,
        origins=(
            "https://example.test/sitemap/articles-current.xml",
            "https://example.test/sitemap/homes.xml",
        ),
        page_type="article",
    )
    result = _run(handler, [entry])[0]

    assert result.original_url == original_url
    assert result.status_code == 200
    assert result.final_url == final_url
    assert result.redirect_count == 1
    assert result.redirect_statuses == (301,)
    assert result.outcome == "redirect"
    assert result.origins == entry.origins
    assert result.page_type == entry.page_type


@pytest.mark.parametrize(
    ("final_status", "expected_outcome", "expected_error"),
    [
        (200, "redirect", None),
        (304, "other_http_status", None),
        (404, "http_error", "HTTP status 404"),
        (500, "http_error", "HTTP status 500"),
    ],
)
def test_302_chain_is_classified_by_final_status(
    final_status, expected_outcome, expected_error
):
    original_url = "https://example.test/start?source=sitemap#story"
    final_url = "https://example.test/final?source=sitemap#story"

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final?source=sitemap#story"})
        return httpx.Response(final_status)

    entry = _entry(
        original_url,
        origins=(
            "https://example.test/sitemap/articles-current.xml",
            "https://example.test/sitemap/homes.xml",
        ),
        page_type="article",
    )
    result = _run(handler, [entry])[0]

    assert result.original_url == original_url
    assert result.final_url == final_url
    assert result.status_code == final_status
    assert result.redirect_statuses == (302,)
    assert result.redirect_count == 1
    assert result.outcome == expected_outcome
    assert result.error_message == expected_error
    assert result.origins == entry.origins
    assert result.page_type == entry.page_type
    assert result.elapsed_ms is not None


def test_multiple_redirects_preserve_intermediate_statuses_in_order():
    original_url = "https://example.test/first"
    final_url = "https://example.test/final"

    def handler(request):
        redirects = {
            "/first": (301, "/second"),
            "/second": (302, "/third"),
            "/third": (307, "/final"),
        }
        if request.url.path in redirects:
            status, location = redirects[request.url.path]
            return httpx.Response(status, headers={"Location": location})
        return httpx.Response(200)

    result = _run(handler, [_entry(original_url)])[0]

    assert result.original_url == original_url
    assert result.final_url == final_url
    assert result.status_code == 200
    assert result.redirect_statuses == (301, 302, 307)
    assert result.redirect_count == 3
    assert result.outcome == "redirect"
    assert result.error_message is None


def test_redirect_loop_is_request_failure_without_synthetic_http_status():
    def handler(request):
        return httpx.Response(302, headers={"Location": "/loop"})

    result = _run(handler, [_entry("https://example.test/loop")])[0]

    assert result.original_url == "https://example.test/loop"
    assert result.status_code is None
    assert result.final_url is None
    assert result.redirect_count is None
    assert result.redirect_statuses == ()
    assert result.outcome == "request_error"
    assert result.error_message.startswith("Request failure (TooManyRedirects)")


@pytest.mark.parametrize(
    ("exception_type", "outcome", "message"),
    [
        (httpx.ReadTimeout, "timeout", "Timeout"),
        (httpx.ConnectError, "connection_error", "Connection failure"),
        (httpx.RemoteProtocolError, "request_error", "Request failure"),
    ],
)
def test_request_failures_are_distinguished_per_url(
    exception_type, outcome, message
):
    entry = _entry("https://example.test/fails")

    def handler(request):
        raise exception_type("simulated failure", request=request)

    result = _run(handler, [entry])[0]
    assert result.original_url == entry.url
    assert result.status_code is None
    assert result.final_url is None
    assert result.redirect_count is None
    assert result.outcome == outcome
    assert result.error_message.startswith(message)


def test_individual_failure_does_not_discard_other_results():
    urls = [
        "https://example.test/ok",
        "https://example.test/timeout",
        "https://example.test/not-found",
    ]

    def handler(request):
        if request.url.path == "/timeout":
            raise httpx.ReadTimeout("simulated", request=request)
        if request.url.path == "/not-found":
            return httpx.Response(404)
        return httpx.Response(200)

    results = _run(handler, [_entry(url) for url in urls])
    assert [result.original_url for result in results] == urls
    assert [result.outcome for result in results] == [
        "success",
        "timeout",
        "http_error",
    ]
    assert [result.status_code for result in results] == [200, None, 404]


def test_invalid_request_url_is_a_per_url_request_error():
    result = _run(
        lambda request: httpx.Response(200),
        [_entry("not-an-absolute-url")],
    )[0]
    assert result.original_url == "not-an-absolute-url"
    assert result.status_code is None
    assert result.outcome == "request_error"
    assert result.error_message.startswith("Invalid request URL")


def test_configured_concurrency_and_timeout_are_used():
    class TrackingTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.timeout_extensions = []

        async def handle_async_request(self, request):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.timeout_extensions.append(request.extensions["timeout"])
            await asyncio.sleep(0.01)
            self.active -= 1
            return httpx.Response(200, request=request)

    transport = TrackingTransport()
    entries = [_entry(f"https://example.test/{index}") for index in range(6)]
    results = asyncio.run(
        check_http_status(
            entries,
            concurrency=2,
            timeout_seconds=4.5,
            transport=transport,
        )
    )

    assert len(results) == len(entries)
    assert all(result.outcome == "success" for result in results)
    assert transport.max_active == 2
    assert all(
        timeout == {
            "connect": 4.5,
            "read": 4.5,
            "write": 4.5,
            "pool": 4.5,
        }
        for timeout in transport.timeout_extensions
    )


@pytest.mark.parametrize(
    ("concurrency", "timeout_seconds"),
    [(0, 5), (1, 0), (1, -1)],
)
def test_invalid_configuration_fails_before_requests(concurrency, timeout_seconds):
    with pytest.raises(ValueError):
        asyncio.run(
            check_http_status(
                [_entry("https://example.test/")],
                concurrency=concurrency,
                timeout_seconds=timeout_seconds,
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200)
                ),
            )
        )


def test_transient_failure_then_success_is_not_marked_down(monkeypatch):
    """Uma falha transitória seguida de 200 não pode virar 'fora do ar'."""
    import auditor.checks.http_status as http_status_module

    # Evita esperar o backoff real entre as tentativas durante o teste.
    async def _instant_sleep(_seconds):
        return None

    monkeypatch.setattr(http_status_module.asyncio, "sleep", _instant_sleep)

    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("conexão recusada", request=request)
        return httpx.Response(200)

    result = _run(handler, [_entry("https://example.test/intermitente")])[0]

    assert calls["count"] == 2  # falhou uma vez, teve sucesso na segunda
    assert result.status_code == 200
    assert result.outcome == "success"
    assert result.is_down is False
    assert result.error_message is None


def test_persistent_failure_after_retries_is_down(monkeypatch):
    """Se a falha persiste em todas as tentativas, a URL fica fora do ar."""
    import auditor.checks.http_status as http_status_module

    async def _instant_sleep(_seconds):
        return None

    monkeypatch.setattr(http_status_module.asyncio, "sleep", _instant_sleep)

    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        raise httpx.ConnectError("sempre falha", request=request)

    result = _run(
        handler,
        [_entry("https://example.test/sempre-cai")],
        max_attempts=3,
    )[0]

    assert calls["count"] == 3
    assert result.outcome == "connection_error"
    assert result.is_down is True


def test_http_error_is_not_retried(monkeypatch):
    """Um 404/500 é erro real e não deve ser retentado."""
    import auditor.checks.http_status as http_status_module

    async def _instant_sleep(_seconds):  # pragma: no cover - não deve ser usado
        return None

    monkeypatch.setattr(http_status_module.asyncio, "sleep", _instant_sleep)

    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        return httpx.Response(404)

    result = _run(handler, [_entry("https://example.test/404")])[0]

    assert calls["count"] == 1  # sem retry
    assert result.status_code == 404
    assert result.outcome == "http_error"
