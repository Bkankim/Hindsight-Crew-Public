#!/usr/bin/env bash
# teardown.sh — stop the stack. Pass -v to also remove data volumes.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
docker compose --env-file .env.local down "$@"
docker rm -f hc-restore-hs >/dev/null 2>&1 || true
docker volume rm hc-restore-pg0 >/dev/null 2>&1 || true
