"""Fetch and select URLs from the live sitemap index."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

import httpx
from lxml import etree

SitemapRole = Literal["articles", "galleries", "videos", "homes"]
_USER_AGENT = "SitemapAuditor/0.1 (live sitemap inventory)"

# Sufixos de caminho (path) dos sitemaps filhos que o índice mestre deve expor,
# por papel. É só um padrão: configure o seu em ``sources.sitemaps`` do
# ``config.yaml``. Papéis ausentes do mapa não são lidos nem exigidos.
DEFAULT_SOURCE_PATHS: dict[str, tuple[str, ...]] = {
    "articles": ("/sitemap-articles.xml",),
    "galleries": ("/sitemap-galleries.xml",),
    "videos": ("/sitemap-videos.xml",),
    "homes": ("/sitemap-pages.xml",),
}
_ROLES = ("articles", "galleries", "videos", "homes")


class SitemapReadError(RuntimeError):
    """A sitemap could not be fetched or interpreted safely."""

    def __init__(
        self,
        message: str,
        sitemap_url: str,
        *,
        accessed_sitemaps: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"{message}: {sitemap_url}")
        self.sitemap_url = sitemap_url
        self.accessed_sitemaps = accessed_sitemaps


class SitemapSourceError(SitemapReadError):
    """The master index does not expose a required sitemap source."""


class SitemapFetchError(SitemapReadError):
    """A sitemap request failed."""


class SitemapHTTPError(SitemapFetchError):
    """A sitemap request returned an unsuccessful HTTP status."""

    def __init__(
        self,
        sitemap_url: str,
        status_code: int,
        *,
        accessed_sitemaps: tuple[str, ...] = (),
    ) -> None:
        super().__init__(
            f"HTTP {status_code} while fetching sitemap",
            sitemap_url,
            accessed_sitemaps=accessed_sitemaps,
        )
        self.status_code = status_code


class SitemapXMLFormatError(SitemapReadError):
    """A sitemap response is not a valid sitemap XML document."""


@dataclass(frozen=True)
class SitemapEntry:
    url: str
    lastmod: datetime | None
    origins: tuple[str, ...]
    occurrences: int
    page_type: str

    @property
    def is_duplicate(self) -> bool:
        return self.occurrences > 1


@dataclass(frozen=True)
class SitemapInventory:
    entries: tuple[SitemapEntry, ...]
    articles: tuple[SitemapEntry, ...]
    galleries: tuple[SitemapEntry, ...]
    videos: tuple[SitemapEntry, ...]
    main_routes: tuple[SitemapEntry, ...]
    duplicates: tuple[SitemapEntry, ...]
    accessed_sitemaps: tuple[str, ...]
    url_counts_by_sitemap: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class _URLRecord:
    url: str
    lastmod: datetime | None


@dataclass
class _Aggregate:
    lastmod: datetime | None
    origins: list[str]
    occurrences: int
    roles: set[SitemapRole]


class _FetchState:
    def __init__(self) -> None:
        self.accessed: list[str] = []
        self.url_counts: dict[str, int] = {}


def _local_name(element: etree._Element) -> str:
    return etree.QName(element).localname


def _parse_lastmod(value: str | None, sitemap_url: str) -> datetime | None:
    if value is None or not value.strip():
        return None

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        lastmod = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SitemapXMLFormatError(
            f"Invalid lastmod value {value!r} in sitemap",
            sitemap_url,
        ) from exc

    if lastmod.tzinfo is None:
        lastmod = lastmod.replace(tzinfo=timezone.utc)
    return lastmod


def _read_loc_and_lastmod(
    element: etree._Element, sitemap_url: str
) -> _URLRecord:
    loc_values = [
        (child.text or "").strip()
        for child in element
        if _local_name(child) == "loc"
    ]
    if len(loc_values) != 1 or not loc_values[0]:
        raise SitemapXMLFormatError(
            "Each sitemap entry must contain exactly one non-empty loc",
            sitemap_url,
        )

    lastmod_values = [
        (child.text or "").strip()
        for child in element
        if _local_name(child) == "lastmod"
    ]
    if len(lastmod_values) > 1:
        raise SitemapXMLFormatError(
            "A sitemap entry contains more than one lastmod",
            sitemap_url,
        )
    lastmod = _parse_lastmod(
        lastmod_values[0] if lastmod_values else None,
        sitemap_url,
    )
    return _URLRecord(url=loc_values[0], lastmod=lastmod)


def _parse_sitemap_xml(
    content: bytes, sitemap_url: str
) -> tuple[Literal["urlset", "sitemapindex"], list[_URLRecord]]:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        remove_comments=True,
    )
    try:
        root = etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SitemapXMLFormatError(
            f"Invalid XML ({exc})",
            sitemap_url,
        ) from exc

    root_name = _local_name(root)
    if root_name not in {"urlset", "sitemapindex"}:
        raise SitemapXMLFormatError(
            f"Unexpected XML root element {root_name!r}",
            sitemap_url,
        )

    expected_entry = "url" if root_name == "urlset" else "sitemap"
    records = [
        _read_loc_and_lastmod(element, sitemap_url)
        for element in root
        if _local_name(element) == expected_entry
    ]
    if root_name == "urlset":
        return "urlset", records
    return "sitemapindex", records


def _role_for_sitemap_url(
    sitemap_url: str, source_paths: Mapping[str, Sequence[str]]
) -> SitemapRole | None:
    """Papel do sitemap filho, pelo sufixo de caminho configurado."""
    path = urlsplit(sitemap_url).path.lower().rstrip("/")
    for role, suffixes in source_paths.items():
        for suffix in suffixes:
            if path.endswith(suffix.lower().rstrip("/")):
                return role  # type: ignore[return-value]
    return None


def _same_site_sitemap_url(sitemap_url: str, expected_host: str) -> bool:
    parsed = urlsplit(sitemap_url)
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() == expected_host.lower()
        and not parsed.username
        and not parsed.password
    )


def classify_page_type(
    url: str,
    source_role: SitemapRole,
    page_type_rules: Sequence[tuple[str, str]] = (),
) -> str:
    """Classifica uma URL sem alterar query string ou fragmento.

    Ordem: raiz do site = ``home``; depois as regras de prefixo de caminho
    (``page_type_rules``, na ordem em que foram dadas); por fim o papel do
    sitemap de origem (articles -> article, videos -> video, galleries ->
    gallery). O que não casar vira ``other``.
    """
    path = urlsplit(url).path.lower().rstrip("/")

    if path in {"", "/"}:
        return "home"
    for prefix, page_type in page_type_rules:
        normalized = prefix.lower().rstrip("/")
        if path == normalized or path.startswith(normalized + "/"):
            return page_type
    if source_role == "articles":
        return "article"
    if source_role == "videos":
        return "video"
    if source_role == "galleries":
        return "gallery"
    return "other"


def _sort_newest(
    entries: list[SitemapEntry], limit: int | None
) -> tuple[SitemapEntry, ...]:
    dated_first = sorted(
        entries,
        key=lambda entry: (
            entry.lastmod is not None,
            entry.lastmod or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    # limit=None significa "sem corte" (auditar todas as URLs do tipo).
    if limit is None:
        return tuple(dated_first)
    return tuple(dated_first[:limit])


async def read_sitemaps(
    sitemap_index_url: str,
    *,
    articles_limit: int | None = 80,
    galleries_limit: int | None = 20,
    videos_limit: int | None = 20,
    exclude_urls: frozenset[str] | set[str] = frozenset(),
    source_paths: Mapping[str, Sequence[str]] | None = None,
    page_type_rules: Sequence[tuple[str, str]] = (),
    concurrency: int = 10,
    timeout_seconds: float = 30,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SitemapInventory:
    """Read the live master index and select the newest sitemap entries.

    URLs without ``lastmod`` are retained, ordered after dated URLs, and used
    to fill a sample only when fewer dated URLs than the requested limit exist.
    Stable sitemap discovery order breaks ties, and exact URL strings (including
    query strings and fragments) are used for duplicate detection.

    Um limite ``None`` significa "sem amostragem": todas as URLs daquele tipo
    (matérias, galerias ou vídeos) entram no inventário.

    ``exclude_urls`` remove matérias, galerias e vídeos já auditados antes do
    corte por quantidade, de modo que o limite pegue as próximas mais recentes.
    Não afeta ``main_routes`` (rotas de homes) nem ``entries``/``duplicates``,
    que continuam descrevendo o sitemap inteiro.

    ``source_paths`` mapeia cada papel (articles, galleries, videos, homes) aos
    sufixos de caminho dos sitemaps filhos no índice mestre; só os papéis
    presentes são lidos e exigidos (padrão: ``DEFAULT_SOURCE_PATHS``).
    ``page_type_rules`` é uma lista de ``(prefixo_de_caminho, tipo_de_pagina)``.
    """
    source_paths = DEFAULT_SOURCE_PATHS if source_paths is None else source_paths
    unknown_roles = set(source_paths) - set(_ROLES)
    if unknown_roles:
        raise ValueError(
            "Papéis de sitemap desconhecidos: " + ", ".join(sorted(unknown_roles))
        )
    provided_limits = [
        value
        for value in (articles_limit, galleries_limit, videos_limit)
        if value is not None
    ]
    if provided_limits and min(provided_limits) < 0:
        raise ValueError("Selection limits must be zero or greater")
    if concurrency < 1:
        raise ValueError("Concurrency must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("HTTP timeout must be greater than zero")

    parsed_index = urlsplit(sitemap_index_url)
    if (
        parsed_index.scheme != "https"
        or parsed_index.hostname is None
        or parsed_index.username
        or parsed_index.password
    ):
        raise ValueError("The sitemap index URL must be an absolute HTTPS URL")
    expected_host = parsed_index.hostname

    state = _FetchState()
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

        async def fetch_xml(sitemap_url: str) -> bytes:
            if not _same_site_sitemap_url(sitemap_url, expected_host):
                raise SitemapFetchError(
                    "Refusing a sitemap URL outside the configured HTTPS host",
                    sitemap_url,
                    accessed_sitemaps=tuple(state.accessed),
                )
            async with semaphore:
                state.accessed.append(sitemap_url)
                try:
                    response = await client.get(sitemap_url)
                except httpx.TimeoutException as exc:
                    raise SitemapFetchError(
                        f"Timeout while fetching sitemap ({exc})",
                        sitemap_url,
                        accessed_sitemaps=tuple(state.accessed),
                    ) from exc
                except httpx.RequestError as exc:
                    raise SitemapFetchError(
                        f"Connection error while fetching sitemap ({exc})",
                        sitemap_url,
                        accessed_sitemaps=tuple(state.accessed),
                    ) from exc

                if response.status_code < 200 or response.status_code >= 300:
                    raise SitemapHTTPError(
                        sitemap_url,
                        response.status_code,
                        accessed_sitemaps=tuple(state.accessed),
                    )
                if not _same_site_sitemap_url(str(response.url), expected_host):
                    raise SitemapFetchError(
                        "Sitemap request redirected outside the configured HTTPS host",
                        sitemap_url,
                        accessed_sitemaps=tuple(state.accessed),
                    )
                return response.content

        try:
            master_xml = await fetch_xml(sitemap_index_url)
            master_kind, master_records = _parse_sitemap_xml(
                master_xml, sitemap_index_url
            )
            if master_kind != "sitemapindex":
                raise SitemapXMLFormatError(
                    "The configured master sitemap must be a sitemapindex",
                    sitemap_index_url,
                    accessed_sitemaps=tuple(state.accessed),
                )
            state.url_counts[sitemap_index_url] = 0

            source_sitemaps: dict[SitemapRole, list[str]] = {
                role: [] for role in _ROLES if role in source_paths  # type: ignore[misc]
            }
            for record in master_records:
                role = _role_for_sitemap_url(record.url, source_paths)
                if role is not None:
                    if not _same_site_sitemap_url(record.url, expected_host):
                        raise SitemapFetchError(
                            "Master index references a sitemap outside its HTTPS host",
                            record.url,
                            accessed_sitemaps=tuple(state.accessed),
                        )
                    source_sitemaps[role].append(record.url)

            missing_sources = [
                role for role, urls in source_sitemaps.items() if not urls
            ]
            if missing_sources:
                names = ", ".join(missing_sources)
                raise SitemapSourceError(
                    f"Master index does not expose required sitemap sources: {names}",
                    sitemap_index_url,
                    accessed_sitemaps=tuple(state.accessed),
                )

            visited: set[tuple[str, SitemapRole]] = set()

            async def walk_sitemap(
                sitemap_url: str, role: SitemapRole
            ) -> list[tuple[_URLRecord, str]]:
                visit_key = (sitemap_url, role)
                if visit_key in visited:
                    return []
                visited.add(visit_key)

                xml_content = await fetch_xml(sitemap_url)
                kind, records = _parse_sitemap_xml(xml_content, sitemap_url)
                if kind == "urlset":
                    state.url_counts[sitemap_url] = len(records)
                    return [(record, sitemap_url) for record in records]

                state.url_counts[sitemap_url] = 0
                child_urls = [record.url for record in records]
                for child_url in child_urls:
                    if not _same_site_sitemap_url(child_url, expected_host):
                        raise SitemapFetchError(
                            "Sitemap index references a child outside its HTTPS host",
                            child_url,
                            accessed_sitemaps=tuple(state.accessed),
                        )
                children = await asyncio.gather(
                    *(walk_sitemap(child_url, role) for child_url in child_urls),
                    return_exceptions=True,
                )
                for child in children:
                    if isinstance(child, BaseException):
                        raise child
                return [
                    record
                    for child in children
                    for record in child
                ]

            source_tasks = {
                role: asyncio.gather(
                    *(walk_sitemap(url, role) for url in urls),
                    return_exceptions=True,
                )
                for role, urls in source_sitemaps.items()
            }
            source_results = await asyncio.gather(
                *source_tasks.values(),
                return_exceptions=True,
            )
            raw_by_role: dict[SitemapRole, list[tuple[_URLRecord, str]]] = {}
            for role, result in zip(source_tasks, source_results):
                if isinstance(result, BaseException):
                    raise result
                raw_records: list[tuple[_URLRecord, str]] = []
                for sitemap_result in result:
                    if isinstance(sitemap_result, BaseException):
                        raise sitemap_result
                    raw_records.extend(sitemap_result)
                raw_by_role[role] = raw_records

        except SitemapReadError as exc:
            if not exc.accessed_sitemaps:
                exc.accessed_sitemaps = tuple(state.accessed)
            raise

    aggregates: dict[str, _Aggregate] = {}
    role_url_order: dict[SitemapRole, list[str]] = {
        "articles": [],
        "galleries": [],
        "videos": [],
        "homes": [],
    }
    role_url_seen: dict[SitemapRole, set[str]] = {
        "articles": set(),
        "galleries": set(),
        "videos": set(),
        "homes": set(),
    }

    for role, raw_records in raw_by_role.items():
        for record, origin in raw_records:
            if record.url not in role_url_seen[role]:
                role_url_seen[role].add(record.url)
                role_url_order[role].append(record.url)
            aggregate = aggregates.setdefault(
                record.url,
                _Aggregate(lastmod=None, origins=[], occurrences=0, roles=set()),
            )
            current_lastmod = aggregate.lastmod
            if record.lastmod is not None and (
                current_lastmod is None or record.lastmod > current_lastmod
            ):
                aggregate.lastmod = record.lastmod
            if origin not in aggregate.origins:
                aggregate.origins.append(origin)
            aggregate.occurrences += 1
            aggregate.roles.add(role)

    entries_by_url: dict[str, SitemapEntry] = {}
    for url, aggregate in aggregates.items():
        roles = aggregate.roles
        if "articles" in roles:
            preferred_role: SitemapRole = "articles"
        elif "videos" in roles:
            preferred_role = "videos"
        elif "galleries" in roles:
            preferred_role = "galleries"
        else:
            preferred_role = "homes"
        entries_by_url[url] = SitemapEntry(
            url=url,
            lastmod=aggregate.lastmod,
            origins=tuple(aggregate.origins),
            occurrences=aggregate.occurrences,
            page_type=classify_page_type(url, preferred_role, page_type_rules),
        )

    all_entries = tuple(entries_by_url.values())
    return SitemapInventory(
        entries=all_entries,
        articles=_sort_newest(
            [
                entries_by_url[url]
                for url in role_url_order["articles"]
                if url not in exclude_urls
            ],
            articles_limit,
        ),
        galleries=_sort_newest(
            [
                entries_by_url[url]
                for url in role_url_order["galleries"]
                if url not in exclude_urls
            ],
            galleries_limit,
        ),
        videos=_sort_newest(
            [
                entries_by_url[url]
                for url in role_url_order["videos"]
                if url not in exclude_urls
            ],
            videos_limit,
        ),
        main_routes=tuple(
            entries_by_url[url] for url in role_url_order["homes"]
        ),
        duplicates=tuple(
            entry for entry in all_entries if entry.is_duplicate
        ),
        accessed_sitemaps=tuple(state.accessed),
        url_counts_by_sitemap=tuple(state.url_counts.items()),
    )
