"""Testes do dashboard (gerador estático). Usam só a biblioteca padrão."""

from __future__ import annotations

import json
from pathlib import Path

from auditor.dashboard.builder import build_dashboard

_AUDIT_CSV = (
    "url,tipo_pagina,sitemap_origem,lastmod,status_final,redirects,tempo_ms,"
    "fora_do_ar,seo_ok,seo_problemas,schema_ok,schema_problemas,"
    "schema_observacoes,lcp_ms,inp_ms,cls,tbt_ms,cwv_fonte,cwv_faixa,cwv_ruim\n"
    "https://x/a.html,article,https://x/sitemap.xml,,200,,120,"
    "false,true,,true,,,2500,180,0.05,,field,bom,false\n"
    "https://x/b.html,article,https://x/sitemap.xml,,404,,90,"
    "true,false,sem title,false,Article.headline: obrigatorio,,"
    ",,,,,ruim,true\n"
)

_SCHEMA_CSV = (
    "url,tipo_pagina,tipo_schema,regra,caminho_do_campo,esperado,encontrado\n"
    "https://x/b.html,article,Article,obrigatorio,headline,presente,ausente\n"
)

_RESUMO_MD = "# Resumo\n\n## Inventário\n\n- URLs únicas: 2\n"


def _make_run(tmp_path: Path) -> Path:
    run = tmp_path / "2099-01-01_000000"
    run.mkdir()
    (run / "auditoria.csv").write_text(_AUDIT_CSV, encoding="utf-8")
    (run / "schema_detalhe.csv").write_text(_SCHEMA_CSV, encoding="utf-8")
    (run / "resumo.md").write_text(_RESUMO_MD, encoding="utf-8")
    return run


def test_build_dashboard_creates_files(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    index = build_dashboard(run)

    assert index.exists()
    assert index.name == "index.html"
    dados = index.parent / "dados.json"
    assert dados.exists()


def test_build_dashboard_summarizes_counts(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    index = build_dashboard(run)
    data = json.loads((index.parent / "dados.json").read_text(encoding="utf-8"))

    assert data["resumo"]["total_urls"] == 2
    counters = data["resumo"]["counters"]
    assert counters["fora_do_ar"] == 1
    assert counters["seo_fail"] == 1
    assert counters["schema_fail"] == 1
    assert counters["cwv_ruim"] == 1
    assert data["resumo"]["status_http"]["2xx"] == 1
    assert data["resumo"]["status_http"]["4xx"] == 1
    assert data["resumo"]["cwv_faixas"] == {"bom": 1, "ruim": 1}
    assert len(data["linhas"]) == 2
    assert len(data["schema_detalhe"]) == 1
    assert data["arquivos"] == [
        "auditoria.csv",
        "resumo.md",
        "schema_detalhe.csv",
    ]


def test_build_dashboard_handles_missing_files(tmp_path: Path) -> None:
    run = tmp_path / "empty_run"
    run.mkdir()
    index = build_dashboard(run)
    data = json.loads((index.parent / "dados.json").read_text(encoding="utf-8"))

    assert data["resumo"]["total_urls"] == 0
    assert data["linhas"] == []
    assert data["resumo_md"] == ""
