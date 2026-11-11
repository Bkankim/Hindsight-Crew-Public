#!/usr/bin/env bash
# model-audit.sh - thin wrapper for model_audit.py (weekly launchd job, issue #10).
# Host settings (bank, owner identity, notification target) come from .env.host.local
# (issue #186, scripts/hc-host-env.sh). The notification target is HC_NOTIFY_DISCORD_*, else the
# legacy GGG_DISCORD_* lines of ~/.env, extracted line by line and NOT sourced: that file is not
# shell-clean (unquoted metacharacters abort a `.` mid-file and can corrupt values).
# Secrets are never committed.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
[ -n "${HOME:-}" ] || { echo "$(date '+%Y-%m-%d %H:%M:%S') MODEL_AUDIT_FAIL - home_unset"; exit 1; }
[ -f .env.local ] && { set -a; . ./.env.local; set +a; }
. "$ROOT/scripts/hc-python.sh"
hc_find_python || { echo "$(date '+%Y-%m-%d %H:%M:%S') MODEL_AUDIT_FAIL - python_missing"; exit 1; }
. "$ROOT/scripts/hc-host-env.sh"; hc_load_host_env "$ROOT"; hc_resolve_notify
for key in HC_PERSONAL_BANK HC_OWNER; do
  [ -n "${!key:-}" ] || { echo "$(date '+%Y-%m-%d %H:%M:%S') MODEL_AUDIT_FAIL - config_missing $key"; exit 1; }
done
exec "$PY" scripts/model_audit.py
