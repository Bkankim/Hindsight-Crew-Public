#!/usr/bin/env bash
# Profile-vs-runtime consistency gate for the reranker (2026-07-26 Mac MPS cutover).
#
# Why this gate exists: the other gates all stay GREEN if the reranker silently falls
# back to the in-container CPU provider. That failure is not an error - recall still
# returns correct results, just far slower: 55.8s measured pre-cutover vs a 1.8-2.6s
# round trip after (rerank segment 1.5-2.1s), which then trips the 30s MCP client
# timeout. Removing the trigger (reranker
# keys are env_file-only in docker-compose.yml) is not a detector, so this gate asserts
# the live wiring end to end.
#
# It is a CONSISTENCY gate, not an "MPS required" gate: on local-provider profiles
# (cpu-en, plain ko-full, CI/Linux hosts) it asserts the local configuration is coherent
# and passes. Only when the runtime declares provider=tei does it demand the sidecar.
#
# Latency design (issue #1): absolute ceiling used to be checked only against a small
# personal-alice recall (handful of candidates, 130-530ms). That left CEILING_MS=5000
# unexercised - the probe could slow 10x and still pass. Fix: keep the recall as a
# real-traffic path proof, and measure absolute + per-candidate ceilings against a
# DETERMINISTIC synthetic corpus POSTed straight to the sidecar /rerank (design a).
# Design b (seed a bank) was rejected for write/cleanup/cross-gate interference.
# Design c (drop absolute ceiling) was rejected because whole-request budget would
# never be observed again. The synthetic fixture is fully fabricated - no production
# memory text - with a length schedule near the real-bank shape (min 54 / p50 ~392 /
# max 2665 / mean ~500).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HC_GATE_NEEDS_CONTAINER=1     # dc_env below execs into the hindsight container lib.sh resolves
. "$HERE/lib.sh"

RERANK_URL="${HC_RERANK_URL:-http://127.0.0.1:8787}"
RERANK_LOG="${HC_RERANK_LOG:-$HOME/Library/Logs/hindsight-rerank/server.log}"
PROBE_CORPUS="${HC_RERANK_PROBE_CORPUS:-$HERE/fixtures/gate8-probe-corpus.json}"
# Expected candidate count is pinned to the fixture. A drift here means the fixture
# was edited without updating the gate (or the wrong file was pointed at).
EXPECTED_CANDIDATES="${HC_RERANK_EXPECTED_CANDIDATES:-100}"

# Absolute ceiling on the DETERMINISTIC synthetic probe (not on the small recall).
# Derivation (2026-07-27, live MPS fp16 sidecar, this fixture) - the contended figure
# is MEASURED, not predicted, because predicting it got the margin wrong:
#   idle:      candidates=100 chars=55760 -> elapsed_ms 931-1035 (mean ~950)
#   contended: measured by running this gate alongside MPS-saturating processes -
#              two of them: 2492-2606ms, three of them: 3474ms. An earlier draft
#              predicted 2.18s from a 2.33x multiplier and set the ceiling at 3000ms;
#              the three-process case would have failed that outright. On a
#              machine where the user runs other GPU work, a ceiling that low is
#              a contention alarm, not a regression detector.
#   ceiling:   4000ms = measured contended 2492ms + ~60%. Still 3.5s under the sidecar
#              deadline (7500ms) and 4x the idle probe, so it stays exercised. The
#              failure it must catch - a silent fall back to the CPU cross-encoder -
#              is an order of magnitude away (69.1s measured on CPU fp32 at 300x1800
#              chars), and `degraded`/`device` assertions above catch it directly.
# Scale-independent regression is the per-candidate ceiling's job, not this one.
#
# Deliberate coverage boundary: the recall above is checked for EXISTENCE only, never
# for latency. Real recalls here score a handful of candidates, where fixed overhead
# dominates and a per-candidate rate is noise; and a real query getting slower as the
# bank grows is expected workload, not regression - upstream caps rerank input at
# MAX_CANDIDATES (300), which measures 2.71s, inside budget. So real-traffic latency
# is intentionally unbounded by this gate. If that ever needs bounding, bound it on
# candidates-per-second at a known N, not on wall time of an arbitrary query.
# Override: HC_RERANK_CEILING_MS (set low in CI-negative tests).
CEILING_MS="${HC_RERANK_CEILING_MS:-4000}"
# Scale-independent companion: observed ~9.3ms/candidate on this fixture and
# ~10.6ms on real-bank 99-103 cand; 43ms was the pre-max_length cold outlier.
# 80ms still means device or model regressed regardless of probe size.
PER_CANDIDATE_CEILING_MS="${HC_RERANK_PER_CANDIDATE_CEILING_MS:-80}"

