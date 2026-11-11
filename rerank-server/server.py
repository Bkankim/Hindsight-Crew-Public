"""Mac-native MPS fp16 rerank server, TEI-compatible, for Hindsight-Crew.

Security invariants:
- Binds to loopback only (RERANK_BIND=0.0.0.0 is rejected at startup).
- No authentication (loopback-only trust boundary, see README AC-B2).
- Request payloads (query/texts) are never logged, never written to disk,
  and never included in error messages. Only counts, lengths, timings,
  device, and error codes are logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

# Must be set before transformers/huggingface_hub read it at import time.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import torch  # noqa: E402
import uvicorn  # noqa: E402
from sentence_transformers import CrossEncoder  # noqa: E402
from fastapi import FastAPI, Request, Response  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

LOG = logging.getLogger("rerank_server")
if not LOG.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _env_opt_int(name: str) -> int | None:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else None


@dataclass
class Settings:
    bind: str = field(default_factory=lambda: _env_str("RERANK_BIND", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("RERANK_PORT", 8787))
    model_id: str = field(default_factory=lambda: _env_str("RERANK_MODEL", "BAAI/bge-reranker-v2-m3"))
    model_revision: str = field(
        default_factory=lambda: _env_str(
            "RERANK_MODEL_REVISION", "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
        )
    )
    batch_size: int = field(default_factory=lambda: _env_int("RERANK_BATCH_SIZE", 32))
    # Truncation length for the query+document pair. The model card's own usage example
    # tokenizes with max_length=512, and attention cost is superlinear in sequence
    # length, so leaving this unbounded pays full price for long facts. Measured on the
    # real bank (2026-07-26): 99 candidates 1.66s unbounded vs 1.05s at 512, with
    # spearman 0.9995 and 9/10 of the top ten unchanged - the ordering is preserved.
    max_length: int = field(default_factory=lambda: _env_int("RERANK_MAX_LENGTH", 512))
    max_queue_depth: int = field(default_factory=lambda: _env_int("MAX_QUEUE_DEPTH", 3))
    queue_timeout_seconds: float = field(
        default_factory=lambda: _env_float("RERANK_QUEUE_TIMEOUT_SECONDS", 31.5)
    )
    max_texts: int = field(default_factory=lambda: _env_int("RERANK_MAX_TEXTS", 256))
    total_deadline_seconds: float = field(
        default_factory=lambda: _env_float("RERANK_TOTAL_DEADLINE_SECONDS", 7.5)
    )
    idle_trim_seconds: float = field(default_factory=lambda: _env_float("IDLE_TRIM_SECONDS", 1800.0))
    # Auxiliary port (bench only, issue #78): a second loopback listener in the same process
    # that shares the model, queue and worker but infers with its own batch size. Both must be
    # set, or neither (the default: no second listener, behavior unchanged).
    aux_port: int | None = field(default_factory=lambda: _env_opt_int("RERANK_AUX_PORT"))
    aux_batch_size: int | None = field(default_factory=lambda: _env_opt_int("RERANK_AUX_BATCH_SIZE"))

    def validate(self) -> None:
        if self.bind != "localhost":
            try:
                addr = ipaddress.ip_address(self.bind)
            except ValueError:
                sys.exit(
                    f"RERANK_BIND={self.bind} is forbidden; bind to loopback only (RERANK_BIND)"
                )
            if not addr.is_loopback:
                sys.exit(
                    f"RERANK_BIND={self.bind} is forbidden; bind to loopback only (RERANK_BIND)"
                )
        if (self.aux_port is None) != (self.aux_batch_size is None):
            sys.exit(
                "RERANK_AUX_PORT and RERANK_AUX_BATCH_SIZE must be set together (or both unset)"
            )
        if self.aux_port is not None and self.aux_batch_size is not None:
            # The aux listener binds to self.bind, so the loopback rule above covers it.
            if not 1 <= self.aux_port <= 65535:
                sys.exit(f"RERANK_AUX_PORT={self.aux_port} is not a valid port (1-65535)")
            if self.aux_port == self.port:
                sys.exit(f"RERANK_AUX_PORT={self.aux_port} must differ from RERANK_PORT")
            if self.aux_batch_size < 1:
                sys.exit(f"RERANK_AUX_BATCH_SIZE={self.aux_batch_size} must be >= 1")
        if self.max_queue_depth < 2:
            sys.exit("MAX_QUEUE_DEPTH must be >= 2")

        if self.max_length < 64:
            sys.exit(
                f"RERANK_MAX_LENGTH={self.max_length} is too small to hold a query plus a "
                "document; use at least 64 (default 512)"
            )
        if self.queue_timeout_seconds < self.total_deadline_seconds:
            sys.exit(
                "RERANK_QUEUE_TIMEOUT_SECONDS (wait-phase backstop) must be >= "
                "RERANK_TOTAL_DEADLINE_SECONDS (client-facing absolute bound); a smaller "
                "backstop 503s legitimately queued jobs before the deadline "
                "(plan formula: 1.5 x P_worst x (MAX_QUEUE_DEPTH - 1))"
            )


def load_settings() -> Settings:
    settings = Settings()
    settings.validate()
    return settings


# ---------------------------------------------------------------------------
# Score sanitization
# ---------------------------------------------------------------------------


def sanitize_score(raw: Any) -> float:
    """Sanitize an activation score to a float in [0, 1].

    The CrossEncoder is loaded with an explicit sigmoid activation
    (activation_fn=torch.nn.Sigmoid()), so predict() already returns
    probabilities in [0, 1]. Applying sigmoid again here would compress
    the range to ~[0.5, 0.73] and destroy absolute confidence
    (double-sigmoid bug, caught against the research reference range
    [0.0001, 0.832]). This function therefore only converts, sanitizes
    NaN/inf to 0.0, and clamps to [0, 1].
    """
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(val) or math.isinf(val):
        return 0.0
    return min(1.0, max(0.0, val))


# ---------------------------------------------------------------------------
# Queue errors
# ---------------------------------------------------------------------------


class QueueFullError(Exception):
    def __init__(self, queue_depth: int):
        super().__init__("rerank_queue_full")
        self.queue_depth = queue_depth


class QueueTimeoutError(Exception):
    def __init__(self, waited_seconds: float, queue_depth: int):
        super().__init__("rerank_queue_timeout")
        self.waited_ms = waited_seconds * 1000.0
        self.queue_depth = queue_depth


class ClientDisconnected(Exception):
    """Raised when the worker discards a job because the client is gone.

    Not a cancellation: the request handler is expected to catch this and
    return a normal (if unusual) HTTP response, so CancelledError never has
    to be caught at the route boundary.
    """

    def __init__(self):
        super().__init__("client_disconnected")


class RerankInternalError(Exception):
    """Opaque wrapper for unexpected inference failures.

    The original exception's message is never included here (only its
    type name) so nothing from the request payload or model internals can
    leak into logs or HTTP responses via exception text.
    """

    def __init__(self, exc_type_name: str, code: str = "inference_failed"):
        super().__init__(f"{code} type={exc_type_name}")
        self.code = code


# ---------------------------------------------------------------------------
# Rerank job / engine
# ---------------------------------------------------------------------------


@dataclass
class RerankJob:
    query: str
    texts: list[str]
    enqueued_at: float
    future: "asyncio.Future[list[dict[str, Any]]]"
    request: Any  # duck-typed: has async is_disconnected()
    return_text: bool = False
    # Batch size this job infers with (the port it arrived on); None = settings.batch_size.
    batch_size: int | None = None
    port: int | None = None  # listening port it arrived on; only logged when an aux port is set


class RerankEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.queue: "asyncio.Queue[RerankJob]" = asyncio.Queue()
        self.model: Any = None
        self.device = "cpu"
        self.dtype = "float32"
        self.degraded = False
        self.ready = False
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.worker_task: asyncio.Task | None = None
        self.idle_task: asyncio.Task | None = None
        self.last_request_at = time.monotonic()
        self._trimmed_since_last_request = True

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self.executor, self._load_model)
        self.worker_task = asyncio.create_task(self._worker_loop())
        self.idle_task = asyncio.create_task(self._idle_trim_loop())
        self.ready = True

    async def stop(self) -> None:
        for task in (self.worker_task, self.idle_task):
            if task is not None:
                task.cancel()
        self.executor.shutdown(wait=False)

    def is_worker_alive(self) -> bool:
        return self.worker_task is not None and not self.worker_task.done()

    # -- model lifecycle ---------------------------------------------------

    def _warmup(self, model: Any) -> None:
        dummy_pairs = [("warmup query", "warmup passage")] * 8
        model.predict(dummy_pairs, batch_size=self.settings.batch_size)

    def _load_model(self) -> None:
        mps_available = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
        device = "mps" if mps_available else "cpu"
        degraded = not mps_available
        try:
            model = CrossEncoder(
                self.settings.model_id,
                revision=self.settings.model_revision,
                device=device,
                activation_fn=torch.nn.Sigmoid(),
                max_length=self.settings.max_length,
            )
            if device == "mps":
                model.model.half()
            self._warmup(model)
        except Exception:
            LOG.exception("rerank_model_primary_load_failed device=%s", device)
            device = "cpu"
            degraded = True
            model = CrossEncoder(
                self.settings.model_id,
                revision=self.settings.model_revision,
                device="cpu",
                activation_fn=torch.nn.Sigmoid(),
                max_length=self.settings.max_length,
            )
            self._warmup(model)

        self.model = model
        self.device = device
        self.dtype = "float16" if device == "mps" else "float32"
        self.degraded = degraded
        LOG.info("rerank_model_ready device=%s dtype=%s degraded=%s", self.device, self.dtype, self.degraded)

    # -- request submission --------------------------------------------------

    async def submit(
        self,
        request: Any,
        query: str,
        texts: list[str],
        return_text: bool = False,
        aux: bool = False,
    ) -> list[dict[str, Any]]:
        depth = self.queue.qsize()
        if depth >= self.settings.max_queue_depth:
            LOG.warning("rerank_queue_full queue_depth=%d", depth)
            raise QueueFullError(depth)
        loop = asyncio.get_event_loop()
        future: "asyncio.Future[list[dict[str, Any]]]" = loop.create_future()
        enqueued_at = time.monotonic()
        job = RerankJob(
            query=query,
            texts=texts,
            enqueued_at=enqueued_at,
            future=future,
            request=request,
            return_text=return_text,
            batch_size=self.batch_size_for(aux),
            port=self.port_for(aux),
        )
        await self.queue.put(job)
        # Absolute deadline independent of worker health: if the worker
        # never dequeues the job at all (dead, hung, blocked), this bounds
        # the wait regardless of the dequeue-time staleness check below.
        try:
            return await asyncio.wait_for(future, timeout=self.settings.total_deadline_seconds)
        except asyncio.TimeoutError:
            waited = time.monotonic() - enqueued_at
            depth = self.queue.qsize()
            LOG.warning(
                "rerank_queue_timeout_absolute waited_ms=%.1f queue_depth=%d", waited * 1000.0, depth
            )
            raise QueueTimeoutError(waited, depth) from None

    # -- worker loop --------------------------------------------------------

    async def _worker_loop(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                # Skip jobs whose future is already settled: the submit-side
                # absolute deadline fired (asyncio.wait_for cancelled it) or
                # the caller vanished. Never burn inference on a dead job.
                if job.future.done():
                    LOG.info("rerank_skipped_settled_job")
                    continue

                # Defense-in-depth backstop from the plan formula
                # (1.5 x P_worst x (MAX_QUEUE_DEPTH - 1)). Under the validate()
                # invariant (backstop >= total deadline) this branch is
                # structurally unreachable in the production shape: the
                # submit-side deadline always settles the future first and the
                # settled-job skip above consumes it. Kept deliberately as the
                # last line of defense for non-submit ingress (e.g. direct
                # queue injection in tests or future refactors that bypass
                # submit()); not dead-code-eligible for removal.
                waited = time.monotonic() - job.enqueued_at
                if waited > self.settings.queue_timeout_seconds:
                    depth = self.queue.qsize()
                    LOG.warning("rerank_queue_timeout waited_ms=%.1f queue_depth=%d", waited * 1000.0, depth)
                    job.future.set_exception(QueueTimeoutError(waited, depth))
                    continue

                try:
                    disconnected = await job.request.is_disconnected()
                except Exception:
                    disconnected = False
                if disconnected:
                    LOG.info("rerank_discarded_disconnected")
                    if not job.future.done():
                        job.future.set_exception(ClientDisconnected())
                    continue

                result = await self._infer(job)
                if not job.future.done():
                    job.future.set_result(result)
            except Exception as exc:  # pragma: no cover - defensive
                LOG.error("rerank_worker_error error_type=%s", type(exc).__name__)
                if not job.future.done():
                    job.future.set_exception(RerankInternalError(type(exc).__name__))
            finally:
                self.queue.task_done()

    def batch_size_for(self, aux: bool) -> int:
        if aux and self.settings.aux_batch_size is not None:
            return self.settings.aux_batch_size
        return self.settings.batch_size

    def port_for(self, aux: bool) -> int:
        if aux and self.settings.aux_port is not None:
            return self.settings.aux_port
        return self.settings.port

    async def _infer(self, job: RerankJob) -> list[dict[str, Any]]:
        start = time.monotonic()
        pairs = [(job.query, t) for t in job.texts]
        batch_size = job.batch_size if job.batch_size is not None else self.settings.batch_size
        loop = asyncio.get_event_loop()
        raw_scores = await loop.run_in_executor(self.executor, self._predict, pairs, batch_size)
        scores = [sanitize_score(s) for s in raw_scores]
        elapsed_ms = (time.monotonic() - start) * 1000.0
        self.last_request_at = time.monotonic()
        self._trimmed_since_last_request = False
        total_chars = len(job.query) + sum(len(t) for t in job.texts)
        batches = math.ceil(len(job.texts) / batch_size) if job.texts else 0
        if self.settings.aux_port is None:
            LOG.info(
                "rerank_processed candidates=%d chars=%d batches=%d elapsed_ms=%.1f device=%s",
                len(job.texts),
                total_chars,
                batches,
                elapsed_ms,
                self.device,
            )
        else:
            LOG.info(
                "rerank_processed candidates=%d chars=%d batches=%d elapsed_ms=%.1f device=%s "
                "port=%d batch_size=%d",
                len(job.texts),
                total_chars,
                batches,
                elapsed_ms,
                self.device,
                job.port,
                batch_size,
            )
        results = [{"index": i, "score": s} for i, s in enumerate(scores)]
        if job.return_text:
            for item, text in zip(results, job.texts):
                item["text"] = text
        # TEI-compatible ordering: score descending, "index" keeps the
        # original request-order position of each text.
        results.sort(key=lambda item: item["score"], reverse=True)
        return results

    def _predict(self, pairs: list[tuple[str, str]], batch_size: int) -> list[float]:
        return self.model.predict(pairs, batch_size=batch_size)

    # -- idle trim ------------------------------------------------------------

    async def _idle_trim_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            idle_for = time.monotonic() - self.last_request_at
            if self.device != "mps":
                continue
            if idle_for >= self.settings.idle_trim_seconds and not self._trimmed_since_last_request:
                try:
                    torch.mps.empty_cache()
                    LOG.info("rerank_idle_trim device=mps idle_seconds=%.1f", idle_for)
                except Exception:
                    LOG.exception("rerank_idle_trim_failed")
                self._trimmed_since_last_request = True

    def info_payload(self, aux: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model_id": self.settings.model_id,
            "device": self.device,
            "degraded": self.degraded,
            "dtype": self.dtype,
            "revision": self.settings.model_revision,
            "queue_depth": self.queue.qsize(),
        }
        if self.settings.aux_port is not None:
            # Added only when an aux port is configured, so the default info is unchanged.
            # batch_size = the port this request arrived on; batch_sizes = every port, keyed
            # by port number (primary key is RERANK_PORT, aux key is RERANK_AUX_PORT).
            payload["batch_size"] = self.batch_size_for(aux)
            payload["batch_sizes"] = {
                str(self.settings.port): self.settings.batch_size,
                str(self.settings.aux_port): self.settings.aux_batch_size,
            }
        return payload


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


class RerankRequest(BaseModel):
    query: str
    texts: list[str]
    return_text: bool = False


# Shutdown bound for the aux listener. It has no signal handlers of its own, so a client that
# stalls mid-request could otherwise hold the process until SIGKILL. uvicorn's own graceful
# timeout (_AUX_DRAIN_SECONDS) cancels in-flight request tasks; the margin is the extra time the
# lifespan waits before cancelling the aux task itself as a last resort.
_AUX_DRAIN_SECONDS = 10.0
_AUX_CANCEL_MARGIN_SECONDS = 2.0


class _AuxServer(uvicorn.Server):
    """Second uvicorn listener that lives inside the primary server's event loop.

    Signal handling stays with the primary server: installing a second set of handlers from
    here would replace the primary's, so shutdown is driven by lifespan via should_exit.
    """

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def _start_aux_listener(settings: Settings) -> tuple[_AuxServer, "asyncio.Task[None]"]:
    """Open the aux port and wait until it is listening; raise if it cannot bind."""
    assert settings.aux_port is not None
    config = uvicorn.Config(
        _aux_asgi,
        host=settings.bind,
        port=settings.aux_port,
        lifespan="off",
        log_config=None,
        timeout_graceful_shutdown=_AUX_DRAIN_SECONDS,
    )
    aux_server = _AuxServer(config)

    async def serve() -> None:
        try:
            await aux_server.serve()
        except SystemExit:
            # uvicorn exits via sys.exit when it cannot bind; keep that out of the event loop.
            pass

    task = asyncio.create_task(serve())
    while not aux_server.started and not task.done():
        await asyncio.sleep(0.01)
    if not aux_server.started:
        raise RuntimeError(f"aux listener failed to start on port {settings.aux_port}")
    LOG.info(
        "rerank_aux_listener_ready port=%d batch_size=%d", settings.aux_port, settings.aux_batch_size
    )
    return aux_server, task


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    engine = RerankEngine(settings)
    app.state.engine = engine
    await engine.start()
    aux_server: _AuxServer | None = None
    aux_task: "asyncio.Task[None] | None" = None
    try:
        if settings.aux_port is not None:
            aux_server, aux_task = await _start_aux_listener(settings)
        yield
    finally:
        try:
            if aux_server is not None and aux_task is not None:
                aux_server.should_exit = True
                done, _ = await asyncio.wait(
                    {aux_task}, timeout=_AUX_DRAIN_SECONDS + _AUX_CANCEL_MARGIN_SECONDS
                )
                if not done:
                    aux_task.cancel()
                # Collect the result so a failed or cancelled aux task never skips engine.stop().
                await asyncio.gather(aux_task, return_exceptions=True)
        finally:
            await engine.stop()


app = FastAPI(lifespan=lifespan)


async def _aux_asgi(scope, receive, send) -> None:
    """Entry point of the aux listener: the same app, with the request marked as aux-port."""
    if scope["type"] in ("http", "websocket"):
        scope["rerank_aux"] = True
    await app(scope, receive, send)


def _is_aux(request: Request) -> bool:
    return bool(request.scope.get("rerank_aux"))


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Opaque by design: pydantic's default detail echoes back the invalid
    # input verbatim, which would leak query/texts payload content into the
    # HTTP response. Log only that a validation failure occurred.
    LOG.warning("rerank_invalid_request")
    return JSONResponse(status_code=422, content={"error": "rerank_invalid_request"})


def _engine(request: Request) -> RerankEngine:
    return request.app.state.engine


@app.get("/info")
async def get_info(request: Request):
    return _engine(request).info_payload(aux=_is_aux(request))


@app.get("/health")
async def get_health(request: Request):
    engine = _engine(request)
    if not engine.ready:
        return PlainTextResponse("not ready", status_code=503)
    if not engine.is_worker_alive():
        return PlainTextResponse("worker not running", status_code=503)
    return PlainTextResponse("ok")


@app.post("/rerank")
async def post_rerank(request: Request, body: RerankRequest):
    engine = _engine(request)
    if len(body.texts) > engine.settings.max_texts:
        return JSONResponse(
            status_code=413,
            content={"error": "rerank_too_many_texts", "max": engine.settings.max_texts},
        )
    if not body.texts:
        return []
    try:
        results = await engine.submit(
            request, body.query, body.texts, body.return_text, aux=_is_aux(request)
        )
    except QueueFullError as exc:
        return JSONResponse(
            status_code=503, content={"error": "rerank_queue_full", "queue_depth": exc.queue_depth}
        )
    except QueueTimeoutError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "error": "rerank_queue_timeout",
                "waited_ms": exc.waited_ms,
                "queue_depth": exc.queue_depth,
            },
        )
    except ClientDisconnected:
        return Response(status_code=499)
    except RerankInternalError:
        return JSONResponse(status_code=500, content={"error": "rerank_internal_error"})
    return results


def main() -> None:
    settings = load_settings()
    import uvicorn

    uvicorn.run(app, host=settings.bind, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
