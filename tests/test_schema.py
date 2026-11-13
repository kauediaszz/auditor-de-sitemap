import asyncio
import json

import httpx
import pytest

from auditor.checks.schema import analyze_schema, check_schema
from auditor.checks.schema_rules import (
    flatten_graph,
    load_rules,
)
from auditor.sitemap import SitemapEntry

RULES_DIR = "config/schema_rules"


def _entry(url: str, page_type: str) -> SitemapEntry:
    return SitemapEntry(
        url=url,
        lastmod=None,
        origins=("https://www.example.com/sitemap/articles-current.xml",),
        occurrences=1,
        page_type=page_type,
    )


def _html(blocks) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(b)}</script>'
        for b in blocks
    )
    return f"<html><head>{scripts}</head><body></body></html>"


# ----- JSON-LD completo e válido de uma matéria (baseado no doc) -----

def _news_article(**overrides):
    article = {
        "@type": "NewsArticle",
        "headline": "Abel Ferreira lidera ranking mundial de vitórias",
        "description": "Abel Ferreira completa seis anos no Palmeiras.",
        "datePublished": "2026-11-04T11:58:00-03:00",
        "dateModified": "2026-11-04T12:02:53-03:00",
        "articleSection": "Palmeiras",
        "isAccessibleForFree": "http://schema.org/True",
        "author": {"@type": "Person", "name": "Rafaela Cardoso"},
        "publisher": {
            "@type": "Organization",
            "@id": "https://www.example.com/#organization",
            "name": "ACME!",
        },
        "hasPart": {"@type": "WebPageElement", "cssSelector": ".paywall-content"},
        "mainEntityOfPage": {
            "@type": "WebPage",
            "@id": "https://www.example.com/palmeiras/abel.html",
        },
        "image": [
            {"@type": "ImageObject", "width": 1280, "height": 720},
            {"@type": "ImageObject", "width": 1200, "height": 1200},
            {"@type": "ImageObject", "width": 960, "height": 1200},
        ],
    }
    article.update(overrides)
    return article


def _article_graph(organization_id="https://www.example.com/#organization", **article_overrides):
    return [
        _news_article(**article_overrides),
        {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "Organization",
                    "@id": organization_id,
                    "name": "ACME!",
                    "logo": {"@type": "ImageObject", "url": "x"},
                },
                {"@type": "WebSite", "@id": "https://www.example.com/#website"},
                {
                    "@type": "WebPage",
                    "@id": "https://www.example.com/palmeiras/abel.html#webpage",
                    "inLanguage": "pt-BR",
                },
                {
                    "@type": "BreadcrumbList",
                    "itemListElement": [{"@type": "ListItem", "position": 1}],
                },
            ],
        },
    ]


def _run(handler, entries, rules):
    return asyncio.run(
        check_schema(entries, rules=rules, transport=httpx.MockTransport(handler))
    )


# ----- flatten_graph -----

def test_flatten_opens_graph_and_keeps_top_level_nodes():
    blocks = [
        {"@type": "NewsArticle", "headline": "x"},
        {"@graph": [{"@type": "Organization"}, {"@type": "WebSite"}]},
    ]
    nodes = flatten_graph(blocks)
    types = [n["@type"] for n in nodes]
    assert types == ["NewsArticle", "Organization", "WebSite"]


# ----- carregamento das regras externas -----

def test_rules_load_from_external_json_files():
    rules = load_rules(RULES_DIR)
    assert rules.for_page_type("article") is not None
    assert rules.for_page_type("home") is not None
    assert "NewsArticle" in rules.for_page_type("article").types


# ----- matéria válida -----

def test_valid_article_has_no_errors():
    rules = load_rules(RULES_DIR)
    result = analyze_schema(
        _html(_article_graph()),
        url="https://www.example.com/palmeiras/abel.html",
        page_type="article",
        origins=(),
        rules=rules,
    )
    assert result.error_message is None
    assert result.schema_ok is True
    assert "NewsArticle" in result.detected_types


# ----- achado conhecido: Organizations duplicadas (@id divergente) -----

def test_duplicate_organizations_are_warning_not_error():
    rules = load_rules(RULES_DIR)
    # Grafo com a barra; publisher sem a barra -> duas Organizations distintas.
    # Isso é observação (warning), não reprova a URL.
    blocks = _article_graph(organization_id="https://www.example.com#organization")
    result = analyze_schema(
        _html(blocks),
        url="https://www.example.com/palmeiras/abel.html",
        page_type="article",
        origins=(),
        rules=rules,
    )
    dup = [f for f in result.findings if f.rule == "organizations_duplicadas"]
    assert dup and dup[0].severity == "warning"
    # Só observação -> a URL continua OK.
    assert result.schema_ok is True
    assert result.warnings
    assert dup[0] in result.warnings


# ----- dateModified anterior a datePublished -----