dc_env() {  # dc_env VAR -> prints the value seen by the running hindsight container
  docker exec "$HC_HINDSIGHT_CONTAINER" printenv "$1" 2>/dev/null | tr -d '\r\n'
}

hc_require_container

provider=$(dc_env HINDSIGHT_API_RERANKER_PROVIDER)
[ -n "$provider" ] || { hc_log "could not read HINDSIGHT_API_RERANKER_PROVIDER from the hindsight container"; exit 1; }

# --- embeddings coherence (runs on every profile) -----------------------------
# docker-compose.yml declares no model defaults, so a hand-made .env.local could boot
# with upstream-default embeddings. The embedding model IS the vector dimension, so a
# silent revert corrupts the index - assert the keys exist and that the declared
# dimension matches the model actually configured.
emb_provider=$(dc_env HINDSIGHT_API_EMBEDDINGS_PROVIDER)
emb_model=$(dc_env HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL)
[ -n "$emb_provider" ] || { hc_log "HINDSIGHT_API_EMBEDDINGS_PROVIDER is unset (compose declares no default: .env.local must carry it)"; exit 1; }
if [ "$emb_provider" = "local" ]; then
  [ -n "$emb_model" ] || { hc_log "embeddings provider=local but HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL is unset"; exit 1; }
  case "${emb_model}:${HC_EMBED_DIM:-unset}" in
    BAAI/bge-m3:1024|BAAI/bge-small-en-v1.5:384) ;;
    *:unset) hc_log "HC_EMBED_DIM is unset for embeddings model ${emb_model}"; exit 1 ;;
    BAAI/bge-m3:*|BAAI/bge-small-en-v1.5:*)
      hc_log "embeddings model ${emb_model} does not match HC_EMBED_DIM=${HC_EMBED_DIM} (bge-m3=1024, bge-small-en-v1.5=384)"; exit 1 ;;
    *)
      # Unknown model is a coverage gap, not a proven mismatch - say which it is.
      hc_log "embeddings model ${emb_model} is not in this gate's dimension map; extend the map in ${BASH_SOURCE[0]##*/} to cover it"; exit 1 ;;
  esac
fi

if [ "$provider" != "tei" ]; then
  # Local-provider profile: assert coherence, do not demand the Mac sidecar.
  model=$(dc_env HINDSIGHT_API_RERANKER_LOCAL_MODEL)
  [ -n "$model" ] || { hc_log "provider=$provider but HINDSIGHT_API_RERANKER_LOCAL_MODEL is unset"; exit 1; }
  hc_log "reranker provider=$provider (local profile), model=$model - consistent, MPS sidecar not required"
  exit 0
fi

# --- provider=tei: the Mac MPS sidecar path must be fully wired ----------------
# python is only needed from here on, so a local-provider host without python3 still exits 0 above.
. "$ROOT/scripts/hc-python.sh"; hc_find_python || { hc_log "python3 not found (set HC_PYTHON)"; exit 1; }
# Deadline chain must stay locked: sidecar total deadline 7.5s < client timeout 7.8s,
# and client concurrency must stay 1 because the sidecar is a single-worker MPS server
# (>1 pushes waiting into the sidecar queue, past its deadline, into a retry storm).
mc=$(dc_env HINDSIGHT_API_RERANKER_TEI_MAX_CONCURRENT)
[ "$mc" = "1" ] || { hc_log "TEI max_concurrent=${mc:-unset} (expected 1; >1 revives the 503 retry storm)"; exit 1; }
to=$(dc_env HINDSIGHT_API_RERANKER_TEI_HTTP_TIMEOUT)
[ "$to" = "7.8" ] || { hc_log "TEI http_timeout=${to:-unset} (expected 7.8; the 7.5s sidecar deadline must fire first)"; exit 1; }

