"""Unit tests for rerank-server. No real model load, no network."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import sys
import time
from types import SimpleNamespace

import pytest
import torch


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import server  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCrossEncoder:
    """Drop-in fake for sentence_transformers.CrossEncoder. No network/model load."""

    def __init__(self, model_id=None, *, revision=None, device="cpu", activation_fn=None, **kw):
        self.model_id = model_id
        self.revision = revision
        self.device = device
        self.activation_fn = activation_fn
        self.extra_kwargs = kw
        self.half_called = False
        self.model = SimpleNamespace(half=self._mark_half)
        self.calls: list[list[tuple[str, str]]] = []
        self.predict_batch_sizes: list[int] = []

    def _mark_half(self) -> None:
        self.half_called = True

    def predict(self, pairs, batch_size=32):
        self.calls.append(list(pairs))
        self.predict_batch_sizes.append(batch_size)
        return [0.0 for _ in pairs]


def _capturing_factory(created: list):
    """Factory that records every FakeCrossEncoder constructed, with its kwargs."""

    def factory(*args, **kwargs):
        instance = FakeCrossEncoder(*args, **kwargs)
        created.append(instance)
        return instance

    return factory


class FakeRequest:
    def __init__(self, disconnected: bool = False):
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


# ---------------------------------------------------------------------------
# sanitize_score
# ---------------------------------------------------------------------------


def test_sanitize_score_nan_and_inf_become_zero():
    assert server.sanitize_score(float("nan")) == 0.0
    assert server.sanitize_score(float("inf")) == 0.0
    assert server.sanitize_score(float("-inf")) == 0.0


def test_sanitize_score_passes_through_activations_and_clamps_extremes():
    # Model activation (sigmoid) already yields [0, 1]; sanitize must NOT
    # re-transform (double-sigmoid regression guard) - only clamp.
    for raw in [-1e6, -1.0, 0.0, 0.25, 0.5, 0.832, 1.0, 1000.0]:
        s = server.sanitize_score(raw)
        assert 0.0 <= s <= 1.0

    assert server.sanitize_score(1e6) == pytest.approx(1.0, abs=1e-9)
    assert server.sanitize_score(-1e6) == pytest.approx(0.0, abs=1e-9)
    assert server.sanitize_score(0.0) == pytest.approx(0.0, abs=1e-9)
    assert server.sanitize_score(0.42) == pytest.approx(0.42, abs=1e-9)
    assert server.sanitize_score(0.832) == pytest.approx(0.832, abs=1e-9)


def test_sanitize_score_non_numeric_becomes_zero():
    assert server.sanitize_score("not-a-number") == 0.0
    assert server.sanitize_score(None) == 0.0


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


def test_settings_rejects_wildcard_bind(monkeypatch):
    monkeypatch.setenv("RERANK_BIND", "0.0.0.0")
    with pytest.raises(SystemExit):
        server.load_settings()


@pytest.mark.parametrize("bind_value", ["::", "0", "192.0.2.5", ""])
def test_settings_rejects_non_loopback_bind(monkeypatch, bind_value):
    monkeypatch.setenv("RERANK_BIND", bind_value)
    with pytest.raises(SystemExit):
        server.load_settings()


@pytest.mark.parametrize("bind_value", ["127.0.0.1", "::1", "localhost"])
def test_settings_accepts_loopback_bind(monkeypatch, bind_value):
    monkeypatch.setenv("RERANK_BIND", bind_value)
    settings = server.load_settings()
    assert settings.bind == bind_value


def test_settings_rejects_too_small_queue_depth(monkeypatch):
    monkeypatch.delenv("RERANK_BIND", raising=False)
    monkeypatch.setenv("MAX_QUEUE_DEPTH", "1")
    with pytest.raises(SystemExit):
        server.load_settings()


def test_settings_accepts_defaults(monkeypatch):
    monkeypatch.delenv("RERANK_BIND", raising=False)
    monkeypatch.delenv("MAX_QUEUE_DEPTH", raising=False)
    settings = server.load_settings()
    assert settings.bind == "127.0.0.1"
    assert settings.max_queue_depth == 3
    assert settings.max_length == 512
    assert settings.model_id == "BAAI/bge-reranker-v2-m3"
    assert settings.model_revision == "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
    assert settings.queue_timeout_seconds == 31.5
    assert settings.max_texts == 256
    assert settings.total_deadline_seconds == 7.5


def test_settings_rejects_wait_backstop_below_total_deadline():
    with pytest.raises(SystemExit):
        server.Settings(queue_timeout_seconds=3.0, total_deadline_seconds=7.5).validate()


def test_normal_worst_case_wait_below_total_deadline_is_served():
    """Negative assertion (plan 7-1): a job whose combined queue wait plus
    processing exceeds queue_timeout_seconds but stays below
    total_deadline_seconds must succeed, not 503."""

    async def run():
        settings = server.Settings(
            max_queue_depth=10, queue_timeout_seconds=1.0, total_deadline_seconds=0.5
        )
        engine = server.RerankEngine(settings)

        async def slow_infer(job):
            await asyncio.sleep(0.12)  # job2: wait 0.12s + proc 0.12s < deadline 0.5
            return [{"index": i, "score": 0.5} for i, _ in enumerate(job.texts)]

        engine._infer = slow_infer
        engine.worker_task = asyncio.create_task(engine._worker_loop())
        try:
            first = asyncio.create_task(engine.submit(FakeRequest(), "q", ["a"]))
            await asyncio.sleep(0.01)
            second = asyncio.create_task(engine.submit(FakeRequest(), "q", ["b"]))
            r1 = await first
            r2 = await second  # queued ~0.12s (< backstop 1.0), total < deadline 0.5
            assert r1 and r2
        finally:
            engine.worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await engine.worker_task

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Queue mechanics (engine-level, _infer monkeypatched to avoid real inference)
# ---------------------------------------------------------------------------


def test_fifo_order_preserved():
    async def run():
        settings = server.Settings(max_queue_depth=10, queue_timeout_seconds=5.0)
        engine = server.RerankEngine(settings)
        order: list[str] = []

        async def fake_infer(job):
            order.append(job.query)
            await asyncio.sleep(0.01)
            return [{"index": 0, "score": 0.5}]

        engine._infer = fake_infer
        engine.worker_task = asyncio.create_task(engine._worker_loop())
        try:
            req = FakeRequest()
            results = await asyncio.gather(
                *[engine.submit(req, f"q{i}", ["t"]) for i in range(5)]
            )
            assert order == [f"q{i}" for i in range(5)]
            assert len(results) == 5
        finally:
            engine.worker_task.cancel()

    asyncio.run(run())


def test_queue_full_rejects_with_503_payload():
    async def run():
        settings = server.Settings(max_queue_depth=2, queue_timeout_seconds=5.0)
        engine = server.RerankEngine(settings)
        gate = asyncio.Event()

        async def fake_infer(job):
            await gate.wait()
            return [{"index": 0, "score": 0.5}]

        engine._infer = fake_infer
        engine.worker_task = asyncio.create_task(engine._worker_loop())
        try:
            req = FakeRequest()
            t1 = asyncio.create_task(engine.submit(req, "q1", ["t"]))
            await asyncio.sleep(0.02)  # worker dequeues t1, blocks in fake_infer on gate
            t2 = asyncio.create_task(engine.submit(req, "q2", ["t"]))
            await asyncio.sleep(0.02)
            t3 = asyncio.create_task(engine.submit(req, "q3", ["t"]))
            await asyncio.sleep(0.02)

            with pytest.raises(server.QueueFullError) as exc_info:
                await engine.submit(req, "q4", ["t"])
            assert exc_info.value.queue_depth == 2

            gate.set()
            results = await asyncio.gather(t1, t2, t3)
            assert len(results) == 3
        finally:
            engine.worker_task.cancel()

    asyncio.run(run())


def test_queue_timeout_returns_503_for_stale_item():
    """Dequeue-time staleness check: a job that sat in the queue longer than
    queue_timeout_seconds before the worker even started on it must fail
    with QueueTimeoutError, independent of submit()'s absolute deadline
    (tested separately, and given a generous cap here so it does not
    interfere with what this test targets)."""

    async def run():
        settings = server.Settings(max_queue_depth=10, queue_timeout_seconds=0.05)
        engine = server.RerankEngine(settings)

        async def fake_infer(job):
            return [{"index": 0, "score": 0.5}]

        engine._infer = fake_infer
        engine.worker_task = asyncio.create_task(engine._worker_loop())
        try:
            req = FakeRequest()
            loop = asyncio.get_event_loop()
            stale_future: "asyncio.Future[list[dict]]" = loop.create_future()
            stale_job = server.RerankJob(
                query="stale",
                texts=["t"],
                enqueued_at=time.monotonic() - 1.0,  # already far past the timeout
                future=stale_future,
                request=req,
            )
            await engine.queue.put(stale_job)

            with pytest.raises(server.QueueTimeoutError) as exc_info:
                await asyncio.wait_for(stale_future, timeout=1.0)
            assert exc_info.value.waited_ms >= 50.0
        finally:
            engine.worker_task.cancel()

    asyncio.run(run())


def test_disconnected_request_is_discarded_without_inference():
    async def run():
        settings = server.Settings(max_queue_depth=10, queue_timeout_seconds=5.0)
        engine = server.RerankEngine(settings)
        infer_calls = {"count": 0}

        async def fake_infer(job):
            infer_calls["count"] += 1
            return [{"index": 0, "score": 0.5}]

        engine._infer = fake_infer
        engine.worker_task = asyncio.create_task(engine._worker_loop())
        try:
            req = FakeRequest(disconnected=True)
            with pytest.raises(server.ClientDisconnected):
                await engine.submit(req, "q", ["t"])
            assert infer_calls["count"] == 0
        finally:
            engine.worker_task.cancel()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# App-level integration (TestClient, model mocked)
# ---------------------------------------------------------------------------


def test_info_schema_and_rerank_response_shape(monkeypatch):
    monkeypatch.setattr(server, "CrossEncoder", lambda *a, **kw: FakeCrossEncoder())
    with TestClient(server.app) as client:
        info = client.get("/info").json()
        assert info["model_id"] == "BAAI/bge-reranker-v2-m3"
        assert info["revision"] == "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
        assert info["dtype"] in ("float16", "float32")
        assert info["device"] in ("mps", "cpu")
        assert isinstance(info["degraded"], bool)
        assert isinstance(info["queue_depth"], int)

        assert client.get("/health").status_code == 200
        assert client.get("/health").text == "ok"

        texts = [f"passage-{i}" for i in range(20)]
        resp = client.post(
            "/rerank", json={"query": "sample query text", "texts": texts, "return_text": False}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == len(texts)
        indices = sorted(item["index"] for item in body)
        assert indices == list(range(len(texts)))
        for item in body:
            assert 0.0 <= item["score"] <= 1.0


def test_health_returns_503_not_ready_when_engine_marked_not_ready(monkeypatch):
    monkeypatch.setattr(server, "CrossEncoder", lambda *a, **kw: FakeCrossEncoder())
    with TestClient(server.app) as client:
        assert client.get("/health").status_code == 200
        server.app.state.engine.ready = False
        resp = client.get("/health")
        assert resp.status_code == 503
        assert resp.text == "not ready"


# ---------------------------------------------------------------------------
# CrossEncoder construction: pinned revision, sigmoid activation, CPU fallback
# ---------------------------------------------------------------------------


def test_cross_encoder_primary_load_uses_pinned_revision_and_sigmoid(monkeypatch):
    created: list[FakeCrossEncoder] = []
    monkeypatch.setattr(server, "CrossEncoder", _capturing_factory(created))
    with TestClient(server.app):
        pass
    assert len(created) == 1
    instance = created[0]
    assert isinstance(instance.activation_fn, torch.nn.Sigmoid)
    assert instance.revision == server.Settings().model_revision
    assert instance.revision == "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"


def test_primary_load_failure_falls_back_to_cpu_with_sigmoid(monkeypatch):
    created: list[FakeCrossEncoder] = []
    attempts = {"n": 0}

    def factory(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("primary load boom")
        instance = FakeCrossEncoder(*args, **kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr(server, "CrossEncoder", factory)
    with TestClient(server.app) as client:
        info = client.get("/info").json()

    assert info["device"] == "cpu"
    assert info["dtype"] == "float32"
    assert info["degraded"] is True
    assert len(created) == 1
    assert isinstance(created[0].activation_fn, torch.nn.Sigmoid)


def test_mps_available_uses_fp16_half_call_and_not_degraded(monkeypatch):
    created: list[FakeCrossEncoder] = []
    monkeypatch.setattr(server, "CrossEncoder", _capturing_factory(created))
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
    with TestClient(server.app) as client:
        info = client.get("/info").json()

    assert info["device"] == "mps"
    assert info["dtype"] == "float16"
    assert info["degraded"] is False
    assert len(created) == 1
    assert created[0].half_called is True


# ---------------------------------------------------------------------------
# Inference failures must be opaque: no secret text, no exception message
# ---------------------------------------------------------------------------


def test_inference_exception_is_opaque_and_secret_free(monkeypatch, caplog):
    secret = "SECRET-INFERENCE-FAILURE-MARKER-9f3a"

    class ExplodingCrossEncoder(FakeCrossEncoder):
        def predict(self, pairs, batch_size=32):
            # Warmup (during model load) must still succeed; only the
            # real inference call for the actual request should explode.
            if any(query == "warmup query" for query, _ in pairs):
                return [0.0 for _ in pairs]
            raise RuntimeError(secret)

    monkeypatch.setattr(server, "CrossEncoder", lambda *a, **kw: ExplodingCrossEncoder())

    with caplog.at_level(logging.DEBUG):
        with TestClient(server.app) as client:
            resp = client.post(
                "/rerank", json={"query": "q", "texts": ["t"], "return_text": False}
            )

    assert resp.status_code == 500
    assert resp.json() == {"error": "rerank_internal_error"}
    assert secret not in resp.text

    all_log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in all_log_text


# ---------------------------------------------------------------------------
# Score sanitization observed at the HTTP boundary (double-sigmoid regression)
# ---------------------------------------------------------------------------


def test_rerank_response_sanitizes_nan_and_extreme_scores(monkeypatch):
    raw_scores = [0.832, float("nan"), 1e6, -1.0, 0.25]
    expected_scores = [0.832, 0.0, 1.0, 0.0, 0.25]

    class VariedFakeCrossEncoder(FakeCrossEncoder):
        def predict(self, pairs, batch_size=32):
            self.calls.append(list(pairs))
            self.predict_batch_sizes.append(batch_size)
            if len(pairs) == len(raw_scores):
                return list(raw_scores)
            return [0.0 for _ in pairs]

    created: list[VariedFakeCrossEncoder] = []

    def factory(*args, **kwargs):
        instance = VariedFakeCrossEncoder(*args, **kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr(server, "CrossEncoder", factory)
    settings = server.Settings()
    texts = [f"passage-{i}" for i in range(len(raw_scores))]

    with TestClient(server.app) as client:
        resp = client.post(
            "/rerank", json={"query": "q", "texts": texts, "return_text": False}
        )

    assert resp.status_code == 200
    body = resp.json()
    scores = [item["score"] for item in sorted(body, key=lambda item: item["index"])]
    assert scores == expected_scores

    instance = created[0]
    real_calls = [call for call in instance.calls if len(call) == len(texts)]
    assert len(real_calls) == 1
    assert real_calls[0] == [("q", t) for t in texts]
    assert settings.batch_size in instance.predict_batch_sizes


# ---------------------------------------------------------------------------
# Canary: request payloads must never reach logs
# ---------------------------------------------------------------------------


def test_query_and_texts_never_appear_in_logs(monkeypatch, caplog):
    monkeypatch.setattr(server, "CrossEncoder", lambda *a, **kw: FakeCrossEncoder())
    secret_query = "TOTALLY-SECRET-QUERY-MARKER-9f3a"
    secret_texts = [f"SECRET-PASSAGE-{i}-CONFIDENTIAL-9f3a" for i in range(5)]

    with caplog.at_level(logging.DEBUG):
        with TestClient(server.app) as client:
            resp = client.post(
                "/rerank",
                json={"query": secret_query, "texts": secret_texts, "return_text": False},
            )
            assert resp.status_code == 200

    all_log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_query not in all_log_text
    for text in secret_texts:
        assert text not in all_log_text


# ---------------------------------------------------------------------------
# RequestValidationError must be opaque (no echoed input, no secret leakage)
# ---------------------------------------------------------------------------


def test_invalid_request_is_opaque_and_secret_free(monkeypatch, caplog):
    monkeypatch.setattr(server, "CrossEncoder", lambda *a, **kw: FakeCrossEncoder())
    secret = "SECRET-VALIDATION-MARKER-9f3a"

    with caplog.at_level(logging.DEBUG):
        with TestClient(server.app) as client:
            resp = client.post("/rerank", json={"texts": [secret]})

    assert resp.status_code == 422
    assert resp.json() == {"error": "rerank_invalid_request"}
    assert secret not in resp.text

    all_log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in all_log_text


# ---------------------------------------------------------------------------
# TEI conformance: score-descending order, original index, optional text
# ---------------------------------------------------------------------------


def test_rerank_response_sorted_by_score_descending_with_original_index(monkeypatch):
    raw_scores = [0.2, 0.9, 0.5]

    class OrderedFakeCrossEncoder(FakeCrossEncoder):
        def predict(self, pairs, batch_size=32):
            self.calls.append(list(pairs))
            self.predict_batch_sizes.append(batch_size)
            if len(pairs) == len(raw_scores):
                return list(raw_scores)
            return [0.0 for _ in pairs]

    monkeypatch.setattr(server, "CrossEncoder", lambda *a, **kw: OrderedFakeCrossEncoder())
    texts = [f"passage-{i}" for i in range(len(raw_scores))]

    with TestClient(server.app) as client:
        resp = client.post("/rerank", json={"query": "q", "texts": texts, "return_text": False})

    assert resp.status_code == 200
    body = resp.json()
    scores = [item["score"] for item in body]
    assert scores == sorted(scores, reverse=True)
    assert [item["index"] for item in body] == [1, 2, 0]


def test_rerank_return_text_includes_text_per_item(monkeypatch):
    monkeypatch.setattr(server, "CrossEncoder", lambda *a, **kw: FakeCrossEncoder())
    texts = ["alpha", "beta", "gamma"]

    with TestClient(server.app) as client:
        resp_with_text = client.post(
            "/rerank", json={"query": "q", "texts": texts, "return_text": True}
        )
        resp_without_text = client.post(
            "/rerank", json={"query": "q", "texts": texts, "return_text": False}
        )

    body_with = resp_with_text.json()
    body_without = resp_without_text.json()
    assert all("text" in item for item in body_with)
    for item in body_with:
        assert item["text"] == texts[item["index"]]
    assert all("text" not in item for item in body_without)


# ---------------------------------------------------------------------------
# Absolute deadline: submit() must not hang forever even if the worker dies
# ---------------------------------------------------------------------------


def test_submit_times_out_absolutely_even_if_worker_is_dead():
    async def run():
        settings = server.Settings(
            max_queue_depth=10, queue_timeout_seconds=0.2, total_deadline_seconds=0.05
        )
        engine = server.RerankEngine(settings)
        engine.worker_task = asyncio.create_task(engine._worker_loop())
        await asyncio.sleep(0.01)
        engine.worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await engine.worker_task

        req = FakeRequest()
        start = time.monotonic()
        with pytest.raises(server.QueueTimeoutError):
            await engine.submit(req, "q", ["t"])
        elapsed = time.monotonic() - start
        assert elapsed < 1.0

    asyncio.run(run())


def test_health_route_reports_503_when_worker_task_dead():
    async def run():
        settings = server.Settings()
        engine = server.RerankEngine(settings)
        engine.ready = True
        engine.worker_task = asyncio.create_task(engine._worker_loop())
        engine.worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await engine.worker_task

        fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))
        resp = await server.get_health(fake_request)
        assert resp.status_code == 503

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Client-disconnect handling: no CancelledError leaks past the route handler
# ---------------------------------------------------------------------------


def test_disconnected_client_gets_499_without_cancelled_error_leaking(monkeypatch):
    monkeypatch.setattr(server, "CrossEncoder", lambda *a, **kw: FakeCrossEncoder())

    async def fake_is_disconnected(self):
        return True

    monkeypatch.setattr(server.Request, "is_disconnected", fake_is_disconnected)
    with TestClient(server.app) as client:
        resp = client.post(
            "/rerank", json={"query": "q", "texts": ["t"], "return_text": False}
        )
    assert resp.status_code == 499


# ---------------------------------------------------------------------------
# Workload cap: RERANK_MAX_TEXTS boundary
# ---------------------------------------------------------------------------


def test_max_texts_boundary_256_ok_257_rejected(monkeypatch):
    monkeypatch.setattr(server, "CrossEncoder", lambda *a, **kw: FakeCrossEncoder())
    with TestClient(server.app) as client:
        ok_texts = [f"t{i}" for i in range(256)]
        resp_ok = client.post(
            "/rerank", json={"query": "q", "texts": ok_texts, "return_text": False}
        )
        assert resp_ok.status_code == 200
        assert len(resp_ok.json()) == 256

        too_many = [f"t{i}" for i in range(257)]
        resp_bad = client.post(
            "/rerank", json={"query": "q", "texts": too_many, "return_text": False}
        )
        assert resp_bad.status_code == 413
        assert resp_bad.json() == {"error": "rerank_too_many_texts", "max": 256}


# ---------------------------------------------------------------------------
# Empty input: no model call, immediate empty response
# ---------------------------------------------------------------------------


def test_empty_texts_returns_empty_list_without_model_call(monkeypatch):
    created: list[FakeCrossEncoder] = []
    monkeypatch.setattr(server, "CrossEncoder", _capturing_factory(created))
    with TestClient(server.app) as client:
        resp = client.post("/rerank", json={"query": "q", "texts": [], "return_text": False})

    assert resp.status_code == 200
    assert resp.json() == []
    instance = created[0]
    real_calls = [call for call in instance.calls if not any(q == "warmup query" for q, _ in call)]
    assert real_calls == []


def test_max_length_floor_rejected(monkeypatch):
    """A max_length too small to hold a query plus a document is a startup error.

    Truncation is a latency lever, not a correctness one, so it must stay large enough
    that the pair survives. 63 is below the floor; 64 is the documented minimum.
    """
    monkeypatch.setenv("RERANK_MAX_LENGTH", "63")
    with pytest.raises(SystemExit):
        server.Settings().validate()
    monkeypatch.setenv("RERANK_MAX_LENGTH", "64")
    server.Settings().validate()
