"""Verificações de SEO a partir do HTML das páginas selecionadas.

Lê o HTML de cada URL e avalia os seguintes pontos:
title e meta description presentes com tamanho razoável, canonical igual à
própria URL (com www), ausência de noindex, um único H1, presença de tags
Open Graph e igualdade entre a URL do sitemap e a URL final.

O Google não publica limites de caracteres para title/description, então o
tamanho fora da faixa configurada é tratado como AVISO (``warning``); apenas a
ausência desses elementos e as demais regras objetivas geram ``error``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Iterable
from urllib.parse import urlsplit

import httpx
from lxml import html as lxml_html

from auditor.models import SEOFinding, SEOResult
from auditor.sitemap import SitemapEntry

_USER_AGENT = "SitemapAuditor/0.1 (SEO HTML check)"

# Faixas padrão (estimativas da comunidade SEO, não regra do Google).
DEFAULT_TITLE_MIN = 50
DEFAULT_TITLE_MAX = 60
DEFAULT_DESCRIPTION_MIN = 120
DEFAULT_DESCRIPTION_MAX = 160


@dataclass(frozen=True)
class SEOConfig:
    """Limites configuráveis para os avisos de tamanho."""

    title_min: int = DEFAULT_TITLE_MIN
    title_max: int = DEFAULT_TITLE_MAX
    description_min: int = DEFAULT_DESCRIPTION_MIN
    description_max: int = DEFAULT_DESCRIPTION_MAX

    @classmethod
    def from_mapping(cls, mapping: dict | None) -> "SEOConfig":
        if not mapping:
            return cls()
        return cls(
            title_min=int(mapping.get("title_min", DEFAULT_TITLE_MIN)),
            title_max=int(mapping.get("title_max", DEFAULT_TITLE_MAX)),
            description_min=int(
                mapping.get("description_min", DEFAULT_DESCRIPTION_MIN)
            ),
            description_max=int(
                mapping.get("description_max", DEFAULT_DESCRIPTION_MAX)
            ),
        )


def _normalize_whitespace(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def _ensure_www(url: str) -> str:
    """Normaliza para comparar canonical/URL, forçando o host com www."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host and not host.startswith("www."):
        host = f"www.{host}"
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{host}{port}{path}"


def _extract_meta_description(tree: lxml_html.HtmlElement) -> str | None:
    for element in tree.iter("meta"):
        name = (element.get("name") or "").strip().lower()
        if name == "description":
            return _normalize_whitespace(element.get("content"))
    return None


def _has_noindex(tree: lxml_html.HtmlElement) -> bool:
    for element in tree.iter("meta"):
        name = (element.get("name") or "").strip().lower()
        if name in {"robots", "googlebot"}:
            content = (element.get("content") or "").lower()
            directives = {part.strip() for part in content.split(",")}
            if "noindex" in directives or "none" in directives:
                return True
    return False


def _extract_canonical(tree: lxml_html.HtmlElement) -> str | None:
    for element in tree.iter("link"):
        rel = (element.get("rel") or "").strip().lower()
        if rel == "canonical":
            return _normalize_whitespace(element.get("href"))
    return None


def _extract_open_graph(tree: lxml_html.HtmlElement) -> tuple[str, ...]:
    found: list[str] = []
    for element in tree.iter("meta"):
        prop = (element.get("property") or "").strip().lower()
        if prop.startswith("og:") and prop not in found:
            found.append(prop)
    return tuple(found)


_REQUIRED_OG = ("og:title", "og:type", "og:url", "og:image")


def _evaluate(
    *,
    title: str | None,
    title_length: int | None,
    description: str | None,
    description_length: int | None,
    canonical: str | None,
    has_noindex: bool,
    h1_count: int,
    open_graph: tuple[str, ...],
    requested_url: str,
    final_url: str | None,
    config: SEOConfig,
) -> tuple[SEOFinding, ...]:
    findings: list[SEOFinding] = []

    # Title
    if not title:
        findings.append(
            SEOFinding("title_present", "error", "title ausente ou vazio")
        )
    else:
        length = title_length or 0
        if length < config.title_min:
            findings.append(
                SEOFinding(
                    "title_length",
                    "warning",
                    f"title curto ({length} caracteres; recomendado "
                    f"{config.title_min}-{config.title_max})",
                )
            )
        elif length > config.title_max:
            findings.append(
                SEOFinding(
                    "title_length",
                    "warning",
                    f"title longo ({length} caracteres; recomendado "
                    f"{config.title_min}-{config.title_max})",
                )
            )

    # Meta description
    if not description:
        findings.append(
            SEOFinding(
                "description_present",
                "error",
                "meta description ausente ou vazia",
            )
        )
    else:
        length = description_length or 0
        if length < config.description_min:
            findings.append(
                SEOFinding(
                    "description_length",
                    "warning",
                    f"meta description curta ({length} caracteres; recomendado "
                    f"{config.description_min}-{config.description_max})",
                )
            )
        elif length > config.description_max:
            findings.append(
                SEOFinding(
                    "description_length",
                    "warning",
                    f"meta description longa ({length} caracteres; recomendado "
                    f"{config.description_min}-{config.description_max})",
                )
            )

    # noindex
    if has_noindex:
        findings.append(
            SEOFinding("noindex", "error", "página marcada como noindex")
        )

    # Canonical igual à própria URL (com www)
    reference_url = final_url or requested_url
    if canonical is None:
        findings.append(
            SEOFinding("canonical_present", "error", "canonical ausente")
        )
    elif _ensure_www(canonical) != _ensure_www(reference_url):
        findings.append(
            SEOFinding(
                "canonical_self",
                "error",
                f"canonical ({canonical}) difere da URL da página "
                f"({reference_url})",
            )
        )

    # Um único H1
    if h1_count == 0:
        findings.append(SEOFinding("h1_single", "error", "nenhum H1 na página"))
    elif h1_count > 1:
        findings.append(
            SEOFinding("h1_single", "error", f"{h1_count} tags H1 na página")
        )

    # Open Graph
    missing_og = [tag for tag in _REQUIRED_OG if tag not in open_graph]
    if missing_og:
        findings.append(
            SEOFinding(
                "open_graph",
                "warning",
                f"tags Open Graph ausentes: {', '.join(missing_og)}",
            )
        )

    # URL do sitemap igual à URL final
    if final_url is not None and _ensure_www(final_url) != _ensure_www(
        requested_url
    ):
        findings.append(
            SEOFinding(
                "sitemap_url_matches_final",
                "error",
                f"URL do sitemap ({requested_url}) difere da URL final "
                f"({final_url})",
            )
        )

    return tuple(findings)


