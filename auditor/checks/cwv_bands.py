"""Faixas (bom / precisa melhorar / ruim) dos Core Web Vitals.

Valores do Google. Compartilhado entre os coletores
PageSpeed Insights e Chrome UX Report (CrUX).
"""

from __future__ import annotations

# Limites das faixas.
LCP_GOOD_MS = 2500.0
LCP_POOR_MS = 4000.0
INP_GOOD_MS = 200.0
INP_POOR_MS = 500.0
CLS_GOOD = 0.1
CLS_POOR = 0.25


def band(value: float | None, good: float, poor: float) -> str:
    """Classifica um valor numa das faixas, ou ``sem_dado`` se ``None``."""
    if value is None:
        return "sem_dado"
    if value <= good:
        return "bom"
    if value <= poor:
        return "precisa_melhorar"
    return "ruim"


def lcp_band(value: float | None) -> str:
    return band(value, LCP_GOOD_MS, LCP_POOR_MS)


def inp_band(value: float | None) -> str:
    return band(value, INP_GOOD_MS, INP_POOR_MS)


def cls_band(value: float | None) -> str:
    return band(value, CLS_GOOD, CLS_POOR)
