#!/usr/bin/env bash
# model-watch.sh - thin wrapper for model_watch.py (daily launchd job, issue #428).
# Same environment as model-audit.sh: host settings from .env.host.local (scripts/hc-host-env.sh), the
# notification target HC_NOTIFY_DISCORD_*, else the legacy GGG_DISCORD_* lines of ~/.env (extracted line
# by line, never sourced). Arguments pass through (the launchd job gives --grace-hours).
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
[ -n "${HOME:-}" ] || { echo "$(date '+%Y-%m-%d %H:%M:%S') MODEL_WATCH_FAIL - home_unset"; exit 1; }
[ -f .env.local ] && { set -a; . ./.env.local; set +a; }
. "$ROOT/scripts/hc-python.sh"
hc_find_python || { echo "$(date '+%Y-%m-%d %H:%M:%S') MODEL_WATCH_FAIL - python_missing"; exit 1; }
. "$ROOT/scripts/hc-host-env.sh"; hc_load_host_env "$ROOT"; hc_resolve_notify
for key in HC_PERSONAL_BANK HC_OWNER; do
  [ -n "${!key:-}" ] || { echo "$(date '+%Y-%m-%d %H:%M:%S') MODEL_WATCH_FAIL - config_missing $key"; exit 1; }
done
echo "$(date '+%Y-%m-%d %H:%M:%S') MODEL_WATCH_START"
exec "$PY" scripts/model_watch.py "$@"
