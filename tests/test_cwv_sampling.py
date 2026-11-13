from datetime import datetime, timezone

from auditor.main import _cwv_sample_by_category, _category_of
from auditor.sitemap import SitemapEntry, SitemapInventory


def _entry(url, lastmod=None, page_type="other"):
    return SitemapEntry(
        url=url, lastmod=lastmod, origins=(), occurrences=1, page_type=page_type
    )


def test_category_of_uses_first_path_segment():
    assert _category_of("https://www.example.com/news/abel.html") == "news"
    assert _category_of("https://www.example.com/world-news/x.html") == "world-news"
    assert _category_of("https://www.example.com/") == ""


def test_sample_is_homes_plus_one_article_per_category():
    def dt(day):
        return datetime(2026, 11, day, tzinfo=timezone.utc)

    # Duas categorias com várias matérias; a mais recente de cada deve entrar.
    articles = (
        _entry("https://x/news/nova.html", dt(5), "article"),
        _entry("https://x/news/velha.html", dt(1), "article"),
        _entry("https://x/sports/unica.html", dt(3), "article"),
    )
    homes = (
        _entry("https://x/news", page_type="other"),
        _entry("https://x/tables/league", page_type="table"),
    )
    inv = SitemapInventory(
        entries=articles + homes, articles=articles, galleries=(), videos=(),
        main_routes=homes, duplicates=(), accessed_sitemaps=(),
        url_counts_by_sitemap=(),
    )
    sample = {e.url for e in _cwv_sample_by_category(inv)}

    # Todas as rotas de homes entram.
    assert "https://x/news" in sample
    assert "https://x/tables/league" in sample
    # Uma matéria por categoria: a mais recente de news, e a de sports.
    assert "https://x/news/nova.html" in sample
    assert "https://x/news/velha.html" not in sample
    assert "https://x/sports/unica.html" in sample


def test_sample_dedupes_urls_present_in_both():
    shared = _entry("https://x/news", page_type="other")
    inv = SitemapInventory(
        entries=(shared,), articles=(), galleries=(), videos=(),
        main_routes=(shared,), duplicates=(), accessed_sitemaps=(),
        url_counts_by_sitemap=(),
    )
    sample = _cwv_sample_by_category(inv)
    assert len(sample) == 1
