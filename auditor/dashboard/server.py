"""Servidor HTTP local para abrir o dashboard em ``http://localhost:<porta>``.

Usa apenas a biblioteca padrão (``http.server``). Serve a pasta da execução
atual (a que contém ``dashboard/`` e os relatórios ``.csv``/``.md``/``.xlsx``),
de modo que o ``index.html`` carregue o ``dados.json`` por ``fetch`` e também
ofereça links de download para os arquivos originais da entrega. A raiz ``/``
redireciona para ``/dashboard/``.

Quando recebe ``config_path``, expõe também uma API para rodar uma nova
auditoria só com URLs ainda não auditadas:

- ``GET  /api/status``: estado da rodada e padrões de quantidade.
- ``POST /api/run``: corpo JSON ``{"articles": n, "galleries": n, "videos": n}``.

A API só aceita requisições locais (``Host``/``Origin`` de localhost) com
``Content-Type: application/json``, e roda uma auditoria por vez. Ao concluir,
o servidor passa a servir a pasta da nova execução.
"""

from __future__ import annotations

import http.server
import json
import threading
import webbrowser
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
_MAX_BODY_BYTES = 1024
_RUN_KEYS = ("articles", "galleries", "videos")


def _default_runner(
    config_path: Path, limits: dict[str, int | None]
) -> Any:
    """Executa ``run_audit`` só com URLs novas (import tardio evita ciclo)."""
    import asyncio

    from auditor.main import run_audit

    return asyncio.run(
        run_audit(
            config_path,
            articles_limit=limits["articles"],
            galleries_limit=limits["galleries"],
            videos_limit=limits["videos"],
            only_new=True,
        )
    )


class ServerState:
    """Estado compartilhado: execução servida e controle da rodada em curso."""

    def __init__(
        self,
        current_run: Path,
        *,
        config_path: Path | None = None,
        runner: Callable[[Path, dict[str, int | None]], Any] | None = None,
    ) -> None:
        self.current_run = current_run
        self.config_path = config_path
        self._runner = runner or _default_runner
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {
            "estado": "ocioso",
            "mensagem": "",
            "pedido": None,
            "obtido": None,
        }

    @property
    def api_enabled(self) -> bool:
        return self.config_path is not None

    def _config(self) -> dict[str, Any]:
        from auditor.main import _load_config

        assert self.config_path is not None
        return _load_config(self.config_path)

    def status(self) -> dict[str, Any]:
        config = self._config()
        with self._lock:
            snapshot = dict(self._status)
        snapshot.update(
            {
                "api": True,
                "execucao": self.current_run.name,
                "padroes": {k: config["sampling"][k] for k in _RUN_KEYS},
                "teto": (config.get("limits") or {}).get("max_urls_per_run", 500),
            }
        )
        return snapshot

    def start_run(self, payload: Any) -> tuple[int, dict[str, Any]]:
        """Valida o pedido e inicia a rodada. Devolve ``(http_status, corpo)``."""
        from auditor.main import resolve_limits

        if not isinstance(payload, dict) or set(payload) != set(_RUN_KEYS):
            return 400, {"erro": "Informe articles, galleries e videos."}
        try:
            limits = resolve_limits(
                self._config(),
                articles=payload["articles"],
                galleries=payload["galleries"],
                videos=payload["videos"],
            )
        except ValueError as error:
            return 400, {"erro": str(error)}
        if not any(limits.values()):
            return 400, {"erro": "Informe ao menos uma URL para auditar."}

        with self._lock:
            if self._status["estado"] == "rodando":
                return 409, {"erro": "Já existe uma auditoria em andamento."}
            self._status = {
                "estado": "rodando",
                "mensagem": "Auditoria em andamento...",
                "pedido": limits,
                "obtido": None,
            }
        threading.Thread(
            target=self._run, args=(limits,), name="auditoria", daemon=True
        ).start()
        return 202, {"estado": "rodando"}

    def _run(self, limits: dict[str, int | None]) -> None:
        from auditor.main import NoNewURLsError

        try:
            outputs = self._runner(self.config_path, limits)
        except NoNewURLsError:
            self._finish("sem_novas", "Não há URLs novas para auditar.")
            return
        except Exception as error:  # a rodada nunca derruba o servidor
            self._finish("erro", f"Falha na auditoria: {type(error).__name__}")
            return
        run_dir = getattr(outputs, "run_directory", None)
        if run_dir is not None and (Path(run_dir) / "dashboard").is_dir():
            self.current_run = Path(run_dir)
        self._finish(
            "concluido",
            "Auditoria concluída.",
            obtained=getattr(outputs, "obtained", None),
        )

    def _finish(
        self, estado: str, mensagem: str, *, obtained: Any = None
    ) -> None:
        with self._lock:
            self._status = {
                "estado": estado,
                "mensagem": mensagem,
                "pedido": self._status.get("pedido"),
                "obtido": obtained,
            }


