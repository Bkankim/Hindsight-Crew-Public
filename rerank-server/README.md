# rerank-server

Mac-native MPS fp16 rerank server for Hindsight-Crew. Speaks the TEI (text-embeddings-inference)
`/info` and `/rerank` contract so the existing Hindsight TEI client can point at it unmodified.

## Purpose

Runs `BAAI/bge-reranker-v2-m3` locally on Apple Silicon (MPS, fp16) with a CPU fp32 fallback,
behind a small FastAPI service. No training, no external calls at inference time (offline HF hub
cache only).

## Environment variables

The `Default` column is both the code default (`Settings` in `server.py`) and the deployed
value in `rerank-server/launchd/__PREFIX__.hindsight-rerank.plist.example` (rendered by
`scripts/render-launchd.py --set PREFIX=<label prefix>`, see `rerank-server/launchd/README.md`). They are kept
identical on purpose, so the operational envelope quoted by `docs/THREAT-MODEL.md` is
enforced by committed code rather than by an untracked LaunchAgent. One exception: `RERANK_BATCH_SIZE` is
`32` as the code default but `16` in the template (the production deployment value, B2 / issue #84);
applying it to the running sidecar is the #156 window's job.

| Variable                        | Default                                       | Notes                                                        |
|----------------------------------|------------------------------------------------|----------------------------------------------------------------|
| `RERANK_BIND`                   | `127.0.0.1`                                    | Loopback only, checked via `ipaddress.ip_address(...).is_loopback`; a value that fails to parse is rejected unless it is exactly `localhost`. Anything non-loopback (`0.0.0.0`, `::`, `192.0.2.10`, empty string, ...) is rejected at startup (`sys.exit`). |
| `RERANK_PORT`                   | `8787`                                         |                                                                |
| `RERANK_MODEL`                  | `BAAI/bge-reranker-v2-m3`                      |                                                                |
| `RERANK_MODEL_REVISION`         | `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`     | Pinned snapshot; must exist in local HF cache.                |
| `RERANK_MAX_LENGTH`             | `512`  | Truncation length for the query+document pair. The model card's own example uses 512, and attention cost is superlinear in sequence length, so leaving it unbounded pays full price for long facts (measured: 99 candidates 1.657s unbounded vs 1.054s at 512, spearman 0.9995). Startup rejects values under 64. |
| `RERANK_BATCH_SIZE`             | `32`                                            | Batch size passed to `CrossEncoder.predict`. Code default `32`; the template (production deployment value) sets `16` (B2, issue #84), so the Default column is not the template value for this key. The running sidecar keeps `32` until the #156 window re-renders the plist. |
| `MAX_QUEUE_DEPTH`                | `3`                                             | Must be `>= 2`; smaller values are rejected at startup. Counts waiting jobs only; one additional job may be in-flight, so effective concurrent admission is `depth + 1`. |
| `RERANK_QUEUE_TIMEOUT_SECONDS`  | `31.5`                                          | Wait-phase backstop evaluated at dequeue time. Deliberately loose: with `MAX_QUEUE_DEPTH=3` the original sizing formula `1.5 x P_worst x (depth - 1)` yields 6.3s, which is below the total deadline and therefore invalid, so this is set well above the deadline instead and is structurally unreachable in the production shape (the submit-side deadline settles first). Must be `>=` the total deadline; startup rejects smaller values. |
| `RERANK_TOTAL_DEADLINE_SECONDS` | `7.5`                                           | Client-facing absolute bound on queue wait + processing, enforced in `submit()` independent of worker health. Set just below the Hindsight client timeout `T = 7.8s` so the caller receives an explicit `503` instead of timing out blind. |
| `RERANK_MAX_TEXTS`              | `256`                                           | Max `texts` entries per `/rerank` request; over the limit gets a `413`, model is never invoked. |
| `IDLE_TRIM_SECONDS`             | `1800`                                           | Idle time before a one-shot `torch.mps.empty_cache()`.        |
| `HF_HUB_OFFLINE`                | `1`                                             | Set as default; can be overridden via env for debugging.      |
| `RERANK_AUX_PORT`               | unset                                           | **Bench only.** Second loopback listener with its own batch size; see "Auxiliary port (bench only)". Must be set together with `RERANK_AUX_BATCH_SIZE`. |
| `RERANK_AUX_BATCH_SIZE`         | unset                                           | **Bench only.** Batch size for jobs that arrive on the aux port. Must be `>= 1`. |

Invariant: `RERANK_MAX_TEXTS` must stay at least 2x the client's per-request batch cap, so a `413`
never fires on the normal request path. Production runs the Hindsight client at its default batch cap of 128
(`HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE` is not set), and the LaunchAgent plist sets `RERANK_MAX_TEXTS=256`,
the same as the code default. The B1 experiment (client cap 300 with plist `RERANK_MAX_TEXTS=600`, issue #22,
2026-09-29) was rolled back on 2026-10-06 after the canary judged ROLLBACK, and the variable was removed.
Raise the plist before raising the client cap, otherwise a request above the sidecar cap gets a `413`.

## Auxiliary port (bench only)

> **벤치 전용.** 운영 plist(`__PREFIX__.hindsight-rerank.plist.example` 포함)는 이 두 변수를 설정하지
> 않는다. 둘 다 없으면 포트·응답·로그·`/info` 모두 보조 포트 도입 전과 같다.

Purpose (issue #78): the batch size is a per-process setting, but the bench shares one sidecar across
its sets (one sidecar per set swaps unified memory, see `bench/README.md`). To A/B a batch size on that
one sidecar, a second port on the same process serves the same model with a different batch size.

```sh
RERANK_PORT=18781 RERANK_BATCH_SIZE=32 \
RERANK_AUX_PORT=18782 RERANK_AUX_BATCH_SIZE=16 \
uv run uvicorn server:app --host 127.0.0.1 --port 18781
```

- The aux listener is opened by the app's lifespan inside the same process and event loop, so it
  works under `uvicorn server:app` and `python server.py` alike. It binds to `RERANK_BIND` (the same
  loopback-only rule as the primary port); the primary port is still the one `uvicorn` is told to bind.
  Keep `RERANK_PORT` equal to the `--port` you pass: the aux-port-differs check and the `/info` keys use
  `RERANK_PORT`.
- Both ports share one model (weights are loaded once per process), one queue and one serial
  worker. `MAX_QUEUE_DEPTH` is a single budget summed over both ports, and `RERANK_MAX_TEXTS`,
  `RERANK_TOTAL_DEADLINE_SECONDS` and `RERANK_QUEUE_TIMEOUT_SECONDS` apply to both. Each job carries the
  batch size of the port it arrived on and infers with it. Scores do not depend on the batch size.
- Startup is refused (`sys.exit`) when only one of the two variables is set, when
  `RERANK_AUX_PORT` equals `RERANK_PORT` or is outside 1-65535, when `RERANK_AUX_BATCH_SIZE < 1`, or when
  `RERANK_BIND` is not loopback. An aux batch size equal to the primary one is allowed (A/A control).
  If the aux port cannot be bound (already in use), the whole sidecar fails to start.
- `GET /info` on either port keeps every existing field and, only when an aux port is configured, adds:
  - `"batch_size": <int>` - the batch size of the port this request arrived on.
  - `"batch_sizes": {"<RERANK_PORT>": <int>, "<RERANK_AUX_PORT>": <int>}` - batch size of every port, keys are
    the port numbers as strings.
  e.g. `{"model_id": ..., "queue_depth": 0, "batch_size": 16, "batch_sizes": {"18781": 32, "18782": 16}}` read
  from `:18782`. A harness `verify` can compare `info["batch_size"]` from the URL a set points at with the
  arm's configured batch size (no port lookup needed), or look the port up in `batch_sizes`.
- The `rerank_processed` log line gets ` port=<port> batch_size=<n>` appended only when an aux port is
  configured; `batches=` is computed with that job's batch size.

## Running

```sh
uv sync
uv run uvicorn server:app --host 127.0.0.1 --port 8787
```

(or `uv run python server.py`, which reads the same env vars and calls `uvicorn.run` directly.)

Model loading and warmup happen once at process startup, before `/health` returns `200`.

## TEI contract summary

- `GET /info` -> `200` `{"model_id": ..., "device": "mps"|"cpu", "degraded": bool, "dtype": "float16"|"float32", "revision": "<sha>", "queue_depth": int}`
  (with the bench-only aux port configured it also carries `batch_size` and `batch_sizes`, see "Auxiliary port (bench only)")
- `GET /health` -> `200 "ok"` once the model is loaded, warmed up, and the background worker task
  is alive; `503` before the model is ready or if the worker task has died.
- `POST /rerank` `{"query": str, "texts": [str], "return_text": false}` -> `200`
  `[{"index": i, "score": s, "text"?: str}, ...]`, one entry per input text, sorted by `score`
  descending to match real TEI's response ordering; `index` always keeps the original position of
  the text in the request. `text` is included per item only when `return_text` is `true`. Scores
  are sigmoid-transformed into `[0, 1]`; `NaN`/`inf` are sanitized to `0.0`. An empty `texts` list
  returns `200 []` immediately without invoking the model.
- Malformed request body (schema validation failure) -> `422 {"error": "rerank_invalid_request"}`;
  the response never echoes the invalid input back.
- Too many texts (over `RERANK_MAX_TEXTS`) -> `413 {"error": "rerank_too_many_texts", "max": n}`.
- Queue-full -> `503 {"error": "rerank_queue_full", "queue_depth": n}`.
- Absolute deadline (queue wait + processing past `RERANK_TOTAL_DEADLINE_SECONDS`, including a
  dead/hung worker) or wait-phase backstop past `RERANK_QUEUE_TIMEOUT_SECONDS` at dequeue ->
  `503 {"error": "rerank_queue_timeout", "waited_ms": x, "queue_depth": n}`. Jobs whose future
  was already settled by the deadline are skipped at dequeue, never inferred.
- Client disconnected before a result was ready -> `499` with an empty body (no inference wasted
  on an abandoned request; this does not surface as an unhandled `CancelledError`).
- Client characteristics assumed: client concurrency 1 (matches the sidecar's single MPS worker), batches up to 128 texts/request, retries on 5xx (up to 4 attempts total, backoff 0.5+1+2=3.5s), ignores `Retry-After`.

## Security boundary

- Binds to loopback only; any non-loopback `RERANK_BIND` value is refused at startup. No
  authentication layer is implemented by design (AC-B2: loopback-only reachability is the trust
  boundary, matching the rest of the local Hindsight-Crew toolchain that assumes same-host
  callers).
- Request payloads (`query`, `texts`) are never written to logs, error responses (including
  validation errors), or disk. Only candidate counts, character-length totals, batch counts,
  elapsed milliseconds, device, and error codes are logged.

## Latency budget

Downstream retrieval budget constants referenced by the Hindsight pipeline: `A = 4` (total
attempts including retries, source-fixed) and backoff `B = 3.5s` (0.5 + 1 + 2). The client-facing
bound is `RERANK_TOTAL_DEADLINE_SECONDS = 7.5` (just below the client timeout `T = 7.8s`), so a
stalled or saturated server produces an explicit `503` before the client aborts on its own.
`RERANK_QUEUE_TIMEOUT_SECONDS` is NOT a client signal: it is the dequeue-time backstop from the
plan formula and must stay `>=` the total deadline, otherwise legitimately queued worst-case
batches (third of three concurrent long batches waits ~2 x P ~= 5-6s) would be rejected while
still inside the deadline. Dead-job cleanup is handled by the settled-future skip at dequeue plus
client-disconnect discard, not by shrinking the backstop.

## Tuning levers and what they cost

The acceptance criterion this server was built against ("rerank segment under 1s") is
recorded as FAILED rather than quietly relaxed, and the substitute figures below were
accepted on 2026-07-26. Anyone tempted to make that number green should know the price
first.

| Lever | Effect | Cost |
|---|---|---|
| `RERANK_MAX_LENGTH` 512 (current) | 99-103 candidates in ~1.08s, quality untouched | full-bank queries exceed 1s by 8% |
| `RERANK_MAX_LENGTH` 256 | ~0.70s, the original criterion passes | ranking quality: spearman 0.9995 -> 0.984, top-ten agreement 9/10 -> 8-10/10 |
| upstream `MAX_CANDIDATES` lower | saturation cost falls proportionally | narrower retrieval, so recall drops |
| second sidecar process | concurrent throughput | a second resident copy of the model, about +2.3GB |

The reason 512 is the default rather than 256: the 1s target came from a bench corpus of
165-281 char documents (4ms per candidate), while real bank facts cost 10.6ms per
candidate. The target was unreachable on this corpus from the start, so truncating harder
would trade real ranking quality for a miscalibrated number. The user-facing contracts
(20s round trip, 7.8s client timeout) already hold with 7-16x margin.

Because the criterion is knowingly unmet, the latency is watched by
`verify/gate8_rerank_provider.sh` rather than by memory: an absolute 5000ms ceiling plus a
scale-independent 80ms-per-candidate ceiling. 80ms sits well above the measured 10.6ms
because host GPU contention alone stretches latency up to 2.33x; tightening it to 40ms
would false-alarm on normal traffic.

## Measured (N2, 2026-07-26, M5 Pro / MPS fp16 / batch 32)

Provenance matters here: the rows below were taken on different instances and corpora.
Rows marked "bench" come from throwaway foreground instances driven by a benchmark script
(synthetic or replayed bank text), NOT from the resident LaunchAgent sidecar, so they have
no matching lines in `~/Library/Logs/hindsight-rerank/server.log`. Rows marked "resident"
are readable in that log. Do not mix rows from different corpora into a single ratio.

| Scenario                                         | Result                          | Source |
|---------------------------------------------------|----------------------------------|--------|
| `Q_69` (69 texts, short bench corpus)              | max `0.259s`                    | bench |
| `P(128)` (128 texts, short bench corpus)           | max `0.384s`                    | bench |
| `Q_300` (300 texts x ~1800 chars, 3 batches concurrently) | max `5.197s`, `503` count: `0`   | bench |
| 8 concurrent requests                              | all `ok`                        | bench |
| CPU fp32 worst case (300 texts x 1800 chars)        | `69.1s`                          | bench (device=cpu) |
| `P_worst` (single batch, 128 texts x ~1800 chars, server-side) | `2.18s`             | bench; the resident log's own worst is `2053.7ms` on a 70-candidate real-bank request, a different measurement |
| Real bank recall (69-73 candidates, 35-37KB total) | rerank segment `1.5-2.1s` (normal `1.5-1.9s`, cold `2.05s`); ~23-29ms per candidate | resident |
| Real bank recall, later same day (84 candidates, 42141 chars) | `3.61s` - NOT candidate growth: reproduced as host GPU contention (same payload is `1.60s` idle, `3.59-3.74s` under external GPU load, 2.33x) | resident |
| 99 candidates (52KB), `max_length` unbounded vs `512` | `1.657s` -> `1.054s`, quality-neutral (spearman 0.9995, 9/10 of top ten unchanged) | bench |
| 300 candidates (the upstream `MAX_CANDIDATES` cap, 159KB) | `3.265s` -> `2.709s` at `max_length=512`; per-candidate cost falls with batch size, so linear extrapolation overstates this badly | bench |
| Real bank recall after `max_length=512` shipped (96-100 candidates, ~48KB) | `1073-1087ms`, MCP round trip `1.22s` | resident |
| S+G (non-rerank portion of a recall round trip: search + gateway/MCP overhead) | max `0.25s` observed (3 runs, unloaded); `0.5s` used conservatively | resident |

Saturation semantics (designed behavior, not a defect): the queue admits up to `depth + 1`
worst-case batches, which at `P_worst = 2.18s` is more work (~17s drain) than one
`RERANK_TOTAL_DEADLINE_SECONDS = 7.5s` window can serve. Two distinct fast-fail 503 paths
apply under overload: arrivals beyond `depth + 1` are rejected immediately with
`rerank_queue_full` (before enqueue), while admitted jobs that cannot finish inside the
7.5s deadline fail with `rerank_queue_timeout`. The Hindsight client absorbs either
through its retry budget (`A = 4`); fast-fail backpressure is preferred over unbounded
queuing. Old-config measurement (depth 8, client concurrency 8; recorded 2026-07-26,
predating the current single-concurrency client below): 12 concurrent requests against
depth 8 produced 9x 200 + 3x 503 `rerank_queue_full` with the server healthy afterwards.

Current regime: `HINDSIGHT_API_RERANKER_TEI_MAX_CONCURRENT = 1` caps the client to one
in-flight rerank call at a time, so at most one Hindsight-originated job occupies the
sidecar and the `MAX_QUEUE_DEPTH = 3` queue sits effectively empty in normal operation;
the saturation math above describes the sidecar's admission ceiling, not the traffic a
single-concurrency client actually generates.

`RERANK_TOTAL_DEADLINE_SECONDS` / client `T` derivation from the measurements above:

- `T_lo = 1.5 * 5.197 = 7.8s` (1.5x margin over the worst observed `Q_300` client-observed time).
- `T_hi = (19.5 - (S+G)) / 2 = (19.5 - 0.5) / 2 = 9.5s` (from the one-retry absorption contract
  `S + 2T + 0.5 + G <= 20s` on the client-observed round trip).
- `T_lo < T_hi`, so `T = T_lo = 7.8s` (plan rule: `T = T_lo` only when `T_lo < T_hi`, otherwise
  escalate levers). `HINDSIGHT_API_RERANKER_TEI_HTTP_TIMEOUT` will be set to `7.8`;
  `RERANK_TOTAL_DEADLINE_SECONDS = 7.5` sits just below it.

## Operating envelope

The sidecar runs a single MPS worker (MPS is not thread-safe for concurrent model
execution), so latency scales linearly with the number of concurrent heavy-recall
requests N: `p_worst ~= N x 1.7s`. Measured: 8 concurrent requests, worst case 13.4s
(inside the 20s round-trip contract); at N ~= 11 the worst case approaches 20s; at
N ~= 17 it exceeds the MCP 30s ceiling. The only scaling path is a second sidecar
process (a second copy of the model resident, ~+2.3GB) - adding worker threads to the
existing process is not an option given MPS's single-worker constraint.

Verify: `bash verify/gate8_rerank_provider.sh` (run from the repo root).

Tests in `tests/` mock `CrossEncoder` entirely; no model download or network access required to
run the test suite.