info=$(curl -s --max-time 10 "$RERANK_URL/info") || { hc_log "rerank sidecar unreachable at $RERANK_URL"; exit 1; }
device=$(printf '%s' "$info" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("device",""))' 2>/dev/null)
degraded=$(printf '%s' "$info" | "$PY" -c 'import json,sys; print(str(json.load(sys.stdin).get("degraded","")).lower())' 2>/dev/null)
[ "$device" = "mps" ] || { hc_log "sidecar device=${device:-unreachable} (expected mps; CPU fallback = silent 20x+ slowdown)"; exit 1; }
[ "$degraded" = "false" ] || { hc_log "sidecar degraded=$degraded (expected false)"; exit 1; }

# Live traffic must actually traverse the sidecar - config alone proves nothing.
# The window is tracked by TOTAL line offset (wc -l), not by match count: the log
# interleaves uvicorn access lines, so a grep -c value under-counts the offset and
# drags historical (possibly slower) lines into later checks.
[ -f "$RERANK_LOG" ] || { hc_log "rerank log missing: $RERANK_LOG"; exit 1; }
before_recall_lines=$(wc -l < "$RERANK_LOG" 2>/dev/null | tr -d ' '); before_recall_lines=${before_recall_lines:-0}
body=$(rest_body POST "$HC_DEMO_TOKEN_ALICE" "$(hc_bank_path personal-alice)/memories/recall" \
  '{"query":"rerank provider gate probe quarterly budget"}')
printf '%s' "$body" | grep -q '"results"' || { hc_log "recall failed: $(printf '%s' "$body" | head -c 160)"; exit 1; }
recall_lines=$(tail -n "+$((before_recall_lines + 1))" "$RERANK_LOG" | grep 'rerank_processed' || true)
[ -n "$recall_lines" ] || { hc_log "no rerank_processed line appeared during recall: traffic bypassed the MPS sidecar"; exit 1; }

