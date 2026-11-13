import asyncio

import httpx
import pytest

from auditor.checks.core_web_vitals import (
    MissingAPIKeyError,
    check_core_web_vitals,
    parse_psi_response,
)
from auditor.sitemap import SitemapEntry


def _entry(url="https://www.example.com/", page_type="home"):
    return SitemapEntry(
        url=url, lastmod=None, origins=(), occurrences=1, page_type=page_type
    )


def _field_payload(lcp=1138, inp=159, cls_x100=0):
    return {
        "loadingExperience": {
            "overall_category": "FAST",
            "metrics": {
                "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": lcp, "category": "FAST"},
                "INTERACTION_TO_NEXT_PAINT": {"percentile": inp, "category": "FAST"},
                "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": cls_x100, "category": "FAST"},
            },
        },
        "lighthouseResult": {
            "audits": {
                "total-blocking-time": {"numericValue": 1093.5},
                "largest-contentful-paint": {"numericValue": 3601.0},
                "cumulative-layout-shift": {"numericValue": 0.0},
            }
        },
    }


def _lab_only_payload():
    return {
        "loadingExperience": {"metrics": {}},
        "lighthouseResult": {
            "audits": {
                "total-blocking-time": {"numericValue": 850.0},
                "largest-contentful-paint": {"numericValue": 4200.0},
                "cumulative-layout-shift": {"numericValue": 0.3},
            }
        },
    }


def _parse(payload):
    return parse_psi_response(
        payload,
        url="https://www.example.com/",
        page_type="home",
        origins=(),
        strategy="mobile",
    )


# ----- parsing dados de campo -----

def test_field_data_is_preferred_and_cls_is_divided_by_100():
    result = _parse(_field_payload(lcp=1138, inp=159, cls_x100=12))
    assert result.source == "field"
    assert result.lcp_ms == 1138
    assert result.inp_ms == 159
    assert result.cls == pytest.approx(0.12)
    assert result.tbt_ms == pytest.approx(1093.5)  # TBT sempre de lab


def test_field_bands_follow_google_thresholds():
    # LCP 2600 (precisa melhorar), INP 250 (precisa melhorar), CLS 0.30 (ruim)
    result = _parse(_field_payload(lcp=2600, inp=250, cls_x100=30))
    assert result.lcp_band == "precisa_melhorar"
    assert result.inp_band == "precisa_melhorar"
    assert result.cls_band == "ruim"
    assert result.is_poor is True
    assert result.worst_band == "ruim"


def test_good_metrics_are_not_poor():
    result = _parse(_field_payload(lcp=2000, inp=150, cls_x100=5))
    assert result.lcp_band == "bom"
    assert result.inp_band == "bom"
    assert result.cls_band == "bom"
    assert result.is_poor is False
    assert result.worst_band == "bom"


def test_boundary_values_are_good_not_needs_improvement():
    # Limites exatos do "bom": LCP 2500, INP 200, CLS 0.10
    result = _parse(_field_payload(lcp=2500, inp=200, cls_x100=10))
    assert result.lcp_band == "bom"
    assert result.inp_band == "bom"
    assert result.cls_band == "bom"


# ----- fallback para laboratório -----

def test_lab_fallback_when_no_field_data():
    result = _parse(_lab_only_payload())
    assert result.source == "lab"
    assert result.lcp_ms == pytest.approx(4200.0)
    assert result.cls == pytest.approx(0.3)
    assert result.inp_ms is None  # INP não existe em laboratório
    assert result.inp_band == "sem_dado"
    assert result.lcp_band == "ruim"  # 4200 ms > 4000 ms
    assert result.cls_band == "ruim"
    assert result.is_poor is True


# ----- fluxo assíncrono -----

def _run(handler, entries, **kwargs):
    return asyncio.run(
        check_core_web_vitals(
            entries,
            api_key="FAKE_KEY",
            transport=httpx.MockTransport(handler),
            **kwargs,
        )
    )


def test_check_collects_metrics_over_http():
    result = _run(
        lambda r: httpx.Response(200, json=_field_payload()), [_entry()]
    )[0]
    assert result.error_message is None
    assert result.source == "field"
    assert result.lcp_ms == 1138


def test_strategy_is_sent_as_query_param():
    captured = {}

    def handler(request):
        captured["strategy"] = request.url.params.get("strategy")
        captured["has_key"] = "key" in request.url.params
        return httpx.Response(200, json=_field_payload())

    _run(handler, [_entry()], strategy="desktop")
    assert captured["strategy"] == "desktop"
    assert captured["has_key"] is True


def test_http_error_does_not_leak_api_key():
    result = _run(lambda r: httpx.Response(429, text="quota"), [_entry()])[0]
    assert result.error_message == "PSI HTTP 429"
    assert "FAKE_KEY" not in (result.error_message or "")


def test_network_failure_is_isolated():
    def handler(request):
        raise httpx.ReadTimeout("simulated", request=request)

    result = _run(handler, [_entry()])[0]
    assert result.error_message.startswith("Timeout")
    assert "FAKE_KEY" not in (result.error_message or "")


def test_failures_are_per_url():
    def handler(request):
        if "fail" in request.url.params.get("url", ""):
            return httpx.Response(500)
        return httpx.Response(200, json=_field_payload())

    entries = [
        _entry("https://www.example.com/ok"),
        _entry("https://www.example.com/fail"),
    ]
    results = _run(handler, entries)
    assert results[0].error_message is None
    assert results[1].error_message == "PSI HTTP 500"


def test_missing_api_key_raises():
    with pytest.raises(MissingAPIKeyError):
        asyncio.run(
            check_core_web_vitals(
                [_entry()],
                api_key="",
                transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            )
        )


@pytest.mark.parametrize(
    ("strategy", "concurrency", "timeout_seconds"),
    [("invalid", 4, 90), ("mobile", 0, 90), ("mobile", 4, 0)],
)
def test_invalid_configuration_fails_before_requests(
    strategy, concurrency, timeout_seconds
):
    with pytest.raises(ValueError):
        asyncio.run(
            check_core_web_vitals(
                [_entry()],
                api_key="FAKE_KEY",
                strategy=strategy,
                concurrency=concurrency,
                timeout_seconds=timeout_seconds,
                transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            )
        )
