import asyncio

import httpx
import pytest

from auditor.checks.crux import (
    MissingAPIKeyError,
    check_core_web_vitals_crux,
    parse_crux_record,
)
from auditor.sitemap import SitemapEntry


def _entry(url="https://www.example.com/", page_type="home"):
    return SitemapEntry(
        url=url, lastmod=None, origins=(), occurrences=1, page_type=page_type
    )


def _record(lcp=1138, inp=159, cls="0.05"):
    return {
        "record": {
            "key": {"formFactor": "PHONE", "url": "https://www.example.com/"},
            "metrics": {
                "largest_contentful_paint": {"percentiles": {"p75": lcp}},
                "interaction_to_next_paint": {"percentiles": {"p75": inp}},
                "cumulative_layout_shift": {"percentiles": {"p75": cls}},
            },
        }
    }


def _parse(payload):
    return parse_crux_record(
        payload, url="https://www.example.com/", page_type="home",
        origins=(), strategy="mobile",
    )


# ----- parsing -----

def test_parses_p75_and_cls_string():
    r = _parse(_record(lcp=1138, inp=159, cls="0.05"))
    assert r.source == "field"
    assert r.lcp_ms == 1138
    assert r.inp_ms == 159
    assert r.cls == pytest.approx(0.05)
    assert r.tbt_ms is None  # CrUX não tem TBT
    assert r.lcp_band == "bom"
    assert r.inp_band == "bom"
    assert r.cls_band == "bom"
    assert r.is_poor is False


def test_bands_follow_google_thresholds():
    r = _parse(_record(lcp=4200, inp=600, cls="0.30"))
    assert r.lcp_band == "ruim"
    assert r.inp_band == "ruim"
    assert r.cls_band == "ruim"
    assert r.is_poor is True
    assert r.worst_band == "ruim"


def test_missing_metric_is_sem_dado():
    payload = {"record": {"metrics": {
        "largest_contentful_paint": {"percentiles": {"p75": 1000}},
    }}}
    r = _parse(payload)
    assert r.lcp_ms == 1000
    assert r.inp_ms is None
    assert r.cls is None
    assert r.inp_band == "sem_dado"
    assert r.cls_band == "sem_dado"


# ----- fluxo assíncrono -----

def _run(handler, entries, **kwargs):
    # max_qps alto por padrão para não adicionar pacing real nos testes.
    kwargs.setdefault("max_qps", 1000.0)
    return asyncio.run(
        check_core_web_vitals_crux(
            entries, api_key="FAKE", transport=httpx.MockTransport(handler), **kwargs
        )
    )


def test_collects_over_http_and_sends_form_factor():
    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        captured["has_key"] = "key" in request.url.params
        return httpx.Response(200, json=_record())

    r = _run(handler, [_entry()], strategy="desktop")[0]
    assert r.error_message is None
    assert r.lcp_ms == 1138
    assert captured["body"]["formFactor"] == "DESKTOP"
    assert captured["has_key"] is True


def test_404_means_no_field_data_and_does_not_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, json={"error": {}})

    r = _run(handler, [_entry()])[0]
    assert r.error_message == "Sem dados de campo no CrUX"
    assert r.is_poor is False
    assert calls["n"] == 1  # 404 não deve ser retentado


def test_rate_limit_is_retried_then_succeeds(monkeypatch):
    import auditor.checks.crux as crux_mod

    monkeypatch.setattr(crux_mod, "_RETRY_BASE_DELAY", 0.0)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(403, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
        return httpx.Response(200, json=_record())

    r = _run(handler, [_entry()])[0]
    assert r.error_message is None
    assert r.lcp_ms == 1138
    assert calls["n"] == 3  # 2 rate limits + 1 sucesso


def test_persistent_rate_limit_gives_up_with_clear_message(monkeypatch):
    import auditor.checks.crux as crux_mod

    monkeypatch.setattr(crux_mod, "_RETRY_BASE_DELAY", 0.0)
    r = _run(lambda req: httpx.Response(429), [_entry()], max_qps=1000.0)[0]
    assert "Rate limit" in r.error_message
    assert "FAKE" not in r.error_message


def test_retry_after_header_is_honored(monkeypatch):
    import auditor.checks.crux as crux_mod

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(crux_mod.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=_record())

    r = _run(handler, [_entry()], max_qps=1000.0)[0]
    assert r.error_message is None
    # Esperou exatamente os 7s do header antes de retentar.
    assert 7.0 in slept


def test_retry_after_is_capped(monkeypatch):
    import auditor.checks.crux as crux_mod

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(crux_mod.asyncio, "sleep", fake_sleep)

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "9999"})

    _run(handler, [_entry()], max_qps=1000.0)
    # Nenhuma espera deve exceder o teto configurado.
    assert all(s <= crux_mod._RETRY_MAX_DELAY for s in slept)


def test_http_error_does_not_leak_key():
    # 500 não é rate limit -> erro direto, sem retry.
    r = _run(lambda req: httpx.Response(500, text="erro interno"), [_entry()])[0]
    assert r.error_message == "CrUX HTTP 500"
    assert "FAKE" not in (r.error_message or "")


def test_failures_are_per_url():
    import json

    def handler(request):
        body = json.loads(request.content)
        if "fail" in body["url"]:
            return httpx.Response(404)
        return httpx.Response(200, json=_record())

    entries = [_entry("https://www.example.com/ok"), _entry("https://www.example.com/fail")]
    results = _run(handler, entries)
    assert results[0].error_message is None
    assert results[1].error_message == "Sem dados de campo no CrUX"


def test_missing_api_key_raises():
    with pytest.raises(MissingAPIKeyError):
        asyncio.run(
            check_core_web_vitals_crux(
                [_entry()], api_key="",
                transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            )
        )


@pytest.mark.parametrize(
    ("strategy", "concurrency", "timeout_seconds"),
    [("invalid", 4, 30), ("mobile", 0, 30), ("mobile", 4, 0)],
)
def test_invalid_config_fails_before_requests(strategy, concurrency, timeout_seconds):
    with pytest.raises(ValueError):
        asyncio.run(
            check_core_web_vitals_crux(
                [_entry()], api_key="FAKE", strategy=strategy,
                concurrency=concurrency, timeout_seconds=timeout_seconds,
                transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            )
        )
