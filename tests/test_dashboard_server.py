"""Testes da API local do dashboard (botão 'Rodar novamente')."""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from auditor.dashboard.server import make_server
from auditor.main import NoNewURLsError

_CONFIG = """
sources:
  sitemap_index: https://example.test/sitemap/index.xml
sampling:
  articles: 80
  galleries: 20
  videos: 20
http:
  concurrency: 2
  timeout_seconds: 5
output:
  directory: saida
limits:
  max_urls_per_run: 100
"""


def _make_run(root: Path, name: str, marker: str) -> Path:
    run = root / name
    (run / "dashboard").mkdir(parents=True)
    (run / "dashboard" / "index.html").write_text(marker, encoding="utf-8")
    return run


class _Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            payload = None if body is None else json.dumps(body)
            all_headers = {"Content-Type": "application/json"}
            all_headers.update(headers or {})
            conn.request(method, path, body=payload, headers=all_headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8")
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, raw
        finally:
            conn.close()


@pytest.fixture
def served(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(_CONFIG, encoding="utf-8")
    first = _make_run(tmp_path, "run1", "PRIMEIRA")
    second = _make_run(tmp_path, "run2", "SEGUNDA")
    gate = threading.Event()
    calls: list[dict] = []
    behavior = {"mode": "ok"}

    def runner(config_path, limits):
        calls.append(dict(limits))
        gate.wait(timeout=5)
        if behavior["mode"] == "empty":
            raise NoNewURLsError("sem novas")
        if behavior["mode"] == "boom":
            raise RuntimeError("segredo-interno")
        return SimpleNamespace(
            run_directory=second,
            obtained={"articles": 2, "galleries": 0, "videos": 0},
        )

    httpd, state = make_server(
        first / "dashboard", port=18000, config_path=config, runner=runner
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    gate.set()  # por padrão, o runner não bloqueia
    try:
        yield SimpleNamespace(
            client=_Client(httpd.server_address[1]),
            state=state,
            gate=gate,
            calls=calls,
            behavior=behavior,
        )
    finally:
        gate.set()
        httpd.shutdown()
        httpd.server_close()


def _wait_for(client, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = client.request("GET", "/api/status")
        if predicate(body):
            return body
        time.sleep(0.05)
    raise AssertionError(f"timeout; último estado: {body}")


def test_status_reports_defaults_and_cap(served):
    status, body = served.client.request("GET", "/api/status")
    assert status == 200
    assert body["estado"] == "ocioso"
    assert body["execucao"] == "run1"
    assert body["padroes"] == {"articles": 80, "galleries": 20, "videos": 20}
    assert body["teto"] == 100


def test_api_disabled_without_config(tmp_path):
    first = _make_run(tmp_path, "run1", "X")
    httpd, _ = make_server(first / "dashboard", port=18100)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        client = _Client(httpd.server_address[1])
        assert client.request("GET", "/api/status")[0] == 404
        assert client.request("POST", "/api/run", {})[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_run_starts_and_switches_served_run(served):
    status, _ = served.client.request(
        "POST", "/api/run", {"articles": 5, "galleries": 0, "videos": 0}
    )
    assert status == 202
    body = _wait_for(served.client, lambda b: b["estado"] == "concluido")
    assert body["execucao"] == "run2"
    assert body["obtido"]["articles"] == 2
    assert served.calls == [{"articles": 5, "galleries": 0, "videos": 0}]
    # Novas conexões passam a servir a pasta da nova execução.
    _, html = served.client.request("GET", "/dashboard/index.html")
    assert html == "SEGUNDA"


@pytest.mark.parametrize(
    "payload",
    [
        {"articles": -1, "galleries": 0, "videos": 0},
        {"articles": "10", "galleries": 0, "videos": 0},
        {"articles": True, "galleries": 0, "videos": 0},
        {"articles": 1.5, "galleries": 0, "videos": 0},
        {"articles": 90, "galleries": 10, "videos": 10},  # soma 110 > teto 100
        {"articles": 0, "galleries": 0, "videos": 0},  # nada a auditar
        {"articles": 5},  # faltam chaves
        {"articles": 5, "galleries": 0, "videos": 0, "extra": 1},
        [1, 2, 3],
    ],
)
def test_run_rejects_invalid_payload_on_server(served, payload):
    status, body = served.client.request("POST", "/api/run", payload)
    assert status == 400
    assert "erro" in body
    assert served.calls == []


def test_second_run_while_running_returns_409(served):
    served.gate.clear()
    ok = {"articles": 3, "galleries": 0, "videos": 0}
    assert served.client.request("POST", "/api/run", ok)[0] == 202
    status, _ = served.client.request("POST", "/api/run", ok)
    assert status == 409
    served.gate.set()
    _wait_for(served.client, lambda b: b["estado"] == "concluido")
    assert len(served.calls) == 1


def test_no_new_urls_and_errors_are_reported_without_leaking(served):
    ok = {"articles": 3, "galleries": 0, "videos": 0}

    served.behavior["mode"] = "empty"
    served.client.request("POST", "/api/run", ok)
    body = _wait_for(served.client, lambda b: b["estado"] == "sem_novas")
    assert "novas" in body["mensagem"]

    served.behavior["mode"] = "boom"
    served.client.request("POST", "/api/run", ok)
    body = _wait_for(served.client, lambda b: b["estado"] == "erro")
    assert "segredo-interno" not in json.dumps(body)
    # A run servida não mudou após as falhas.
    assert body["execucao"] == "run1"


def test_rejects_foreign_origin_host_and_wrong_content_type(served):
    ok = {"articles": 3, "galleries": 0, "videos": 0}
    status, _ = served.client.request(
        "POST", "/api/run", ok, headers={"Origin": "https://evil.example"}
    )
    assert status == 403
    status, _ = served.client.request(
        "POST", "/api/run", ok, headers={"Host": "evil.example"}
    )
    assert status == 403
    status, _ = served.client.request(
        "GET", "/api/status", headers={"Host": "evil.example"}
    )
    assert status == 403
    status, _ = served.client.request(
        "POST", "/api/run", ok, headers={"Content-Type": "text/plain"}
    )
    assert status == 415
    assert served.calls == []


def test_rejects_oversized_and_malformed_body(served):
    conn = http.client.HTTPConnection("127.0.0.1", served.client.port, timeout=5)
    try:
        conn.request(
            "POST",
            "/api/run",
            body="{ não é json",
            headers={"Content-Type": "application/json"},
        )
        assert conn.getresponse().status == 400
    finally:
        conn.close()
    big = {"articles": 1, "galleries": 0, "videos": 0, "x": "a" * 5000}
    assert served.client.request("POST", "/api/run", big)[0] == 413
    assert served.calls == []