def _analyze_html(
    content: bytes,
    *,
    requested_url: str,
    final_url: str | None,
    page_type: str,
    origins: tuple[str, ...],
    config: SEOConfig,
) -> SEOResult:
    tree = lxml_html.fromstring(content)

    title_elements = tree.findall(".//title")
    title = _normalize_whitespace(
        title_elements[0].text_content() if title_elements else None
    )
    description = _extract_meta_description(tree)
    canonical = _extract_canonical(tree)
    has_noindex = _has_noindex(tree)
    h1_count = len(tree.findall(".//h1"))
    open_graph = _extract_open_graph(tree)

    title_length = len(title) if title else None
    description_length = len(description) if description else None

    findings = _evaluate(
        title=title,
        title_length=title_length,
        description=description,
        description_length=description_length,
        canonical=canonical,
        has_noindex=has_noindex,
        h1_count=h1_count,
        open_graph=open_graph,
        requested_url=requested_url,
        final_url=final_url,
        config=config,
    )

    return SEOResult(
        url=requested_url,
        final_url=final_url,
        page_type=page_type,
        origins=origins,
        title=title,
        title_length=title_length,
        meta_description=description,
        meta_description_length=description_length,
        canonical=canonical,
        has_noindex=has_noindex,
        h1_count=h1_count,
        open_graph_tags=open_graph,
        findings=findings,
    )


def _error_result(
    entry: SitemapEntry, message: str
) -> SEOResult:
    return SEOResult(
        url=entry.url,
        final_url=None,
        page_type=entry.page_type,
        origins=entry.origins,
        title=None,
        title_length=None,
        meta_description=None,
        meta_description_length=None,
        canonical=None,
        has_noindex=False,
        h1_count=0,
        open_graph_tags=(),
        findings=(),
        error_message=message,
    )


async def check_seo(
    entries: Iterable[SitemapEntry],
    *,
    concurrency: int = 10,
    timeout_seconds: float = 30,
    config: SEOConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[SEOResult, ...]:
    """Baixa o HTML de cada entrada e avalia as regras de SEO.

    Falhas de rede, HTTP de erro ou HTML inválido viram um ``SEOResult`` com
    ``error_message`` preenchido (``seo_ok`` False), sem derrubar as demais URLs.
    """
    if concurrency < 1:
        raise ValueError("Concurrency must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("HTTP timeout must be greater than zero")

    seo_config = config or SEOConfig()
    selected_entries = tuple(entries)
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

        async def check_one(entry: SitemapEntry) -> SEOResult:
            async with semaphore:
                try:
                    parsed_url = urlsplit(entry.url)
                    valid_url = (
                        parsed_url.scheme in {"http", "https"}
                        and parsed_url.hostname is not None
                    )
                except ValueError:
                    valid_url = False
                if not valid_url:
                    return _error_result(entry, "Invalid request URL")

                try:
                    response = await client.get(entry.url)
                except httpx.TimeoutException as exc:
                    return _error_result(entry, f"Timeout ({type(exc).__name__})")
                except httpx.RequestError as exc:
                    return _error_result(
                        entry, f"Request failure ({type(exc).__name__})"
                    )

                if response.status_code >= 400:
                    return _error_result(
                        entry, f"HTTP status {response.status_code}"
                    )

                try:
                    return _analyze_html(
                        response.content,
                        requested_url=entry.url,
                        final_url=str(response.url),
                        page_type=entry.page_type,
                        origins=entry.origins,
                        config=seo_config,
                    )
                except Exception as exc:  # noqa: BLE001 - parse defensivo
                    return _error_result(
                        entry, f"HTML parse failure ({type(exc).__name__})"
                    )

        return tuple(
            await asyncio.gather(
                *(check_one(entry) for entry in selected_entries)
            )
        )
