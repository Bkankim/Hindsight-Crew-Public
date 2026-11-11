#!/usr/bin/env bash
# scheduled-checks.sh - daily launchd wrapper for scheduled_checks.py (issues #22, #44).
# Same host settings as model-audit.sh (.env.host.local, issue #186; the notification target
# falls back to the legacy GGG_* lines of ~/.env, extracted line by line - that file is not
# shell-clean). scheduled_checks.py reads the target under the GGG_* names, so they are
# exported from the resolved HC_NOTIFY_DISCORD_* pair. launchd's PATH is minimal, so
# docker/uv locations are added explicitly. Secrets are never printed or committed.
set -u
[ -n "${HOME:-}" ] || { echo "$(date '+%Y-%m-%d %H:%M:%S') SCHEDULED_FAIL - home_unset"; exit 1; }
export PATH="/opt/homebrew/bin:/usr/local/bin:${HOME}/.local/bin:${PATH}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
. "$ROOT/scripts/hc-python.sh"
hc_find_python || { echo "$(date '+%Y-%m-%d %H:%M:%S') SCHEDULED_FAIL - python_missing"; exit 1; }
. "$ROOT/scripts/hc-host-env.sh"; hc_load_host_env "$ROOT"; hc_resolve_notify
if [ -n "${HC_NOTIFY_DISCORD_BOT_TOKEN:-}" ] && [ -n "${HC_NOTIFY_DISCORD_CHANNEL_ID:-}" ]; then
  export GGG_DISCORD_BOT_TOKEN="$HC_NOTIFY_DISCORD_BOT_TOKEN" GGG_DISCORD_CHANNEL_ID="$HC_NOTIFY_DISCORD_CHANNEL_ID"
fi
exec "$PY" scripts/scheduled_checks.py "$@"