def test_date_modified_before_published_is_error():
    rules = load_rules(RULES_DIR)
    blocks = _article_graph(dateModified="2026-11-03T10:00:00-03:00")
    result = analyze_schema(
        _html(blocks),
        url="https://www.example.com/palmeiras/abel.html",
        page_type="article",
        origins=(),
        rules=rules,
    )
    assert any(f.rule == "date_not_before" for f in result.findings)
    assert result.schema_ok is False


# ----- imagem faltando dimensão exigida -----

def test_missing_required_image_dimension_is_error():
    rules = load_rules(RULES_DIR)
    blocks = _article_graph(
        image=[{"@type": "ImageObject", "width": 1280, "height": 720}]
    )
    result = analyze_schema(
        _html(blocks),
        url="https://www.example.com/palmeiras/abel.html",
        page_type="article",
        origins=(),
        rules=rules,
    )
    missing = [f for f in result.findings if f.rule == "image_dimension_missing"]
    assert {f.expected for f in missing} == {"1200x1200", "960x1200"}


# ----- campo obrigatório ausente -----

def test_missing_required_field_is_error():
    rules = load_rules(RULES_DIR)
    article = _news_article()
    del article["headline"]
    blocks = [article] + _article_graph()[1:]
    result = analyze_schema(
        _html(blocks),
        url="https://www.example.com/palmeiras/abel.html",
        page_type="article",
        origins=(),
        rules=rules,
    )
    assert any(
        f.rule == "required_field_missing" and f.field_path == "headline"
        for f in result.findings
    )


# ----- dois NewsArticle (contagem errada) -----

def test_two_news_articles_violate_max_count():
    rules = load_rules(RULES_DIR)
    blocks = [_news_article(), _news_article()] + _article_graph()[1:]
    result = analyze_schema(
        _html(blocks),
        url="https://www.example.com/palmeiras/abel.html",
        page_type="article",
        origins=(),
        rules=rules,
    )
    assert any(
        f.schema_type == "NewsArticle" and f.rule == "max_count"
        for f in result.findings
    )


# ----- ACME! vs Acme! -> aviso -----

def test_organization_name_inconsistency_is_warning():
    rules = load_rules(RULES_DIR)
    blocks = [
        _news_article(),
        {
            "@graph": [
                {
                    "@type": "Organization",
                    "@id": "https://www.example.com/#organization",
                    "name": "ACME!",
                    "logo": {"@type": "ImageObject", "url": "x"},
                },
                {
                    "@type": "Organization",
                    "@id": "https://www.example.com/#org2",
                    "name": "Acme!",
                    "logo": {"@type": "ImageObject", "url": "x"},
                },
                {"@type": "WebSite"},
                {"@type": "WebPage", "inLanguage": "pt-BR"},
                {"@type": "BreadcrumbList", "itemListElement": []},
            ]
        },
    ]
    result = analyze_schema(
        _html(blocks),
        url="https://www.example.com/palmeiras/abel.html",
        page_type="article",
        origins=(),
        rules=rules,
    )
    name_findings = [
        f for f in result.findings if f.rule == "organization_name_consistent"
    ]
    assert name_findings and name_findings[0].severity == "warning"
    # Apenas aviso -> continua OK
    assert result.schema_ok is True


# ----- home principal -----

def test_home_requires_organization_website_webpage():
    rules = load_rules(RULES_DIR)
    blocks = [
        {
            "@graph": [
                {
                    "@type": "WebPage",
                    "inLanguage": "pt-BR",
                    "url": "https://www.example.com/",
                },
                {
                    "@type": "WebSite",
                    "url": "https://www.example.com/",
                    "potentialAction": {"@type": "SearchAction"},
                },
                {
                    "@type": "Organization",
                    "name": "ACME!",
                    "url": "https://www.example.com/",
                    "sameAs": ["https://x", "https://y"],
                    "logo": {"@type": "ImageObject"},
                },
            ]
        }
    ]
    result = analyze_schema(
        _html(blocks),
        url="https://www.example.com/",
        page_type="home",
        origins=(),
        rules=rules,
    )
    assert result.schema_ok is True


def test_home_missing_searchaction_is_error():
    rules = load_rules(RULES_DIR)
    blocks = [
        {
            "@graph": [
                {"@type": "WebPage", "inLanguage": "pt-BR", "url": "https://x/"},
                {"@type": "WebSite", "url": "https://x/"},
                {
                    "@type": "Organization",
                    "name": "ACME!",
                    "url": "https://x/",
                    "sameAs": ["https://x"],
                    "logo": {"@type": "ImageObject"},
                },
            ]
        }
    ]
    result = analyze_schema(
        _html(blocks),
        url="https://www.example.com/",
        page_type="home",
        origins=(),
        rules=rules,
    )
    assert any(
        f.schema_type == "WebSite" and f.field_path == "potentialAction"
        for f in result.findings
    )
    assert result.schema_ok is False


# ----- página sem regra para o tipo: só registra tipos detectados -----

