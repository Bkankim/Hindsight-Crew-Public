#!/usr/bin/env bash
# item-roles-observe.sh - hourly launchd wrapper for item_roles_observe.py (issue #415, R7-8).
# Same host settings as scheduled-checks.sh: .env.host.local (HC_LAUNCHD_PREFIX for the sidecar
# label, the notification target HC_NOTIFY_DISCORD_*, legacy GGG_* fallback of ~/.env). The
# Discord sender (scheduled_checks.notify) reads the target under the GGG_* names, so they are
# exported from the resolved pair. Read-only: the role store is opened with a `mode=ro` URI.
# Secrets are never printed or committed.
set -u
[ -n "${HOME:-}" ] || { echo "$(date '+%Y-%m-%d %H:%M:%S') ITEM_ROLES_OBSERVE fail home_unset"; exit 1; }
export PATH="/usr/local/bin:${HOME}/.local/bin:${PATH}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
. "$ROOT/scripts/hc-python.sh"
hc_find_python || { echo "$(date '+%Y-%m-%d %H:%M:%S') ITEM_ROLES_OBSERVE fail python_missing"; exit 1; }
. "$ROOT/scripts/hc-host-env.sh"; hc_load_host_env "$ROOT"; hc_resolve_notify
if [ -n "${HC_NOTIFY_DISCORD_BOT_TOKEN:-}" ] && [ -n "${HC_NOTIFY_DISCORD_CHANNEL_ID:-}" ]; then
  export GGG_DISCORD_BOT_TOKEN="$HC_NOTIFY_DISCORD_BOT_TOKEN" GGG_DISCORD_CHANNEL_ID="$HC_NOTIFY_DISCORD_CHANNEL_ID"
fi
exec "$PY" scripts/item_roles_observe.py "$@"
