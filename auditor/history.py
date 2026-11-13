"""Histórico de URLs já auditadas.

Guarda em ``<saida>/urls_auditadas.json`` as matérias, galerias e vídeos que já
passaram por uma auditoria, para que uma nova rodada possa pegar só URLs novas.
Rotas de ``homes.xml`` nunca entram aqui: elas são sempre auditadas por
completo.

Se o arquivo não existir (ou estiver corrompido), o histórico é reconstruído a
partir dos ``auditoria.csv`` das execuções anteriores em ``<saida>/*/``.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

HISTORY_FILENAME = "urls_auditadas.json"
HISTORY_VERSION = 1
# Tipos de página que contam como "amostra" (os mesmos que sofrem corte).
SAMPLED_PAGE_TYPES = frozenset({"article", "gallery", "video"})

_logger = logging.getLogger(__name__)


def history_path(output_root: str | Path) -> Path:
    return Path(output_root) / HISTORY_FILENAME


def _bootstrap_from_csvs(output_root: Path) -> dict[str, str]:
    """Reconstrói o histórico lendo os ``auditoria.csv`` já existentes."""
    urls: dict[str, str] = {}
    if not output_root.is_dir():
        return urls
    for run_dir in sorted(p for p in output_root.iterdir() if p.is_dir()):
        csv_path = run_dir / "auditoria.csv"
        if not csv_path.is_file():
            continue
        try:
            with csv_path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    url = (row.get("url") or "").strip()
                    if url and row.get("tipo_pagina") in SAMPLED_PAGE_TYPES:
                        urls.setdefault(url, run_dir.name)
        except (OSError, csv.Error, UnicodeDecodeError) as error:
            _logger.warning("Ignorando %s no histórico: %s", csv_path, error)
    return urls


def _write_atomic(path: Path, urls: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"versao": HISTORY_VERSION, "urls": urls}, ensure_ascii=False, indent=2
    )
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=".urls_auditadas.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _load_urls(output_root: Path) -> dict[str, str]:
    path = history_path(output_root)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            urls = data.get("urls") if isinstance(data, dict) else None
            if isinstance(urls, dict):
                return {str(k): str(v) for k, v in urls.items()}
            raise ValueError("estrutura inesperada")
        except (OSError, ValueError) as error:
            _logger.warning(
                "Histórico %s inválido (%s); reconstruindo pelos CSVs.",
                path,
                error,
            )
    rebuilt = _bootstrap_from_csvs(output_root)
    if rebuilt:
        _write_atomic(path, rebuilt)
    return rebuilt


def load_history(output_root: str | Path) -> frozenset[str]:
    """URLs já auditadas (reconstrói pelos CSVs se o arquivo não existir)."""
    return frozenset(_load_urls(Path(output_root)))


def record_urls(
    output_root: str | Path,
    urls: Iterable[str],
    *,
    when: datetime | None = None,
) -> int:
    """Acrescenta URLs ao histórico e devolve quantas eram novas."""
    root = Path(output_root)
    current = _load_urls(root)
    stamp = (when or datetime.now()).isoformat(timespec="seconds")
    added = 0
    for url in urls:
        if url not in current:
            current[url] = stamp
            added += 1
    _write_atomic(history_path(root), current)
    return added
