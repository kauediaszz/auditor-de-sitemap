"""Gera e serve o dashboard de uma execução da auditoria.

Uso:

    # usa a pasta de saída mais recente em ``saida/``
    python -m auditor.dashboard

    # aponta para uma execução específica
    python -m auditor.dashboard saida/2026-11-04_193935

    # só gera os arquivos do dashboard, sem subir o servidor
    python -m auditor.dashboard --no-serve
"""

from __future__ import annotations

import argparse
from pathlib import Path

from auditor.dashboard.builder import build_dashboard
from auditor.dashboard.server import serve_dashboard


def _latest_run(output_root: Path) -> Path | None:
    if not output_root.exists():
        return None
    runs = [p for p in output_root.iterdir() if p.is_dir() and p.name != "."]
    runs = [p for p in runs if (p / "auditoria.csv").exists()]
    if not runs:
        return None
    return max(runs, key=lambda p: p.stat().st_mtime)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gera e serve o dashboard de uma execução da auditoria."
    )
    parser.add_argument(
        "run_directory",
        nargs="?",
        help="Pasta da execução (padrão: a mais recente em 'saida/').",
    )
    parser.add_argument(
        "--output-root",
        default="saida",
        help="Raiz das saídas, usada quando run_directory é omitido.",
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Config usado pelo botão 'Rodar novamente' (padrão: config/config.yaml).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Porta inicial do servidor local (padrão: 8000).",
    )
    parser.add_argument(
        "--no-serve",
        action="store_true",
        help="Apenas gera o dashboard, sem iniciar o servidor.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Não tenta abrir o navegador automaticamente.",
    )
    arguments = parser.parse_args()

    if arguments.run_directory:
        run_dir = Path(arguments.run_directory)
    else:
        run_dir = _latest_run(Path(arguments.output_root))
        if run_dir is None:
            parser.error(
                "Nenhuma execução encontrada em "
                f"'{arguments.output_root}'. Rode a auditoria primeiro ou "
                "informe a pasta explicitamente."
            )

    if not run_dir.exists():
        parser.error(f"Pasta não encontrada: {run_dir}")

    index_path = build_dashboard(run_dir)
    print(f"Dashboard gerado em: {index_path}")

    if not arguments.no_serve:
        serve_dashboard(
            index_path.parent,
            port=arguments.port,
            open_browser=not arguments.no_browser,
            config_path=arguments.config,
        )


if __name__ == "__main__":
    main()
