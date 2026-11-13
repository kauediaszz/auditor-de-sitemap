"""Leitura e processamento de sitemaps."""

from auditor.sitemap.reader import (
    SitemapEntry,
    SitemapFetchError,
    SitemapHTTPError,
    SitemapInventory,
    SitemapReadError,
    SitemapSourceError,
    SitemapXMLFormatError,
    classify_page_type,
    read_sitemaps,
)

__all__ = [
    "SitemapEntry",
    "SitemapFetchError",
    "SitemapHTTPError",
    "SitemapInventory",
    "SitemapReadError",
    "SitemapSourceError",
    "SitemapXMLFormatError",
    "classify_page_type",
    "read_sitemaps",
]
