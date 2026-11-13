import asyncio

import httpx
import pytest

from auditor.checks.seo import SEOConfig, check_seo
from auditor.sitemap import SitemapEntry


def _entry(
    url: str = "https://www.example.com/noticia",
    *,
    origins: tuple[str, ...] = ("https://www.example.com/sitemap/articles-current.xml",),
    page_type: str = "article",
) -> SitemapEntry:
    return SitemapEntry(
        url=url,
        lastmod=None,
        origins=origins,
        occurrences=len(origins),
        page_type=page_type,
    )


def _html(
    *,
    title: str | None = "Um titulo de materia com tamanho na faixa recomendada",
    description: str | None = (
        "Uma meta description com conteúdo suficiente para ficar dentro da faixa "
        "recomendada pela comunidade de SEO sem ser cortada no snippet do Google."
    ),
    canonical: str | None = "https://www.example.com/noticia",
    robots: str | None = None,
    h1: int = 1,
    open_graph: bool = True,
) -> str:
    head = []
    if title is not None:
        head.append(f"<title>{title}</title>")
    if description is not None:
        head.append(f'<meta name="description" content="{description}">')
    if canonical is not None:
        head.append(f'<link rel="canonical" href="{canonical}">')
    if robots is not None:
        head.append(f'<meta name="robots" content="{robots}">')
    if open_graph:
        head.append('<meta property="og:title" content="t">')
        head.append('<meta property="og:type" content="article">')
        head.append('<meta property="og:url" content="https://www.example.com/noticia">')
        head.append('<meta property="og:image" content="https://x/i.jpg">')
    body = "".join(f"<h1>Título {i}</h1>" for i in range(h1))
    return f"<html><head>{''.join(head)}</head><body>{body}</body></html>"


def _run(handler, entries, **kwargs):
    return asyncio.run(
        check_seo(entries, transport=httpx.MockTransport(handler), **kwargs)
    )


def test_clean_page_has_no_findings_and_is_ok():
    result = _run(lambda r: httpx.Response(200, html=_html()), [_entry()])[0]

    assert result.error_message is None
    assert result.seo_ok is True
    assert result.findings == ()
    assert result.title is not None
    assert result.canonical == "https://www.example.com/noticia"
    assert result.h1_count == 1
    assert "og:title" in result.open_graph_tags


def test_missing_title_and_description_are_errors():
    html = _html(title=None, description=None)
    result = _run(lambda r: httpx.Response(200, html=html), [_entry()])[0]

    rules = {f.rule: f.severity for f in result.findings}
    assert rules["title_present"] == "error"
    assert rules["description_present"] == "error"
    assert result.seo_ok is False


def test_title_and_description_length_are_warnings_only():
    html = _html(title="Curto", description="Curta demais")
    result = _run(lambda r: httpx.Response(200, html=html), [_entry()])[0]

    severities = {f.rule: f.severity for f in result.findings}
    assert severities["title_length"] == "warning"
    assert severities["description_length"] == "warning"
    # Apenas avisos -> continua OK
    assert result.seo_ok is True


def test_configurable_length_bounds():
    html = _html(title="Título com vinte e dois chars")
    config = SEOConfig(title_min=5, title_max=10)
    result = _run(
        lambda r: httpx.Response(200, html=html), [_entry()], config=config
    )[0]
    length_findings = [f for f in result.findings if f.rule == "title_length"]
    assert length_findings and length_findings[0].severity == "warning"


def test_noindex_is_error():
    html = _html(robots="index, nofollow")
    ok = _run(lambda r: httpx.Response(200, html=html), [_entry()])[0]
    assert ok.has_noindex is False

    html_noindex = _html(robots="noindex, follow")
    result = _run(lambda r: httpx.Response(200, html=html_noindex), [_entry()])[0]
    assert result.has_noindex is True
    assert any(f.rule == "noindex" and f.severity == "error" for f in result.findings)
    assert result.seo_ok is False


def test_canonical_mismatch_is_error_ignoring_www_and_trailing_slash():
    # canonical sem www mas mesma rota: normalizado para www -> sem erro
    html_same = _html(canonical="https://example.com/noticia/")
    ok = _run(lambda r: httpx.Response(200, html=html_same), [_entry()])[0]
    assert not any(f.rule.startswith("canonical") for f in ok.findings)

    html_diff = _html(canonical="https://www.example.com/outra-pagina")
    result = _run(lambda r: httpx.Response(200, html=html_diff), [_entry()])[0]
    assert any(f.rule == "canonical_self" and f.severity == "error" for f in result.findings)


def test_multiple_and_missing_h1_are_errors():
    result_zero = _run(
        lambda r: httpx.Response(200, html=_html(h1=0)), [_entry()]
    )[0]
    assert any(f.rule == "h1_single" and f.severity == "error" for f in result_zero.findings)

    result_two = _run(
        lambda r: httpx.Response(200, html=_html(h1=2)), [_entry()]
    )[0]
    assert result_two.h1_count == 2
    assert any(f.rule == "h1_single" and f.severity == "error" for f in result_two.findings)


def test_missing_open_graph_is_warning():
    html = _html(open_graph=False)
    result = _run(lambda r: httpx.Response(200, html=html), [_entry()])[0]
    og = [f for f in result.findings if f.rule == "open_graph"]
    assert og and og[0].severity == "warning"
    assert result.seo_ok is True


def test_redirect_to_different_url_is_error():
    def handler(request):
        if request.url.path == "/antiga":
            return httpx.Response(301, headers={"Location": "/noticia"})
        return httpx.Response(200, html=_html())

    entry = _entry("https://www.example.com/antiga")
    result = _run(handler, [entry])[0]
    assert result.final_url == "https://www.example.com/noticia"
    assert any(
        f.rule == "sitemap_url_matches_final" and f.severity == "error"
        for f in result.findings
    )
    assert result.seo_ok is False


def test_http_error_becomes_error_result_without_crashing_others():
    def handler(request):
        if request.url.path == "/erro":
            return httpx.Response(500)
        return httpx.Response(200, html=_html())

    entries = [_entry("https://www.example.com/erro"), _entry()]
    results = _run(handler, entries)
    assert results[0].error_message == "HTTP status 500"
    assert results[0].seo_ok is False
    assert results[1].seo_ok is True


def test_network_failure_is_isolated_per_url():
    def handler(request):
        raise httpx.ReadTimeout("simulated", request=request)

    result = _run(handler, [_entry()])[0]
    assert result.error_message.startswith("Timeout")
    assert result.seo_ok is False


def test_invalid_url_is_error_result():
    result = _run(lambda r: httpx.Response(200), [_entry("not-a-url")])[0]
    assert result.error_message == "Invalid request URL"
    assert result.seo_ok is False


def test_problems_property_lists_all_messages():
    html = _html(title=None, open_graph=False)
    result = _run(lambda r: httpx.Response(200, html=html), [_entry()])[0]
    assert len(result.problems) == len(result.findings)
    assert all(isinstance(p, str) for p in result.problems)


@pytest.mark.parametrize(("concurrency", "timeout_seconds"), [(0, 5), (1, 0)])
def test_invalid_configuration_fails_before_requests(concurrency, timeout_seconds):
    with pytest.raises(ValueError):
        asyncio.run(
            check_seo(
                [_entry()],
                concurrency=concurrency,
                timeout_seconds=timeout_seconds,
                transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            )
        )