def test_page_without_rules_only_detects_types():
    rules = load_rules(RULES_DIR)
    # 'web_story' não tem arquivo de regras
    result = analyze_schema(
        _html([{"@type": "AMPStory", "name": "x"}]),
        url="https://www.example.com/stories/alguma",
        page_type="web_story",
        origins=(),
        rules=rules,
    )
    assert result.schema_ok is True
    assert result.findings == ()
    assert "AMPStory" in result.detected_types


# ----- fluxo assíncrono + isolamento de falhas -----

def test_check_schema_isolates_http_and_network_failures():
    rules = load_rules(RULES_DIR)

    def handler(request):
        if request.url.path.endswith("/erro"):
            return httpx.Response(500)
        if request.url.path.endswith("/timeout"):
            raise httpx.ReadTimeout("simulated", request=request)
        return httpx.Response(200, html=_html(_article_graph()))

    entries = [
        _entry("https://www.example.com/ok", "article"),
        _entry("https://www.example.com/erro", "article"),
        _entry("https://www.example.com/timeout", "article"),
    ]
    results = _run(handler, entries, rules)
    assert results[0].schema_ok is True
    assert results[1].error_message == "HTTP status 500"
    assert results[2].error_message.startswith("Timeout")


# ----- tabela -----


# ----- galeria: achados de @type/@id/@context cru -----


# ----- vídeo -----

def _video_blocks(webpage_url="https://www.example.com/videos/meu-video", **video_overrides):
    video = {
        "@type": "VideoObject",
        "name": "Meu vídeo",
        "description": "desc",
        "thumbnailUrl": "https://img/thumb.jpg",
        "uploadDate": "2026-11-01T10:00:00-03:00",
        "duration": "PT2M30S",
        "contentUrl": "https://video/stream.mp4",
    }
    video.update(video_overrides)
    return [
        video,
        {"@type": "BreadcrumbList", "itemListElement": [{"@type": "ListItem"}]},
        {
            "@graph": [
                {"@type": "Organization", "name": "ACME!"},
                {"@type": "WebSite"},
                {"@type": "WebPage", "inLanguage": "pt-BR", "url": webpage_url},
            ]
        },
    ]


def test_video_valid():
    rules = load_rules(RULES_DIR)
    result = analyze_schema(
        _html(_video_blocks()),
        url="https://www.example.com/videos/meu-video",
        page_type="video",
        origins=(),
        rules=rules,
    )
    assert result.schema_ok is True


def test_video_without_content_or_embed_url_is_error():
    rules = load_rules(RULES_DIR)
    blocks = _video_blocks()
    del blocks[0]["contentUrl"]
    result = analyze_schema(
        _html(blocks),
        url="https://www.example.com/videos/meu-video",
        page_type="video",
        origins=(),
        rules=rules,
    )
    assert any(f.rule == "required_one_of" for f in result.findings)


def test_video_webpage_pointing_to_home_is_error():
    rules = load_rules(RULES_DIR)
    # WebPage do grafo aponta para a home, não para a URL do vídeo (achado spec)
    blocks = _video_blocks(webpage_url="https://www.example.com/")
    result = analyze_schema(
        _html(blocks),
        url="https://www.example.com/videos/meu-video",
        page_type="video",
        origins=(),
        rules=rules,
    )
    assert any(f.rule == "webpage_url_matches_video" for f in result.findings)
    assert result.schema_ok is False


# ----- rota avulsa -----

def test_other_route_requires_generic_graph_and_breadcrumb():
    rules = load_rules(RULES_DIR)
    blocks = [
        {
            "@graph": [
                {"@type": "Organization", "name": "ACME!"},
                {"@type": "WebSite"},
                {"@type": "WebPage", "inLanguage": "pt-BR"},
                {"@type": "BreadcrumbList", "itemListElement": [{"@type": "ListItem"}]},
            ]
        }
    ]
    result = analyze_schema(
        _html(blocks),
        url="https://www.example.com/alguma-rota",
        page_type="other",
        origins=(),
        rules=rules,
    )
    assert result.schema_ok is True


def test_other_route_missing_breadcrumb_is_error():
    rules = load_rules(RULES_DIR)
    blocks = [
        {
            "@graph": [
                {"@type": "Organization", "name": "ACME!"},
                {"@type": "WebSite"},
                {"@type": "WebPage", "inLanguage": "pt-BR"},
            ]
        }
    ]
    result = analyze_schema(
        _html(blocks),
        url="https://www.example.com/alguma-rota",
        page_type="other",
        origins=(),
        rules=rules,
    )
    assert any(
        f.schema_type == "BreadcrumbList" and f.rule == "min_count"
        for f in result.findings
    )


# ----- perfil e tempo real -----


@pytest.mark.parametrize(("concurrency", "timeout_seconds"), [(0, 5), (1, 0)])
def test_invalid_configuration_fails_before_requests(concurrency, timeout_seconds):
    rules = load_rules(RULES_DIR)
    with pytest.raises(ValueError):
        asyncio.run(
            check_schema(
                [_entry("https://www.example.com/", "home")],
                rules=rules,
                concurrency=concurrency,
                timeout_seconds=timeout_seconds,
                transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            )
        )
