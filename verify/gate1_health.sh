#!/usr/bin/env bash
# Gateway front health, and - since 2026-07-26 - upstream reachability too.
#
# The gateway's own /healthz used to describe only itself, so when the main hindsight
# container sat stopped for about two hours this gate stayed GREEN and nothing surfaced
# the fact that recall was dead. `ok` deliberately still refers to the gateway alone
# (restarting the gateway is the wrong remedy for a downed upstream), so the assertion
# below is on the separate upstream_ok field.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/lib.sh"
. "$ROOT/scripts/hc-python.sh"; hc_find_python || { hc_log "python3 not found (set HC_PYTHON)"; exit 1; }

body=$(curl -s -w '\n%{http_code}' --max-time 10 "$HC_GW/healthz")
code=$(printf '%s' "$body" | tail -n 1)
json=$(printf '%s' "$body" | sed '$d')
[ "$code" = "200" ] || { hc_log "gateway health = $code"; exit 1; }

upstream=$(printf '%s' "$json" | "$PY" -c \
  'import json,sys; d=json.load(sys.stdin); print(d.get("upstream_ok"), d.get("upstream","?"))' 2>/dev/null)
case "$upstream" in
  "True ok")
    hc_log "gateway /healthz 200, upstream reachable"; exit 0 ;;
  "None"*)
    # Older gateway build without the field. Do not fail the suite for that, but do not
    # pretend the check happened either.
    hc_log "gateway /healthz 200 (upstream_ok absent - gateway predates the upstream probe)"; exit 0 ;;
  *)
    hc_log "gateway /healthz 200 but upstream is down ($upstream): recall is failing even though the gateway answers"
    exit 1 ;;
esac
