from auditor.checks.inventory import collect_inventory_alerts
from auditor.sitemap import SitemapEntry, SitemapInventory


def _entry(url, *, occurrences=1, origins=("https://www.example.com/sitemap-pages.xml",)):
    return SitemapEntry(
        url=url, lastmod=None, origins=origins, occurrences=occurrences,
        page_type="other",
    )


def _inventory(entries):
    return SitemapInventory(
        entries=tuple(entries), articles=(), galleries=(), videos=(),
        main_routes=(), duplicates=(), accessed_sitemaps=(),
        url_counts_by_sitemap=(),
    )


def test_duplicate_url_alert():
    inv = _inventory([_entry("https://www.example.com/x", occurrences=2)])
    alerts = collect_inventory_alerts(inv)
    assert any(a.kind == "duplicate_url" for a in alerts)


def test_fragment_alert():
    inv = _inventory([_entry("https://www.example.com/schedule#2026-10-26")])
    alerts = collect_inventory_alerts(inv)
    assert any(a.kind == "url_with_fragment" for a in alerts)


def test_host_without_www_alert():
    inv = _inventory([_entry("https://example.com/sem-www")])
    # Desligado por padrão: o alerta é opcional.
    assert collect_inventory_alerts(inv) == ()
    alerts = collect_inventory_alerts(inv, require_www=True)
    assert any(a.kind == "host_without_www" for a in alerts)


def test_legacy_slug_alert():
    inv = _inventory([_entry("https://www.example.com/oldsite/algo")])
    # Sem slugs configurados, nada é acusado.
    assert collect_inventory_alerts(inv) == ()
    alerts = collect_inventory_alerts(inv, legacy_slugs=("OldSite",))
    assert any(a.kind == "legacy_slug" for a in alerts)


def test_clean_url_has_no_alerts():
    inv = _inventory([_entry("https://www.example.com/noticia.html")])
    alerts = collect_inventory_alerts(inv)
    assert alerts == ()


def test_alert_preserves_origins():
    inv = _inventory([
        _entry(
            "https://www.example.com/x",
            occurrences=2,
            origins=("https://www.example.com/sitemap-pages.xml",
                     "https://www.example.com/sitemap-articles.xml"),
        )
    ])
    alerts = collect_inventory_alerts(inv)
    dup = next(a for a in alerts if a.kind == "duplicate_url")
    assert len(dup.origins) == 2