def _host_name(netloc: str) -> str:
    if netloc.startswith("["):
        return netloc.split("]")[0] + "]"
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


class _DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """Serve arquivos, expõe a API local e redireciona a raiz."""

    def __init__(self, *args: Any, state: ServerState, **kwargs: Any) -> None:
        self.state = state
        super().__init__(*args, directory=str(state.current_run), **kwargs)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def end_headers(self) -> None:
        # O conteúdo muda a cada rodada: não deixar o navegador reaproveitar.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # --- utilidades da API ---
    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _is_local_request(self) -> bool:
        """Bloqueia chamadas de outras origens e DNS rebinding."""
        if _host_name(self.headers.get("Host", "")) not in _LOCAL_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            origin_host = urlsplit(origin).netloc
            if _host_name(origin_host) not in _LOCAL_HOSTS:
                return False
        return True

    def do_GET(self) -> None:  # noqa: N802 (nome exigido pela base)
        path = urlsplit(self.path).path
        if path == "/api/status":
            if not self.state.api_enabled:
                self._send_json(404, {"api": False})
            elif not self._is_local_request():
                self._send_json(403, {"erro": "Origem não permitida."})
            else:
                self._send_json(200, self.state.status())
            return
        if path in ("", "/"):
            self.send_response(302)
            self.send_header("Location", "/dashboard/")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != "/api/run" or not self.state.api_enabled:
            self._send_json(404, {"erro": "Não encontrado."})
            return
        if not self._is_local_request():
            self._send_json(403, {"erro": "Origem não permitida."})
            return
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip()
        if content_type != "application/json":
            self._send_json(415, {"erro": "Use Content-Type application/json."})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json(411, {"erro": "Content-Length obrigatório."})
            return
        if length < 0 or length > _MAX_BODY_BYTES:
            self._send_json(413, {"erro": "Corpo grande demais."})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"erro": "JSON inválido."})
            return
        status, body = self.state.start_run(payload)
        self._send_json(status, body)


def make_server(
    dashboard_dir: str | Path,
    *,
    port: int = 8000,
    config_path: str | Path | None = None,
    runner: Callable[[Path, dict[str, int | None]], Any] | None = None,
) -> tuple[http.server.ThreadingHTTPServer, ServerState]:
    """Cria o servidor numa porta livre (a partir de ``port``), sem iniciá-lo."""
    dashboard_path = Path(dashboard_dir).resolve()
    if not (dashboard_path / "index.html").exists():
        raise FileNotFoundError(
            f"index.html não encontrado em {dashboard_path}. "
            "Gere o dashboard antes de servir."
        )
    state = ServerState(
        dashboard_path.parent,
        config_path=Path(config_path) if config_path is not None else None,
        runner=runner,
    )
    handler = partial(_DashboardHandler, state=state)

    for candidate in range(port, port + 20):
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", candidate), handler)
        except OSError:
            continue
        httpd.daemon_threads = True
        return httpd, state
    raise OSError(
        f"Nenhuma porta livre entre {port} e {port + 19} para o dashboard."
    )


def serve_dashboard(
    dashboard_dir: str | Path,
    *,
    port: int = 8000,
    open_browser: bool = True,
    config_path: str | Path | None = None,
) -> None:
    """Sobe um servidor local e bloqueia até Ctrl+C.

    ``dashboard_dir`` é a pasta ``dashboard/`` gerada; o servidor serve a pasta
    pai (a execução) como raiz para que os arquivos originais fiquem acessíveis.
    Se a porta estiver ocupada, tenta as seguintes até achar uma livre. Com
    ``config_path``, habilita o botão "Rodar novamente" (API de rodada).
    """
    httpd, _state = make_server(
        dashboard_dir, port=port, config_path=config_path
    )
    chosen_port = httpd.server_address[1]
    url = f"http://localhost:{chosen_port}/dashboard/"
    print(f"Dashboard disponível em: {url}")
    print("Pressione Ctrl+C para encerrar o servidor.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    with httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServidor do dashboard encerrado.")
