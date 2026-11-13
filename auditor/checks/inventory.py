"""Alertas de inventário.

Gera avisos sobre as URLs do sitemap, sem fazer requisições:

- URL repetida no sitemap (mais de uma ocorrência).
- URL com fragmento (``#...``): o servidor nunca recebe o fragmento.
- Host sem ``www`` (opcional, ``inventory.require_www`` no config).
- Slug legado (opcional, ``inventory.legacy_slugs`` no config): trechos que não
  deveriam mais aparecer nas URLs, como o nome de um site ou seção antiga.

Cada alerta preserva a URL e seus sitemaps de origem.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from auditor.sitemap import SitemapEntry, SitemapInventory

InventoryAlertKind = Literal[
    "duplicate_url",
    "url_with_fragment",
    "host_without_www",
    "legacy_slug",
]


@dataclass(frozen=True)
class InventoryAlert:
    kind: InventoryAlertKind
    url: str
    origins: tuple[str, ...]
    detail: str


def _check_entry(
    entry: SitemapEntry,
    *,
    require_www: bool,
    legacy_slugs: Sequence[str],
) -> list[InventoryAlert]:
    alerts: list[InventoryAlert] = []
    parsed = urlsplit(entry.url)
    host = (parsed.hostname or "").lower()

    if entry.occurrences > 1:
        alerts.append(
            InventoryAlert(
                kind="duplicate_url",
                url=entry.url,
                origins=entry.origins,
                detail=f"{entry.occurrences} ocorrências no(s) sitemap(s)",
            )
        )
    if parsed.fragment:
        alerts.append(
            InventoryAlert(
                kind="url_with_fragment",
                url=entry.url,
                origins=entry.origins,
                detail=f"fragmento '#{parsed.fragment}' não chega ao servidor",
            )
        )
    if require_www and host and not host.startswith("www."):
        alerts.append(
            InventoryAlert(
                kind="host_without_www",
                url=entry.url,
                origins=entry.origins,
                detail=f"host sem www: {host}",
            )
        )
    lowered_url = entry.url.lower()
    for slug in legacy_slugs:
        if slug and slug.lower() in lowered_url:
            alerts.append(
                InventoryAlert(
                    kind="legacy_slug",
                    url=entry.url,
                    origins=entry.origins,
                    detail=f"slug legado '{slug}'",
                )
            )
    return alerts


def collect_inventory_alerts(
    inventory: SitemapInventory,
    *,
    require_www: bool = False,
    legacy_slugs: Sequence[str] = (),
) -> tuple[InventoryAlert, ...]:
    """Varre todas as entradas do inventário e reúne os alertas."""
    alerts: list[InventoryAlert] = []
    for entry in inventory.entries:
        alerts.extend(
            _check_entry(
                entry, require_www=require_www, legacy_slugs=legacy_slugs
            )
        )
    return tuple(alerts)
