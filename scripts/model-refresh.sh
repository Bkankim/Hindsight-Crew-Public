#!/usr/bin/env bash
# model-refresh.sh - daily refresh driver for externally-capped mental models (issues #10, #11).
# Targets ONLY models with trigger.refresh_after_consolidation=false: those opted out of
# the server's consolidation-driven refresh, so this script is their sole refresh path
# (server refresh_cron stays null - single driver, no double-fire).
# Every FULL_INTERVAL_SECONDS (48h) is a full pass: a model whose trigger.mode is exactly "delta"
# gets clear -> refresh (delta-drift rebuild, hindsight.vectorize.io mental-models-deep-dive);
# a model in "full", unknown or absent mode gets a plain refresh with NO clear (issue #272: a
# full-mode refresh rebuilds from scratch and keeps the old content when it fails, while clear
# + a failed refresh leaves the model empty and there is no API to write content back).
# Other passes are a plain delta refresh.
# The full-rebuild epoch advances ONLY when every model's refresh (and its clear, in delta mode)
# succeeded, so a partial
# failure retries the full pass next run instead of silently skipping a model for 48h.
# Activity gating (#11): a model whose tags gained no memories since its last successful
# refresh is skipped (MODEL_REFRESH_SKIP). The fingerprint is the live memory count for the
# model's tags (tags_match=all_strict), kept by THIS driver under
# $STATE_DIR/activity/<id>.{delta,full} - never list_mental_models.last_refreshed_at, which
# was observed out of step with external refreshes. A full pass compares against the count
# at the last FULL rebuild, so every model still gets one full-pass refresh (clear first only
# in delta mode) after its last activity even if a delta already consumed it. No baseline (first run, new model) or a
# failed count => refresh, never skip. `--force` bypasses the gate (manual kickstart after
# a burst of decisions is normally gated correctly: the new memories change the count).
# Log tokens (greppable, probe.sh convention): MODEL_REFRESH_OK <id> <chars> /
# MODEL_REFRESH_FULL <id> (cleared first) / MODEL_REFRESH_FULL <id> clear_skipped mode=<mode|none>
# (full pass, clear not called) / MODEL_REFRESH_SKIP <id> no_new_memories count=<n> /
# MODEL_REFRESH_FAIL <id> <reason> / MODEL_REFRESH_RUN_DONE.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
[ -f .env.local ] && { set -a; . ./.env.local; set +a; }
. "$ROOT/scripts/hc-host-env.sh"; hc_load_host_env "$ROOT"

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    *) echo "usage: $0 [--force]" >&2; exit 2 ;;
  esac
done

TS() { date '+%Y-%m-%d %H:%M:%S'; }
[ -n "${HOME:-}" ] || { echo "$(TS) MODEL_REFRESH_FAIL - home_unset"; exit 1; }
. "$ROOT/scripts/hc-python.sh"
hc_find_python || { echo "$(TS) MODEL_REFRESH_FAIL - python_missing"; exit 1; }

BASE_URL="${HC_GATEWAY_URL:-http://127.0.0.1:8888}"
BANK="${HC_PERSONAL_BANK:-}"
STATE_DIR="${HOME}/Library/Logs/hindsight-model-refresh"
STATE_FILE="${STATE_DIR}/last-full-epoch"
ACTIVITY_DIR="${STATE_DIR}/activity"
mkdir -p "$STATE_DIR" "$ACTIVITY_DIR" 2>/dev/null || true
FULL_INTERVAL_SECONDS=172800
POLL_TIMEOUT_SECONDS=600
SLEEP_BETWEEN_MODELS=15

command -v curl >/dev/null || { echo "$(TS) MODEL_REFRESH_FAIL - curl_missing"; exit 1; }
# No default for the owner's bank or identity (.env.host.local, issue #186): fail loudly instead.
for key in HC_PERSONAL_BANK HC_OWNER; do
  [ -n "${!key:-}" ] || { echo "$(TS) MODEL_REFRESH_FAIL - config_missing $key"; exit 1; }
