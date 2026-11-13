"""Utilitários de configuração e segredos.

Carrega variáveis de ambiente, com suporte a um arquivo ``.env`` simples, sem
dependência externa. Segredos (como ``PSI_API_KEY``) nunca são impressos.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """Lê um ``.env`` simples (``CHAVE=valor`` por linha).

    Linhas vazias e comentários (``#``) são ignorados. Não sobrescreve variáveis
    já presentes no ambiente do processo. Retorna o mapa lido do arquivo.
    """
    env_path = Path(path)
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
            os.environ.setdefault(key, value)
    return values


def get_psi_api_key(*, dotenv_path: str | Path = ".env") -> str:
    """Obtém ``PSI_API_KEY`` do ambiente, consultando o ``.env`` como fallback.

    Retorna string vazia se não encontrada; o chamador decide como tratar.
    """
    existing = os.environ.get("PSI_API_KEY")
    if existing:
        return existing
    return load_dotenv(dotenv_path).get("PSI_API_KEY", "")
