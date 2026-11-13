from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from auditor.history import HISTORY_FILENAME, load_history, record_urls

_HEADER = "url,tipo_pagina,sitemap_origem\n"


def _make_run(root: Path, name: str, rows: list[tuple[str, str]]) -> None:
    run = root / name
    run.mkdir(parents=True)
    body = "".join(f"{url},{tipo},s\n" for url, tipo in rows)
    (run / "auditoria.csv").write_text(_HEADER + body, encoding="utf-8")


def test_empty_output_root_gives_empty_history(tmp_path: Path) -> None:
    assert load_history(tmp_path / "nao_existe") == frozenset()
    assert load_history(tmp_path) == frozenset()


def test_bootstrap_reads_old_csvs_and_skips_routes(tmp_path: Path) -> None:
    _make_run(
        tmp_path,
        "2026-11-05_120000",
        [
            ("https://x/a.html", "article"),
            ("https://x/g", "gallery"),
            ("https://x/v", "video"),
            ("https://x/", "home"),
            ("https://x/tabela/y", "table"),
        ],
    )
    history = load_history(tmp_path)

    assert history == {"https://x/a.html", "https://x/g", "https://x/v"}
    # O bootstrap deixa o arquivo gravado.
    assert (tmp_path / HISTORY_FILENAME).is_file()


def test_record_urls_persists_and_counts_new(tmp_path: Path) -> None:
    when = datetime(2026, 11, 12, 7, 0, 0)
    assert record_urls(tmp_path, ["https://x/1", "https://x/2"], when=when) == 2
    assert record_urls(tmp_path, ["https://x/2", "https://x/3"], when=when) == 1

    assert load_history(tmp_path) == {"https://x/1", "https://x/2", "https://x/3"}
    data = json.loads((tmp_path / HISTORY_FILENAME).read_text(encoding="utf-8"))
    assert data["versao"] == 1
    assert data["urls"]["https://x/1"] == "2026-11-12T07:00:00"
    # Sem arquivos temporários sobrando.
    assert [p.name for p in tmp_path.iterdir()] == [HISTORY_FILENAME]


def test_corrupted_history_is_rebuilt_from_csvs(tmp_path: Path) -> None:
    _make_run(tmp_path, "2026-11-05_120000", [("https://x/a.html", "article")])
    (tmp_path / HISTORY_FILENAME).write_text("{ não é json", encoding="utf-8")

    assert load_history(tmp_path) == {"https://x/a.html"}
    # O arquivo foi regravado de forma válida.
    json.loads((tmp_path / HISTORY_FILENAME).read_text(encoding="utf-8"))