done
TOKEN="$(HC_OWNER="$HC_OWNER" "$PY" -c "
import json, os
acl = json.load(open('secrets/acl.json'))
print(next(t for t, p in acl['tokens'].items() if p.get('identity') == os.environ['HC_OWNER']))
" 2>/dev/null)"
[ -n "$TOKEN" ] || { echo "$(TS) MODEL_REFRESH_FAIL - token_missing"; exit 1; }

# The token travels on curl's stdin (`-H @-`), never in argv where `ps` shows it (issue #214). printf is a
# builtin, so the value reaches no process's command line.
api() { # method path [json-body] -> body on stdout, appends "\n<http_code>"
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    printf 'Authorization: Bearer %s\n' "$TOKEN" | curl -s -m 60 -w '\n%{http_code}' -X "$method" -H @- \
      -H 'Content-Type: application/json' -d "$body" "${BASE_URL}${path}"
  else
    printf 'Authorization: Bearer %s\n' "$TOKEN" | curl -s -m 60 -w '\n%{http_code}' -X "$method" -H @- \
      "${BASE_URL}${path}"
  fi
}

# Externally-driven models = refresh_after_consolidation false (live read, no drift).
# One line per model: <id> <mode> <tag1,tag2,...> (tags feed the activity fingerprint). mode is
# trigger.mode, never empty ("none" when absent) so an empty tag list cannot shift the columns.
# detail=content is explicit: upstream >=0.10 lists with detail=metadata by default, which has no `trigger`; limit=1000 because the list default is 100 (issue #243).
# A non-2xx list ends as list_http_<code> (000 = no response) so auth/5xx never reads as an empty list (issue #255).
list_out="$(api GET "/v1/default/banks/${BANK}/mental-models?detail=content&limit=1000")"
list_code="$(echo "$list_out" | tail -n1)"
case "$list_code" in 2??) ;; *) echo "$(TS) MODEL_REFRESH_FAIL - list_http_${list_code}"; exit 1 ;; esac
MODEL_ROWS="$(echo "$list_out" | sed '$d' | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
items = d.get('items') if isinstance(d, dict) else d
for m in items or []:
    trig = m.get('trigger') or {}
    if trig.get('refresh_after_consolidation') is False:
        mode = trig.get('mode')
        # Never trim: only exactly delta may clear, so delta plus a space must stay a different (unknown) value.
        mode = ''.join('_' if c.isspace() else c for c in mode) if isinstance(mode, str) and mode.strip() else 'none'
        print(m['id'], mode, ','.join(t for t in (m.get('tags') or []) if isinstance(t, str)))
" 2>/dev/null)"
[ -n "$MODEL_ROWS" ] || { echo "$(TS) MODEL_REFRESH_FAIL - no_target_models"; exit 1; }

last=0
[ -f "$STATE_FILE" ] && last="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
case "$last" in ''|*[!0-9]*) last=0 ;; esac
NOW="$(date +%s)"
[ $((NOW - last)) -lt 0 ] && last=0
full_pass=0
[ $((NOW - last)) -ge "$FULL_INTERVAL_SECONDS" ] && full_pass=1

memory_count() { # comma-separated tags -> live count of memories carrying ALL of them ("" on failure)
  local tags="$1" query out code
  [ -n "$tags" ] || return 0
  query="$(printf '%s' "$tags" | "$PY" -c '
import sys, urllib.parse
tags = [t for t in sys.stdin.read().split(",") if t]
print(urllib.parse.urlencode([("tags", t) for t in tags] + [("tags_match", "all_strict"), ("limit", 1)]))')"
  out="$(api GET "/v1/default/banks/${BANK}/memories/list?${query}")"
  code="$(echo "$out" | tail -n1)"
  [ "$code" = "200" ] || return 0
  echo "$out" | sed '$d' | "$PY" -c 'import json,sys
try:
    total = json.load(sys.stdin).get("total")
    print(total if isinstance(total, int) else "")
except Exception: print("")' 2>/dev/null
}

