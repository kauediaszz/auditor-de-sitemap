import asyncio
from datetime import date, timedelta
from xml.etree import ElementTree as ET

import httpx
import pytest

from auditor.sitemap import (
    SitemapFetchError,
    SitemapHTTPError,
    SitemapXMLFormatError,
    classify_page_type,
    read_sitemaps,
)

BASE = "https://example.test"
MASTER = f"{BASE}/sitemap/sitemap-index.xml"
ARTICLES = f"{BASE}/sitemap-articles.xml"
HOMES = f"{BASE}/sitemap-pages.xml"
GALLERIES = f"{BASE}/sitemap-galleries.xml"
VIDEOS = f"{BASE}/sitemap-videos.xml"
NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


def _xml_document(kind: str, entries: list[tuple[str, str | None]], namespaced=True):
    namespace = f"{{{NS}}}" if namespaced else ""
    root = ET.Element(f"{namespace}{kind}")
    entry_tag = "sitemap" if kind == "sitemapindex" else "url"
    for location, lastmod in entries:
        entry = ET.SubElement(root, f"{namespace}{entry_tag}")
        ET.SubElement(entry, f"{namespace}loc").text = location
        if lastmod is not None:
            ET.SubElement(entry, f"{namespace}lastmod").text = lastmod
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _run(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return asyncio.run(
        read_sitemaps(
            MASTER,
            transport=transport,
            **kwargs,
        )
    )


def test_reads_namespaced_indexes_deduplicates_and_selects_limits():
    duplicate_url = f"{BASE}/story?topic=football&mode=full#main"
    gallery_posts = f"{BASE}/galleries/post-sitemap.xml"
    video_current = f"{BASE}/sitemap/videos/articles-current.xml"
    archive = f"{BASE}/sitemap/2026/09/sitemap-articles-2026-09.xml"

    documents = {
        MASTER: _xml_document(
            "sitemapindex",
            [
                (ARTICLES, None),
                (HOMES, None),
                (GALLERIES, None),
                (VIDEOS, None),
                (archive, None),
            ],
        ),
        ARTICLES: _xml_document(
            "urlset",
            [
                (f"{BASE}/article-old", "2026-10-24T10:00:00-03:00"),
                (duplicate_url, "2026-10-26T10:00:00-03:00"),
                (f"{BASE}/article-newest", "2026-10-27T10:00:00-03:00"),
                (f"{BASE}/article-middle", "2026-10-25T10:00:00-03:00"),
            ],
        ),
        HOMES: _xml_document(
            "urlset",
            [
                (duplicate_url, "2026-10-23T10:00:00-03:00"),
                (f"{BASE}/", "2026-10-27T10:00:00-03:00"),
                (f"{BASE}/tables/league", None),
            ],
        ),
        GALLERIES: _xml_document(
            "sitemapindex",
            [(gallery_posts, None)],
        ),
        gallery_posts: _xml_document(
            "urlset",
            [
                (f"{BASE}/galleries/album-old", "2026-10-24T10:00:00-03:00"),
                (f"{BASE}/galleries/album-new", "2026-10-27T10:00:00-03:00"),
                (f"{BASE}/galleries/album-middle", "2026-10-26T10:00:00-03:00"),
            ],
        ),
        VIDEOS: _xml_document(
            "sitemapindex",
            [(video_current, None)],
        ),
        video_current: _xml_document(
            "urlset",
            [
                (f"{BASE}/video-old", "2026-10-24T10:00:00-03:00"),
                (f"{BASE}/video-new", "2026-10-27T10:00:00-03:00"),
                (f"{BASE}/video-middle", "2026-10-26T10:00:00-03:00"),
            ],
        ),
    }
    requested = []

    def handler(request):
        requested.append(str(request.url))
        assert request.headers["user-agent"].startswith("SitemapAuditor/")
        body = documents.get(str(request.url))
        return httpx.Response(200, content=body) if body else httpx.Response(404)

    result = _run(
        handler,
        articles_limit=3,
        galleries_limit=2,
        videos_limit=2,
        page_type_rules=(("/tables", "table"),),
        concurrency=3,
    )

    assert [entry.url for entry in result.articles] == [
        f"{BASE}/article-newest",
        duplicate_url,
        f"{BASE}/article-middle",
    ]
    assert [entry.url for entry in result.galleries] == [
        f"{BASE}/galleries/album-new",
        f"{BASE}/galleries/album-middle",
    ]
    assert [entry.url for entry in result.videos] == [
        f"{BASE}/video-new",
        f"{BASE}/video-middle",
    ]
    assert len(result.main_routes) == 3
    assert len(result.duplicates) == 1
    duplicate = result.duplicates[0]
    assert duplicate.url == duplicate_url
    assert duplicate.origins == (ARTICLES, HOMES)
    assert duplicate.occurrences == 2
    assert duplicate.is_duplicate
    assert duplicate.url.endswith("?topic=football&mode=full#main")
    assert {entry.url: entry.page_type for entry in result.main_routes} == {
        duplicate_url: "article",
        f"{BASE}/": "home",
        f"{BASE}/tables/league": "table",
    }
    assert gallery_posts in result.accessed_sitemaps
    assert archive not in requested


def test_missing_lastmod_is_retained_after_dated_urls_in_discovery_order():
    first_undated = f"{BASE}/article-undated-first"
    second_undated = f"{BASE}/article-undated-second"
    gallery_child = f"{BASE}/galleries/post-sitemap.xml"
    video_child = f"{BASE}/sitemap/videos/articles-current.xml"
    documents = {
        MASTER: _xml_document(
            "sitemapindex",
            [(ARTICLES, None), (HOMES, None), (GALLERIES, None), (VIDEOS, None)],
        ),
        ARTICLES: _xml_document(
            "urlset",
            [
                (first_undated, None),
                (f"{BASE}/article-dated-old", "2026-10-24"),
                (second_undated, None),
                (f"{BASE}/article-dated-new", "2026-10-27"),
            ],
        ),
        HOMES: _xml_document("urlset", [(f"{BASE}/", None)]),
        GALLERIES: _xml_document("sitemapindex", [(gallery_child, None)]),
        gallery_child: _xml_document("urlset", [(f"{BASE}/galleries/one", None)]),
        VIDEOS: _xml_document("sitemapindex", [(video_child, None)]),
        video_child: _xml_document("urlset", [(f"{BASE}/one-video", None)]),
    }

    result = _run(
        lambda request: httpx.Response(
            200, content=documents[str(request.url)]
        ),
        articles_limit=3,
    )

    assert [entry.url for entry in result.articles] == [
        f"{BASE}/article-dated-new",
        f"{BASE}/article-dated-old",
        first_undated,
    ]
    assert result.articles[-1].lastmod is None


def test_default_selection_limits_with_simulated_sitemaps():
    gallery_child = f"{BASE}/galleries/post-sitemap.xml"
    video_child = f"{BASE}/sitemap/videos/articles-current.xml"

    def lastmod(index):
        return (date(2026, 1, 1) + timedelta(days=index)).isoformat()

    documents = {
        MASTER: _xml_document(
            "sitemapindex",
            [(ARTICLES, None), (HOMES, None), (GALLERIES, None), (VIDEOS, None)],
        ),
        ARTICLES: _xml_document(
            "urlset",
            [
                (f"{BASE}/article-{index}", lastmod(index))
                for index in range(100)
            ],
        ),
        HOMES: _xml_document("urlset", [(f"{BASE}/", "2026-01-01")]),
        GALLERIES: _xml_document("sitemapindex", [(gallery_child, None)]),
        gallery_child: _xml_document(
            "urlset",
            [
                (f"{BASE}/galleries/album-{index}", lastmod(index))
                for index in range(25)
            ],
        ),
        VIDEOS: _xml_document("sitemapindex", [(video_child, None)]),
        video_child: _xml_document(
            "urlset",
            [
                (f"{BASE}/video-{index}", lastmod(index))
                for index in range(25)
            ],
        ),
    }

    result = _run(
        lambda request: httpx.Response(200, content=documents[str(request.url)])
    )

    assert len(result.articles) == 80
    assert result.articles[0].url == f"{BASE}/article-99"
    assert result.articles[-1].url == f"{BASE}/article-20"
    assert len(result.galleries) == 20
    assert result.galleries[0].url == f"{BASE}/galleries/album-24"
    assert result.galleries[-1].url == f"{BASE}/galleries/album-5"
    assert len(result.videos) == 20
    assert result.videos[0].url == f"{BASE}/video-24"
    assert result.videos[-1].url == f"{BASE}/video-5"


def test_none_limits_select_all_urls_of_each_type():
    """Com limite None (modo --all) nenhuma URL é cortada da amostra."""
    gallery_child = f"{BASE}/galleries/post-sitemap.xml"
    video_child = f"{BASE}/sitemap/videos/articles-current.xml"

    def lastmod(index):
        return (date(2026, 1, 1) + timedelta(days=index)).isoformat()

    documents = {
        MASTER: _xml_document(
            "sitemapindex",
            [(ARTICLES, None), (HOMES, None), (GALLERIES, None), (VIDEOS, None)],
        ),
        ARTICLES: _xml_document(
            "urlset",
            [(f"{BASE}/article-{index}", lastmod(index)) for index in range(100)],
        ),
        HOMES: _xml_document("urlset", [(f"{BASE}/", "2026-01-01")]),
        GALLERIES: _xml_document("sitemapindex", [(gallery_child, None)]),
        gallery_child: _xml_document(
            "urlset",
            [(f"{BASE}/galleries/album-{index}", lastmod(index)) for index in range(25)],
        ),
        VIDEOS: _xml_document("sitemapindex", [(video_child, None)]),
        video_child: _xml_document(
            "urlset",
            [(f"{BASE}/video-{index}", lastmod(index)) for index in range(25)],
        ),
    }

    result = _run(
        lambda request: httpx.Response(200, content=documents[str(request.url)]),
        articles_limit=None,
        galleries_limit=None,
        videos_limit=None,
    )

    # Todas as URLs de cada tipo entram, sem o corte da amostragem.
    assert len(result.articles) == 100
    assert len(result.galleries) == 25
    assert len(result.videos) == 25


def test_invalid_xml_is_reported_as_format_error():
    docs = {
        MASTER: _xml_document(
            "sitemapindex",
            [(ARTICLES, None), (HOMES, None), (GALLERIES, None), (VIDEOS, None)],
        ),
        ARTICLES: b"<urlset><url>",
        HOMES: _xml_document("urlset", []),
        GALLERIES: _xml_document("sitemapindex", []),
        VIDEOS: _xml_document("sitemapindex", []),
    }
    with pytest.raises(SitemapXMLFormatError, match="Invalid XML"):
        _run(lambda request: httpx.Response(200, content=docs[str(request.url)]))


def test_non_success_http_status_is_reported():
    with pytest.raises(SitemapHTTPError) as error:
        _run(lambda request: httpx.Response(503))
    assert error.value.status_code == 503
    assert error.value.sitemap_url == MASTER


@pytest.mark.parametrize(
    ("exception_type", "message"),
    [
        (httpx.ConnectError, "Connection"),
        (httpx.ReadTimeout, "Timeout"),
    ],
)
def test_connection_and_timeout_failures_are_reported(exception_type, message):
    def handler(request):
        raise exception_type("simulated failure", request=request)

    with pytest.raises(SitemapFetchError, match=message):
        _run(handler)


def test_page_classification_uses_rules_then_source_role():
    rules = (("/tables", "table"), ("/people/", "profile"))
    assert classify_page_type(f"{BASE}/", "homes", rules) == "home"
    assert classify_page_type(f"{BASE}/tables/league", "homes", rules) == "table"
    assert classify_page_type(f"{BASE}/tables", "homes", rules) == "table"
    assert classify_page_type(f"{BASE}/people/jane", "homes", rules) == "profile"
    # Prefixo só casa em fronteira de segmento.
    assert classify_page_type(f"{BASE}/tablesaw", "homes", rules) == "other"
    # Sem regra que case, vale o papel do sitemap de origem.
    assert classify_page_type(f"{BASE}/x", "articles", rules) == "article"
    assert classify_page_type(f"{BASE}/x", "videos", rules) == "video"
    assert classify_page_type(f"{BASE}/x", "galleries", rules) == "gallery"
    assert classify_page_type(f"{BASE}/x", "homes", rules) == "other"
    # Sem regras, só raiz e papel.
    assert classify_page_type(f"{BASE}/tables/league", "homes") == "other"


def test_exclude_urls_removes_audited_before_limit_and_keeps_routes():
    """A exclusão acontece antes do corte: o limite pega as próximas URLs."""
    gallery_child = f"{BASE}/galleries/post-sitemap.xml"
    video_child = f"{BASE}/sitemap/videos/articles-current.xml"

    def lastmod(index):
        return (date(2026, 1, 1) + timedelta(days=index)).isoformat()

    documents = {
        MASTER: _xml_document(
            "sitemapindex",
            [(ARTICLES, None), (HOMES, None), (GALLERIES, None), (VIDEOS, None)],
        ),
        ARTICLES: _xml_document(
            "urlset",
            [(f"{BASE}/article-{index}", lastmod(index)) for index in range(10)],
        ),
        HOMES: _xml_document(
            "urlset", [(f"{BASE}/", "2026-01-01"), (f"{BASE}/news", None)]
        ),
        GALLERIES: _xml_document("sitemapindex", [(gallery_child, None)]),
        gallery_child: _xml_document(
            "urlset",
            [(f"{BASE}/galleries/album-{index}", lastmod(index)) for index in range(5)],
        ),
        VIDEOS: _xml_document("sitemapindex", [(video_child, None)]),
        video_child: _xml_document(
            "urlset",
            [(f"{BASE}/video-{index}", lastmod(index)) for index in range(5)],
        ),
    }

    def handler(request):
        return httpx.Response(200, content=documents[str(request.url)])

    first = _run(handler, articles_limit=3, galleries_limit=2, videos_limit=2)
    audited = {
        entry.url for entry in (*first.articles, *first.galleries, *first.videos)
    }
    # Inclui uma rota de homes na exclusão: ela deve continuar sendo auditada.
    second = _run(
        handler,
        articles_limit=3,
        galleries_limit=2,
        videos_limit=2,
        exclude_urls=frozenset(audited | {f"{BASE}/"}),
    )

    second_urls = {
        entry.url for entry in (*second.articles, *second.galleries, *second.videos)
    }
    assert audited.isdisjoint(second_urls)
    assert len(second.articles) == 3
    assert len(second.galleries) == 2
    assert len(second.videos) == 2
    # As próximas mais recentes: 9,8,7 foram as primeiras; agora 6,5,4.
    assert [e.url for e in second.articles] == [
        f"{BASE}/article-6",
        f"{BASE}/article-5",
        f"{BASE}/article-4",
    ]
    assert {e.url for e in second.main_routes} == {f"{BASE}/", f"{BASE}/news"}


def test_exclude_urls_returns_fewer_when_not_enough_new_urls():
    gallery_child = f"{BASE}/galleries/post-sitemap.xml"
    video_child = f"{BASE}/sitemap/videos/articles-current.xml"
    documents = {
        MASTER: _xml_document(
            "sitemapindex",
            [(ARTICLES, None), (HOMES, None), (GALLERIES, None), (VIDEOS, None)],
        ),
        ARTICLES: _xml_document(
            "urlset",
            [(f"{BASE}/a-{i}", f"2026-01-0{i + 1}") for i in range(4)],
        ),
        HOMES: _xml_document("urlset", [(f"{BASE}/", "2026-01-01")]),
        GALLERIES: _xml_document("sitemapindex", [(gallery_child, None)]),
        gallery_child: _xml_document("urlset", []),
        VIDEOS: _xml_document("sitemapindex", [(video_child, None)]),
        video_child: _xml_document("urlset", []),
    }
    result = _run(
        lambda request: httpx.Response(200, content=documents[str(request.url)]),
        articles_limit=10,
        galleries_limit=0,
        videos_limit=0,
        exclude_urls=frozenset({f"{BASE}/a-0", f"{BASE}/a-1"}),
    )
    assert len(result.articles) == 2


def _small_documents(index_entries):
    return {
        MASTER: _xml_document("sitemapindex", index_entries),
        ARTICLES: _xml_document("urlset", [(f"{BASE}/a-1", "2026-01-02")]),
        HOMES: _xml_document("urlset", [(f"{BASE}/", None)]),
        f"{BASE}/custom/news.xml": _xml_document(
            "urlset", [(f"{BASE}/n-1", "2026-01-03")]
        ),
    }


def test_custom_source_paths_and_optional_roles():
    """Só os papéis configurados são lidos e exigidos (galerias/vídeos opcionais)."""
    documents = _small_documents(
        [(ARTICLES, None), (HOMES, None), (f"{BASE}/custom/news.xml", None)]
    )
    result = _run(
        lambda request: httpx.Response(200, content=documents[str(request.url)]),
        source_paths={
            "articles": ["/sitemap-articles.xml", "/custom/news.xml"],
            "homes": ["/sitemap-pages.xml"],
        },
    )
    assert {e.url for e in result.articles} == {f"{BASE}/a-1", f"{BASE}/n-1"}
    assert result.galleries == ()
    assert result.videos == ()
    assert [e.url for e in result.main_routes] == [f"{BASE}/"]


def test_configured_role_missing_from_index_is_an_error():
    from auditor.sitemap import SitemapSourceError

    documents = _small_documents([(ARTICLES, None)])  # sem sitemap de homes
    with pytest.raises(SitemapSourceError, match="homes"):
        _run(
            lambda request: httpx.Response(200, content=documents[str(request.url)]),
            source_paths={
                "articles": ["/sitemap-articles.xml"],
                "homes": ["/sitemap-pages.xml"],
            },
        )


def test_unknown_role_in_source_paths_is_rejected():
    with pytest.raises(ValueError, match="desconhecidos"):
        _run(
            lambda request: httpx.Response(200, content=b""),
            source_paths={"podcasts": ["/x.xml"]},
        )
