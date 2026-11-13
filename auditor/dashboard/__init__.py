"""Dashboard HTML/JS da auditoria.

Lê os relatórios gerados em uma pasta de saída (``auditoria.csv``,
``schema_detalhe.csv`` e ``resumo.md``) e monta um painel estático
(``dashboard/index.html`` + ``dashboard/dados.json``) que pode ser servido
localmente em ``http://localhost:<porta>``.
"""

from __future__ import annotations

from auditor.dashboard.builder import build_dashboard
from auditor.dashboard.server import serve_dashboard

__all__ = ["build_dashboard", "serve_dashboard"]