# --- deterministic latency probe (sidecar /rerank, fixed-size synthetic corpus) -
# Separated from recall so candidate count is identical every run. Payload hygiene:
# the fixture is synthetic; on failure we report only status/counts/ms, never query
# or texts (same invariant as the sidecar itself).
[ -f "$PROBE_CORPUS" ] || { hc_log "rerank probe corpus missing: $PROBE_CORPUS"; exit 1; }
probe_req=$("$PY" -c '
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
req = doc["request"] if isinstance(doc, dict) and "request" in doc else doc
texts = req.get("texts")
if not isinstance(texts, list) or not texts:
    sys.exit("probe corpus has no texts[]")
n = len(texts)
meta_n = (doc.get("meta") or {}).get("n") if isinstance(doc, dict) else None
if meta_n is not None and int(meta_n) != n:
    sys.exit("probe corpus meta.n=%s != len(texts)=%s" % (meta_n, n))
print(n)
print(json.dumps({"query": req["query"], "texts": texts, "return_text": bool(req.get("return_text", False))}, ensure_ascii=False, separators=(",", ":")))
' "$PROBE_CORPUS") || { hc_log "probe corpus unreadable or malformed: $PROBE_CORPUS"; exit 1; }
probe_n=$(printf '%s\n' "$probe_req" | sed -n '1p')
probe_json=$(printf '%s\n' "$probe_req" | sed -n '2p')
[ "$probe_n" = "$EXPECTED_CANDIDATES" ] || {
  hc_log "probe corpus candidate count ${probe_n} != expected ${EXPECTED_CANDIDATES} (fixture drift)"
  exit 1
}

before_probe_lines=$(wc -l < "$RERANK_LOG" 2>/dev/null | tr -d ' '); before_probe_lines=${before_probe_lines:-0}
# curl --fail-with-body is not on macOS stock curl; capture status explicitly.
# --max-time must exceed sidecar deadline (7.5s) so a slow regression surfaces as
# a ceiling breach (or 503) rather than a client-side curl abort.
probe_tmp=$(mktemp) || { hc_log "mktemp failed"; exit 1; }
probe_code=$(curl -sS --max-time 20 \
  -o "$probe_tmp" -w '%{http_code}' \
  -H 'content-type: application/json' \
  -X POST --data-binary "$probe_json" \
  "$RERANK_URL/rerank") || {
  rm -f "$probe_tmp"
  hc_log "synthetic /rerank probe curl failed (sidecar unreachable or timed out)"
  exit 1
}
if [ "$probe_code" != "200" ]; then
  # Do not dump body: error JSON is opaque by contract, but stay strict.
  rm -f "$probe_tmp"
  hc_log "synthetic /rerank probe HTTP ${probe_code} (expected 200)"
  exit 1
fi
probe_scores=$("$PY" -c 'import json,sys; print(len(json.load(open(sys.argv[1], encoding="utf-8"))))' "$probe_tmp" 2>/dev/null || echo 0)
rm -f "$probe_tmp"
[ "$probe_scores" = "$EXPECTED_CANDIDATES" ] || {
  hc_log "synthetic /rerank returned ${probe_scores} scores (expected ${EXPECTED_CANDIDATES})"
  exit 1
}

probe_lines=$(tail -n "+$((before_probe_lines + 1))" "$RERANK_LOG" | grep 'rerank_processed' || true)
[ -n "$probe_lines" ] || { hc_log "no rerank_processed line for synthetic probe (log path broken?)"; exit 1; }

# Prefer the line whose candidates= matches the fixture; fall back to worst elapsed
# in the probe window if the field is missing (should not happen on current server).
worst_line=$(printf '%s\n' "$probe_lines" | sed -n 's/.*\(rerank_processed .*\)/\1/p' \
  | grep "candidates=${EXPECTED_CANDIDATES}" | sort -t= -k5 -g | tail -n 1)
if [ -z "$worst_line" ]; then
  worst_line=$(printf '%s\n' "$probe_lines" | sed -n 's/.*\(rerank_processed .*\)/\1/p' \
    | sort -t= -k5 -g | tail -n 1)
fi
worst_ms=$(printf '%s' "$worst_line" | sed -n 's/.*elapsed_ms=\([0-9.]*\).*/\1/p')
worst_cand=$(printf '%s' "$worst_line" | sed -n 's/.*candidates=\([0-9]*\).*/\1/p')
[ -n "$worst_ms" ] || { hc_log "rerank_processed line found but elapsed_ms unparseable"; exit 1; }
[ "$worst_cand" = "$EXPECTED_CANDIDATES" ] || {
  hc_log "synthetic probe logged candidates=${worst_cand:-unset} (expected ${EXPECTED_CANDIDATES}) - non-deterministic or wrong window"
  exit 1
}

over=$("$PY" -c "import sys; print(1 if float(sys.argv[1]) > float(sys.argv[2]) else 0)" "$worst_ms" "$CEILING_MS")
[ "$over" = "0" ] || {
  hc_log "rerank ${worst_ms}ms exceeds ceiling ${CEILING_MS}ms on fixed ${EXPECTED_CANDIDATES}-candidate probe - suspect device, model, or host GPU saturation"
  exit 1
}

per_cand=$("$PY" -c "import sys; print(f'{float(sys.argv[1])/int(sys.argv[2]):.1f}')" "$worst_ms" "$worst_cand")
rate_over=$("$PY" -c "import sys; print(1 if float(sys.argv[1]) > float(sys.argv[2]) else 0)" "$per_cand" "$PER_CANDIDATE_CEILING_MS")
[ "$rate_over" = "0" ] || {
  hc_log "rerank ${per_cand}ms per candidate (${worst_ms}ms / ${worst_cand}) exceeds ${PER_CANDIDATE_CEILING_MS}ms - scale-independent regression, suspect device or model"
  exit 1
}

hc_log "reranker on MPS sidecar: provider=tei concurrency=1 timeout=7.8 device=mps degraded=false, live traffic confirmed, synthetic ${worst_cand}cand ${worst_ms}ms < ${CEILING_MS}ms, ${per_cand}ms/candidate < ${PER_CANDIDATE_CEILING_MS}ms"
exit 0