poll_operation() { # op_id -> prints content_len on success, returns 1 on failure/timeout
  local op_id="$1" waited=0 out status
  while [ "$waited" -lt "$POLL_TIMEOUT_SECONDS" ]; do
    sleep 20; waited=$((waited + 20))
    out="$(api GET "/v1/default/banks/${BANK}/operations/${op_id}" | sed '$d')"
    status="$(echo "$out" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("status",""))
except Exception: print("")' 2>/dev/null)"
    case "$status" in
      completed)
        echo "$out" | "$PY" -c 'import json,sys
d=json.load(sys.stdin); print((d.get("result_metadata") or {}).get("content_len", 0))'
        return 0 ;;
      failed) return 1 ;;
    esac
  done
  return 1
}

all_ok=1
while read -r m mode tags; do
  [ -n "$m" ] || continue
  current="$(memory_count "$tags")"
  baseline_file="$ACTIVITY_DIR/${m}.delta"
  [ "$full_pass" = 1 ] && baseline_file="$ACTIVITY_DIR/${m}.full"
  baseline=""
  [ -f "$baseline_file" ] && baseline="$(cat "$baseline_file" 2>/dev/null)"
  if [ "$FORCE" != 1 ] && [ -n "$current" ] && [ -n "$baseline" ] && [ "$current" = "$baseline" ]; then
    echo "$(TS) MODEL_REFRESH_SKIP ${m} no_new_memories count=${current}"
    continue
  fi
  if [ "$full_pass" = 1 ]; then
    if [ "$mode" = "delta" ]; then
      code="$(api POST "/v1/default/banks/${BANK}/mental-models/${m}/clear" | tail -n1)"
      if [ "$code" != "200" ] && [ "$code" != "202" ] && [ "$code" != "204" ]; then
        echo "$(TS) MODEL_REFRESH_FAIL ${m} clear_http_${code}"; all_ok=0; sleep "$SLEEP_BETWEEN_MODELS"; continue
      fi
      echo "$(TS) MODEL_REFRESH_FULL ${m}"
    else
      # Not delta mode: refresh alone rebuilds it, and a failed refresh must not leave it empty (issue #272).
      echo "$(TS) MODEL_REFRESH_FULL ${m} clear_skipped mode=${mode}"
    fi
  fi
  resp="$(api POST "/v1/default/banks/${BANK}/mental-models/${m}/refresh")"
  code="$(echo "$resp" | tail -n1)"
  op_id="$(echo "$resp" | sed '$d' | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("operation_id",""))
except Exception: print("")' 2>/dev/null)"
  if [ "$code" != "200" ] && [ "$code" != "202" ] || [ -z "$op_id" ]; then
    echo "$(TS) MODEL_REFRESH_FAIL ${m} refresh_http_${code}"; all_ok=0; sleep "$SLEEP_BETWEEN_MODELS"; continue
  fi
  if chars="$(poll_operation "$op_id")"; then
    echo "$(TS) MODEL_REFRESH_OK ${m} ${chars}"
    # Record the fingerprint this refresh consumed. A failed count leaves no baseline so the
    # next run refreshes again instead of trusting a stale number.
    if [ -n "$current" ]; then
      echo "$current" > "$ACTIVITY_DIR/${m}.delta"
      [ "$full_pass" = 1 ] && echo "$current" > "$ACTIVITY_DIR/${m}.full"
    else
      rm -f "$ACTIVITY_DIR/${m}.delta"
      [ "$full_pass" = 1 ] && rm -f "$ACTIVITY_DIR/${m}.full"
    fi
  else
    echo "$(TS) MODEL_REFRESH_FAIL ${m} operation_${op_id}"; all_ok=0
  fi
  sleep "$SLEEP_BETWEEN_MODELS"
done <<EOF_MODELS
$MODEL_ROWS
EOF_MODELS

if [ "$full_pass" = 1 ] && [ "$all_ok" = 1 ]; then
  echo "$NOW" > "$STATE_FILE"
fi
echo "$(TS) MODEL_REFRESH_RUN_DONE full_pass=${full_pass} all_ok=${all_ok}"
[ "$all_ok" = 1 ]
