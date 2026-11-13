"""Monta o dashboard estático a partir dos relatórios de uma execução.

O dashboard é 100% local e sem dependências externas: lê os CSV e o Markdown
gerados na pasta da execução, consolida tudo em um ``dados.json`` e copia o
``index.html`` (que usa apenas HTML/CSS/JS puro, sem CDN). Assim, basta servir a
pasta por HTTP para abrir o painel em ``http://localhost``.
"""

from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

_TEMPLATE = Path(__file__).with_name("template") / "index.html"


def _read_audit_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _read_schema_detail_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _as_bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() == "true"


def _as_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _as_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _summarize(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Consolida contagens a partir das linhas de ``auditoria.csv``."""
    total = len(rows)
    counters = {
        "fora_do_ar": 0,
        "seo_fail": 0,
        "schema_fail": 0,
        "cwv_ruim": 0,
        "http_verificadas": 0,
        "seo_verificadas": 0,
        "schema_verificadas": 0,
        "cwv_verificadas": 0,
        # URLs que entraram na amostra de CWV (homes + 1 matéria por
        # categoria). Uma URL amostrada sempre tem faixa preenchida (mesmo que
        # "sem_dado"); as demais ficam fora da coleta e não contam aqui.
        "cwv_amostradas": 0,
        "cwv_sem_dado": 0,
    }
    by_type: dict[str, int] = {}
    cwv_bands: dict[str, int] = {}
    status_classes: dict[str, int] = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "sem": 0}

    for row in rows:
        tipo = row.get("tipo_pagina") or "desconhecido"
        by_type[tipo] = by_type.get(tipo, 0) + 1

        fora = _as_bool(row.get("fora_do_ar"))
        if fora is not None:
            counters["http_verificadas"] += 1
            if fora:
                counters["fora_do_ar"] += 1

        seo_ok = _as_bool(row.get("seo_ok"))
        if seo_ok is not None:
            counters["seo_verificadas"] += 1
            if not seo_ok:
                counters["seo_fail"] += 1

        schema_ok = _as_bool(row.get("schema_ok"))
        if schema_ok is not None:
            counters["schema_verificadas"] += 1
            if not schema_ok:
                counters["schema_fail"] += 1

        cwv_ruim = _as_bool(row.get("cwv_ruim"))
        if cwv_ruim is not None:
            counters["cwv_verificadas"] += 1
            if cwv_ruim:
                counters["cwv_ruim"] += 1

        faixa = row.get("cwv_faixa") or ""
        if faixa:
            # Faixa preenchida = URL fez parte da amostra de CWV.
            counters["cwv_amostradas"] += 1
            if faixa == "sem_dado":
                counters["cwv_sem_dado"] += 1
            cwv_bands[faixa] = cwv_bands.get(faixa, 0) + 1

        status = _as_int(row.get("status_final"))
        if status is None:
            status_classes["sem"] += 1
        else:
            bucket = f"{status // 100}xx"
            status_classes[bucket] = status_classes.get(bucket, 0) + 1

    return {
        "total_urls": total,
        "counters": counters,
        "por_tipo": by_type,
        "cwv_faixas": cwv_bands,
        "status_http": status_classes,
    }


def _normalize_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Converte os campos relevantes para tipos JSON amigáveis à UI."""
    normalized: list[dict[str, Any]] = []
    for row in rows:
        normalized.append(
            {
                "url": row.get("url", ""),
                "tipo_pagina": row.get("tipo_pagina", ""),
                "sitemap_origem": row.get("sitemap_origem", ""),
                "lastmod": row.get("lastmod", ""),
                "status_final": _as_int(row.get("status_final")),
                "tempo_ms": _as_int(row.get("tempo_ms")),
                "fora_do_ar": _as_bool(row.get("fora_do_ar")),
                "seo_ok": _as_bool(row.get("seo_ok")),
                "seo_problemas": row.get("seo_problemas", ""),
                "schema_ok": _as_bool(row.get("schema_ok")),
                "schema_problemas": row.get("schema_problemas", ""),
                "schema_observacoes": row.get("schema_observacoes", ""),
                "lcp_ms": _as_int(row.get("lcp_ms")),
                "inp_ms": _as_int(row.get("inp_ms")),
                "cls": _as_float(row.get("cls")),
                "tbt_ms": _as_int(row.get("tbt_ms")),
                "cwv_fonte": row.get("cwv_fonte", ""),
                "cwv_faixa": row.get("cwv_faixa", ""),
                "cwv_ruim": _as_bool(row.get("cwv_ruim")),
                # True quando a URL entrou na amostra de CWV (faixa preenchida).
                "cwv_amostrado": bool(row.get("cwv_faixa")),
            }
        )
    return normalized


def build_dashboard(run_directory: str | Path) -> Path:
    """Gera ``dashboard/`` dentro de ``run_directory`` e retorna o index.html.

    Lê ``auditoria.csv``, ``schema_detalhe.csv`` e ``resumo.md`` da pasta da
    execução. Arquivos ausentes são tratados como vazios, então o dashboard
    funciona mesmo que o Core Web Vitals não tenha sido coletado.
    """
    run_dir = Path(run_directory)
    audit_rows = _read_audit_csv(run_dir / "auditoria.csv")
    schema_rows = _read_schema_detail_csv(run_dir / "schema_detalhe.csv")

    resumo_path = run_dir / "resumo.md"
    resumo_md = (
        resumo_path.read_text(encoding="utf-8") if resumo_path.exists() else ""
    )

    available_files = sorted(
        p.name
        for p in run_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".csv", ".md", ".xlsx"}
    )

    data = {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "execucao": run_dir.name,
        "arquivos": available_files,
        "resumo": _summarize(audit_rows),
        "linhas": _normalize_rows(audit_rows),
        "schema_detalhe": schema_rows,
        "resumo_md": resumo_md,
    }

    dashboard_dir = run_dir / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)

    (dashboard_dir / "dados.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    index_destination = dashboard_dir / "index.html"
    shutil.copyfile(_TEMPLATE, index_destination)

    return index_destination
