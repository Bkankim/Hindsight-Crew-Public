"""Auxiliary-port tests for rerank-server (issue #78). Bench only. No real model, no network.

The sidecar app is run under a real uvicorn server on loopback ports (not TestClient), because
the thing under test is a second listener inside the same process. Everything is observed at the
public boundary: HTTP responses, /info, the processing log line, and what the (fake) model saw.
"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import re
import socket
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import server  # noqa: E402

ENV_KEYS = [
    "RERANK_BIND",
    "RERANK_PORT",
    "RERANK_BATCH_SIZE",
    "RERANK_AUX_PORT",
    "RERANK_AUX_BATCH_SIZE",
    "MAX_QUEUE_DEPTH",
    "RERANK_MAX_TEXTS",
    "RERANK_QUEUE_TIMEOUT_SECONDS",
    "RERANK_TOTAL_DEADLINE_SECONDS",
    "RERANK_MAX_LENGTH",
]

OLD_INFO_KEYS = {"model_id", "device", "degraded", "dtype", "revision", "queue_depth"}


# ---------------------------------------------------------------------------
# Fakes and harness
# ---------------------------------------------------------------------------


class FakeModels:
    """Stands in for CrossEncoder. Records every construction and every real predict call."""

    def __init__(self):
        self.created = 0
        self.calls: list[tuple[int, int]] = []  # (pairs, batch_size) of non-warmup calls
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.delay = 0.0

    def __call__(self, *args, **kwargs):
        self.created += 1
        owner = self

        class Model:
            def __init__(self):
                self.model = SimpleNamespace(half=lambda: None)

            def predict(self, pairs, batch_size=32):
                if pairs and pairs[0][0] == "warmup query":
                    return [0.0 for _ in pairs]
                owner.calls.append((len(pairs), batch_size))
                owner.entered.set()
                owner.release.wait(timeout=20)
                if owner.delay:
                    time.sleep(owner.delay)
                return [0.5 for _ in pairs]

        return Model()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sidecar:
    def __init__(self, primary: int, aux: int | None, fake: FakeModels, uv: uvicorn.Server):
        self.fake = fake
        self.uv = uv
        self.ports = {"primary": primary, "aux": aux}
        self.exit_code: int | None = None
        self.thread: threading.Thread | None = None

    def client(self, which: str) -> httpx.Client:
        return httpx.Client(base_url=f"http://127.0.0.1:{self.ports[which]}", timeout=30)


def _set_env(monkeypatch, env: dict[str, str]) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@contextlib.contextmanager
def running_sidecar(monkeypatch, env: dict[str, str] | None = None, *, with_aux: bool = True,
                    expect_start: bool = True):
    primary = _free_port()
    aux = _free_port() if with_aux else None
    full_env = dict(env or {})
    full_env["RERANK_PORT"] = str(primary)
    if aux is not None:
        full_env.setdefault("RERANK_AUX_PORT", str(aux))
        aux = int(full_env["RERANK_AUX_PORT"])
    _set_env(monkeypatch, full_env)
    fake = FakeModels()
    monkeypatch.setattr(server, "CrossEncoder", fake)
    config = uvicorn.Config(server.app, host="127.0.0.1", port=primary, log_config=None, lifespan="on")
    uv = uvicorn.Server(config)
    sidecar = Sidecar(primary, aux, fake, uv)

    def run():
        try:
            uv.run()
        except SystemExit as exc:
            sidecar.exit_code = exc.code if isinstance(exc.code, int) else 1

    sidecar.thread = threading.Thread(target=run, daemon=True)
    sidecar.thread.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not uv.started and sidecar.thread.is_alive():
        time.sleep(0.02)
    if expect_start:
        assert uv.started, "sidecar did not start"
    try:
        yield sidecar
    finally:
        fake.release.set()
        uv.should_exit = True
        sidecar.thread.join(timeout=20)


def _rerank(client: httpx.Client, n: int = 7, query: str = "q"):
    return client.post("/rerank", json={"query": query, "texts": [f"t{i}" for i in range(n)]})


def _processed_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("rerank_processed")]


# ---------------------------------------------------------------------------
# Aux settings unset: today's behavior
# ---------------------------------------------------------------------------


def test_without_aux_settings_info_log_and_batch_are_unchanged(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="rerank_server")
    with running_sidecar(monkeypatch, {"RERANK_BATCH_SIZE": "4"}, with_aux=False) as sc:
        with sc.client("primary") as c:
            info = c.get("/info").json()
            resp = _rerank(c, n=7)
    assert set(info) == OLD_INFO_KEYS
    assert resp.status_code == 200
    assert sorted(item["index"] for item in resp.json()) == list(range(7))
    assert sc.fake.calls == [(7, 4)]
    lines = _processed_lines(caplog)
    assert len(lines) == 1
    assert re.fullmatch(
        r"rerank_processed candidates=7 chars=\d+ batches=2 elapsed_ms=[\d.]+ device=\w+", lines[0]
    )


# ---------------------------------------------------------------------------
# Aux settings validation (startup refusal)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"RERANK_AUX_PORT": "18782"}, id="aux-port-without-batch"),
        pytest.param({"RERANK_AUX_BATCH_SIZE": "16"}, id="aux-batch-without-port"),
        pytest.param(
            {"RERANK_PORT": "18781", "RERANK_AUX_PORT": "18781", "RERANK_AUX_BATCH_SIZE": "16"},
            id="aux-port-equals-primary",
        ),
        pytest.param({"RERANK_AUX_PORT": "18782", "RERANK_AUX_BATCH_SIZE": "0"}, id="aux-batch-zero"),
        pytest.param({"RERANK_AUX_PORT": "18782", "RERANK_AUX_BATCH_SIZE": "-4"}, id="aux-batch-negative"),
        pytest.param({"RERANK_AUX_PORT": "0", "RERANK_AUX_BATCH_SIZE": "16"}, id="aux-port-zero"),
        pytest.param({"RERANK_AUX_PORT": "70000", "RERANK_AUX_BATCH_SIZE": "16"}, id="aux-port-too-large"),
        pytest.param(
            {"RERANK_BIND": "0.0.0.0", "RERANK_AUX_PORT": "18782", "RERANK_AUX_BATCH_SIZE": "16"},
            id="non-loopback-bind-with-aux",
        ),
    ],
)
def test_invalid_aux_settings_refuse_startup(monkeypatch, env):
    _set_env(monkeypatch, env)
    with pytest.raises(SystemExit):
        server.load_settings()


@pytest.mark.parametrize("bind", ["127.0.0.1", "::1", "localhost"])
def test_valid_aux_settings_are_accepted_on_loopback_binds(monkeypatch, bind):
    _set_env(
        monkeypatch,
        {"RERANK_BIND": bind, "RERANK_AUX_PORT": "18782", "RERANK_AUX_BATCH_SIZE": "16"},
    )
    settings = server.load_settings()
    assert (settings.aux_port, settings.aux_batch_size) == (18782, 16)


def test_aux_batch_equal_to_primary_is_allowed_for_a_a_control(monkeypatch):
    _set_env(
        monkeypatch,
        {"RERANK_BATCH_SIZE": "32", "RERANK_AUX_PORT": "18782", "RERANK_AUX_BATCH_SIZE": "32"},
    )
    assert server.load_settings().aux_batch_size == 32


# ---------------------------------------------------------------------------
# Aux settings present: per-port batch size, shared engine
# ---------------------------------------------------------------------------


AUX_ENV = {"RERANK_BATCH_SIZE": "4", "RERANK_AUX_BATCH_SIZE": "2"}


@pytest.mark.parametrize(
    "via, expected_batch, expected_batches",
    [("primary", 4, 2), ("aux", 2, 4)],
)
def test_each_port_infers_with_its_own_batch_size(monkeypatch, caplog, via, expected_batch, expected_batches):
    caplog.set_level(logging.INFO, logger="rerank_server")
    with running_sidecar(monkeypatch, AUX_ENV) as sc:
        with sc.client(via) as c:
            resp = _rerank(c, n=7)
        port = sc.ports[via]
    assert resp.status_code == 200
    assert len(resp.json()) == 7
    assert sc.fake.calls == [(7, expected_batch)]
    lines = _processed_lines(caplog)
    assert len(lines) == 1
    assert f"batches={expected_batches}" in lines[0]
    assert lines[0].endswith(f"port={port} batch_size={expected_batch}")


def test_model_is_loaded_once_with_aux_port(monkeypatch):
    with running_sidecar(monkeypatch, AUX_ENV) as sc:
        with sc.client("primary") as p, sc.client("aux") as a:
            assert _rerank(p).status_code == 200
            assert _rerank(a).status_code == 200
    # one CrossEncoder construction in total (no fallback path taken either)
    assert sc.fake.created == 1


@pytest.mark.parametrize("via", ["primary", "aux"])
def test_info_reports_per_port_batch_sizes_and_keeps_old_fields(monkeypatch, via):
    with running_sidecar(monkeypatch, AUX_ENV) as sc:
        with sc.client(via) as c:
            info = c.get("/info").json()
        primary, aux = sc.ports["primary"], sc.ports["aux"]
    assert OLD_INFO_KEYS <= set(info)
    assert info["batch_sizes"] == {str(primary): 4, str(aux): 2}
    assert info["batch_size"] == (4 if via == "primary" else 2)


@pytest.mark.parametrize("via", ["primary", "aux"])
def test_health_and_unaffected_routes_work_on_both_ports(monkeypatch, via):
    with running_sidecar(monkeypatch, AUX_ENV) as sc:
        with sc.client(via) as c:
            health = c.get("/health")
            empty = c.post("/rerank", json={"query": "q", "texts": []})
            invalid = c.post("/rerank", json={"query": "q"})
    assert (health.status_code, health.text) == (200, "ok")
    assert empty.json() == []
    assert invalid.status_code == 422
    assert invalid.json() == {"error": "rerank_invalid_request"}
    assert sc.fake.calls == []


# ---------------------------------------------------------------------------
# Same limits on both ports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("via", ["primary", "aux"])
def test_max_texts_limit_is_shared(monkeypatch, via):
    with running_sidecar(monkeypatch, {**AUX_ENV, "RERANK_MAX_TEXTS": "5"}) as sc:
        with sc.client(via) as c:
            ok = _rerank(c, n=5)
            too_many = _rerank(c, n=6)
    assert ok.status_code == 200
    assert too_many.status_code == 413
    assert too_many.json() == {"error": "rerank_too_many_texts", "max": 5}


@pytest.mark.parametrize("via", ["primary", "aux"])
def test_total_deadline_is_shared(monkeypatch, via):
    env = {**AUX_ENV, "RERANK_TOTAL_DEADLINE_SECONDS": "0.3", "RERANK_QUEUE_TIMEOUT_SECONDS": "0.3"}
    with running_sidecar(monkeypatch, env) as sc:
        sc.fake.delay = 1.0
        with sc.client(via) as c:
            resp = _rerank(c, n=3)
        assert resp.status_code == 503
        assert resp.json()["error"] == "rerank_queue_timeout"


@pytest.mark.parametrize(
    "fill, overflow_via",
    [
        pytest.param(["primary", "aux", "primary"], "aux", id="p-a-p-then-aux"),
        pytest.param(["aux", "primary", "aux"], "primary", id="a-p-a-then-primary"),
        pytest.param(["primary", "primary", "aux"], "aux", id="p-p-a-then-aux"),
        pytest.param(["aux", "aux", "primary"], "primary", id="a-a-p-then-primary"),
    ],
)
def test_queue_depth_cap_is_one_budget_across_both_ports(monkeypatch, fill, overflow_via):
    """MAX_QUEUE_DEPTH=2 admits 1 in flight + 2 waiting in total. Fill that budget alternating
    ports, then the next request from either port is rejected as queue-full."""
    env = {
        **AUX_ENV,
        "MAX_QUEUE_DEPTH": "2",
        "RERANK_TOTAL_DEADLINE_SECONDS": "30",
        "RERANK_QUEUE_TIMEOUT_SECONDS": "30",
    }
    with running_sidecar(monkeypatch, env) as sc:
        sc.fake.release.clear()
        results: list[httpx.Response] = []
        threads: list[threading.Thread] = []

        def send(which: str):
            with sc.client(which) as c:
                results.append(_rerank(c, n=3))

        def depth() -> int:
            with sc.client("primary") as c:
                return c.get("/info").json()["queue_depth"]

        for i, which in enumerate(fill):
            t = threading.Thread(target=send, args=(which,))
            t.start()
            threads.append(t)
            if i == 0:
                assert sc.fake.entered.wait(timeout=10)  # first job is now in flight
            else:
                end = time.monotonic() + 10
                while depth() < i and time.monotonic() < end:
                    time.sleep(0.02)
                assert depth() == i

        with sc.client(overflow_via) as c:
            rejected = _rerank(c, n=3)
        assert rejected.status_code == 503
        assert rejected.json() == {"error": "rerank_queue_full", "queue_depth": 2}

        sc.fake.release.set()
        for t in threads:
            t.join(timeout=20)
    assert sorted(r.status_code for r in results) == [200, 200, 200]
    # all three admitted jobs went through the one serial worker, each with its own port's batch size
    expected = sorted(4 if which == "primary" else 2 for which in fill)
    assert sorted(bs for _, bs in sc.fake.calls) == expected


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_aux_port_already_in_use_refuses_startup(monkeypatch):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    busy = blocker.getsockname()[1]
    try:
        with running_sidecar(
            monkeypatch,
            {**AUX_ENV, "RERANK_AUX_PORT": str(busy)},
            expect_start=False,
        ) as sc:
            sc.thread.join(timeout=20)
            assert not sc.thread.is_alive()
            assert not sc.uv.started
            assert sc.exit_code not in (None, 0)
    finally:
        blocker.close()


def test_aux_listener_is_closed_on_shutdown(monkeypatch):
    with running_sidecar(monkeypatch, AUX_ENV) as sc:
        with sc.client("aux") as c:
            assert c.get("/health").status_code == 200
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", sc.ports["aux"]), timeout=2).close()


def test_shutdown_is_bounded_when_a_client_stalls_on_the_aux_port(monkeypatch):
    """A connection that sent a partial request and then went silent must not hold the
    lifespan shutdown (and so the process) past the aux drain bound."""
    monkeypatch.setattr(server, "_AUX_DRAIN_SECONDS", 1.0)
    monkeypatch.setattr(server, "_AUX_CANCEL_MARGIN_SECONDS", 1.0)
    with running_sidecar(monkeypatch, AUX_ENV) as sc:
        stalled = socket.create_connection(("127.0.0.1", sc.ports["aux"]), timeout=5)
        try:
            stalled.sendall(
                b"POST /rerank HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                b"Content-Length: 100\r\n\r\n{"
            )
            time.sleep(0.3)  # let the server register the half-sent request
            started = time.monotonic()
            sc.uv.should_exit = True
            sc.thread.join(timeout=8)
            took = time.monotonic() - started
            assert not sc.thread.is_alive(), "shutdown hung on the stalled aux connection"
            assert took < 6
        finally:
            stalled.close()


def test_engine_stops_even_if_the_aux_server_task_died_with_an_error(monkeypatch):
    stopped: list[bool] = []
    real_stop = server.RerankEngine.stop

    async def recording_stop(self):
        stopped.append(True)
        await real_stop(self)

    async def boom(self):
        raise RuntimeError("aux main loop died")

    monkeypatch.setattr(server.RerankEngine, "stop", recording_stop)
    monkeypatch.setattr(server._AuxServer, "main_loop", boom)
    with running_sidecar(monkeypatch, AUX_ENV) as sc:
        time.sleep(0.3)  # the aux task has already failed
    assert stopped == [True]
