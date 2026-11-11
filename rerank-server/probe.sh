#!/bin/bash
# Watchdog probe for $HC_LAUNCHD_PREFIX.hindsight-rerank (label prefix: HC_LAUNCHD_PREFIX in
# .env.host.local; ./install defaults it to the prefix in scripts/install/hc_install.py, a manual
# scripts/render-launchd.py install has no default and refuses with config_missing).
# Liveness via GET /health (worker-task aware) + state via GET /info.
# Alerts on: repeated unreachable/non-200, device != mps, degraded=true.
# Requires 2 consecutive in-run failures (3s apart) before alerting: this
# absorbs sub-3s blips only. A restart/deploy window (KeepAlive respawn is
# throttled to 60s; /health stays 503 until model load + warmup) is
# INTENTIONALLY alerted - capped at one notification per 30 min.
# Notification is argv-passed (no AppleScript injection) and the suppression
# timestamp is written ONLY on successful delivery; delivery failure is
# logged as RERANK_PROBE_NOTIFY_FAILED so a dead channel stays visible.
set -u

TS() { date '+%Y-%m-%d %H:%M:%S'; }
[ -n "${HOME:-}" ] || { echo "$(TS) RERANK_PROBE_FAIL home_unset"; exit 1; }

BASE_URL="http://127.0.0.1:8787"
STATE_DIR="${HOME}/Library/Logs/hindsight-rerank"
STATE_FILE="${STATE_DIR}/probe.last-alert"
mkdir -p "$STATE_DIR" 2>/dev/null || true
SUPPRESS_SECONDS=1800
RETRY_DELAY=3
# A missing python3 must stay visible: PY="" makes check_once fail with python_missing, which
# goes through the normal alert path instead of the watchdog dying silently under launchd.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/hc-python.sh"
hc_find_python || PY=""

check_once() {
  # Sets globals: fail_reason ("" when healthy), device, degraded
  fail_reason=""
  device=""
  degraded=""

  local health
  health="$(curl -s -m 5 "${BASE_URL}/health" 2>/dev/null)"
  if [ "$?" -ne 0 ] || [ "$health" != "ok" ]; then
    fail_reason="health_${health:-unreachable}"
    return
  fi

  local body http_code payload
  body="$(curl -s -m 5 -w '\n%{http_code}' "${BASE_URL}/info" 2>/dev/null)"
  if [ "$?" -ne 0 ]; then
    fail_reason="info_unreachable"
    return
  fi
  http_code="$(echo "$body" | tail -n1)"
  payload="$(echo "$body" | sed '$d')"
  if [ "$http_code" != "200" ]; then
    fail_reason="info_http_${http_code:-none}"
    return
  fi

  if [ -z "$PY" ]; then
    fail_reason="python_missing"
    return
  fi
  device="$(echo "$payload" | "$PY" -c 'import json,sys
try:
    print(json.load(sys.stdin).get("device",""))
except Exception:
    print("")' 2>/dev/null)"
  degraded="$(echo "$payload" | "$PY" -c 'import json,sys
try:
    print(str(json.load(sys.stdin).get("degraded","")).lower())
except Exception:
    print("")' 2>/dev/null)"

  if [ "$device" != "mps" ]; then
    fail_reason="device_${device:-unknown}"
  fi
  if [ "$degraded" = "true" ]; then
    fail_reason="${fail_reason:+${fail_reason}_}degraded"
  fi
}

check_once
if [ -n "$fail_reason" ]; then
  first_reason="$fail_reason"
  sleep "$RETRY_DELAY"
  check_once
fi

if [ -z "$fail_reason" ]; then
  echo "$(TS) RERANK_PROBE_OK device=${device} degraded=${degraded:-false}"
  exit 0
fi

reason="${first_reason:+${first_reason}->}${fail_reason}"
echo "$(TS) RERANK_PROBE_ALERT ${reason}"

last=0
if [ -f "$STATE_FILE" ]; then
  last="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
fi
# Numeric guard: corrupt state must fail OPEN (notify), not abort the script.
case "$last" in ''|*[!0-9]*) last=0 ;; esac

NOW="$(date +%s)"
elapsed=$((NOW - last))
# Future-epoch squat also fails open.
if [ "$elapsed" -lt 0 ]; then last=0; elapsed=$SUPPRESS_SECONDS; fi

if [ "$elapsed" -ge "$SUPPRESS_SECONDS" ]; then
  if notify_err="$(/usr/bin/osascript -e 'on run argv
display notification (item 1 of argv) with title "Hindsight Rerank Alert"
end run' -- "$reason" 2>&1)"; then
    echo "$NOW" > "$STATE_FILE"
  else
    echo "$(TS) RERANK_PROBE_NOTIFY_FAILED ${notify_err}"
  fi
fi
exit 0
