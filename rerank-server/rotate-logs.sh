#!/bin/bash
# rotate-logs.sh - size-based copytruncate rotation for the rerank sidecar logs (issue #2)
# and the gateway audit logs (issue #12, #141).
# launchd keeps server.log/probe.log open O_APPEND (StandardOutPath), so
# copy-then-truncate needs NO sidecar restart: the writer's next append lands at
# the new EOF. Rotated generations stay gzipped IN THE SAME directory - the
# payload non-recording invariant is untouched (nothing leaves the log dir).
# Log tokens: LOG_ROTATE_OK <file> <bytes> / LOG_ROTATE_SKIP <file> <bytes> /
# LOG_ROTATE_FAIL <file> <bytes> (cp, truncate or gzip failed; nothing was shifted if cp/truncate failed).
# The audit logs are written by the gateway CONTAINER on a colima virtiofs bind mount, so they are
# truncated inside the container (issue #180, see rotate_one); the sidecar logs on the host.
set -u
TS() { date '+%Y-%m-%d %H:%M:%S'; }
[ -n "${HOME:-}" ] || { echo "$(TS) LOG_ROTATE_FAIL - home_unset"; exit 1; }
LOG_DIR="${HOME}/Library/Logs/hindsight-rerank"
MAX_BYTES="${RERANK_LOG_MAX_BYTES:-10485760}"   # 10MB
GENERATIONS="${RERANK_LOG_GENERATIONS:-3}"

# The gateway's recall ledger (issue #12, audit/recall-queries.log) had no rotation (review
# P3, 2026-09-23). The gateway opens it per write in append mode, so the same copytruncate
# is safe without a restart. Only the on-disk log rotates; the in-memory ledger is untouched.
AUDIT_DIR="${HC_AUDIT_DIR:-$(cd "$(dirname "$0")/.." && pwd)/audit}"

# Issue #180: both audit logs are appended to by the gateway container. A host-side `: >` on the
# virtiofs bind mount leaves the guest with a stale size, and its next append writes NUL bytes
# up to the old length (reproduced 4 of 6 rounds on a test container, #166). So the truncate runs
# INSIDE the container (writer and truncater share one kernel) via `docker exec`, against the
# in-container path of the compose bind mount (./audit -> /audit). The LaunchAgent has a minimal
# env, so the target is the container name (not compose, which needs the project dir and
# .env.local) and the docker binary is found here: HC_DOCKER_BIN (used alone when set, a bad
# value, empty included, is a failure, never a fallthrough) > PATH > the common install locations below
# (HC_DOCKER_FALLBACKS, space separated, replaces that list; an empty value means none).
# Safety checks around the exec: the container's /audit mount source must resolve (realpath) to
# AUDIT_DIR, else the exec would empty a log this run never copied (HC_AUDIT_DIR=scratch with the
# default container name would hit the operational ledger), and the host file must have shrunk
# afterwards, else exec "succeeded" on a different mount and the generations would only be
# duplicated. Either check failing, like no docker/container or an exec that left the file as it was, is
# LOG_ROTATE_FAIL; an exec that exited non-zero but did shrink the file counts as done.
GATEWAY_CONTAINER="${HC_ROTATE_GATEWAY_CONTAINER:-hindsight-crew-gateway-1}"
AUDIT_CONTAINER_DIR=/audit
find_docker() {
  local c
  if [ "${HC_DOCKER_BIN+set}" = set ]; then   # set but empty is a bad value too: no candidate search
    [ -n "$HC_DOCKER_BIN" ] && [ -x "$HC_DOCKER_BIN" ] && [ -f "$HC_DOCKER_BIN" ] && echo "$HC_DOCKER_BIN"
    return
  fi
  if c="$(command -v docker 2>/dev/null)" && [ -x "$c" ]; then
    echo "$c"
    return
  fi
  local fallbacks
  if [ "${HC_DOCKER_FALLBACKS+set}" = set ]; then
    fallbacks="$HC_DOCKER_FALLBACKS"
  else
    fallbacks="${HOME}/.docker/bin/docker /opt/homebrew/bin/docker /usr/local/bin/docker /Applications/Docker.app/Contents/Resources/bin/docker"
  fi
  for c in $fallbacks; do
    if [ -x "$c" ] && [ -f "$c" ]; then
      echo "$c"
      return
    fi
  done
}
# audit_mount_matches <docker bin>: the container's /audit bind source is the directory this run rotates.
audit_mount_matches() {
  local src a b
  src="$("$1" inspect -f "{{range .Mounts}}{{if eq .Destination \"${AUDIT_CONTAINER_DIR}\"}}{{.Source}}{{end}}{{end}}" "$GATEWAY_CONTAINER" 2>/dev/null)" || return 1
  [ -n "$src" ] || return 1
  a="$(cd "$src" 2>/dev/null && pwd -P)" || return 1
  b="$(cd "$AUDIT_DIR" 2>/dev/null && pwd -P)" || return 1
  [ -n "$a" ] && [ "$a" = "$b" ]
}
# prepare_container_truncate: finds docker and checks the mount BEFORE the cp, so the cp-to-truncate
# window (lines lost, a .1 beside the live file) holds only the exec, and a mismatch copies nothing.
# Sets CONTAINER_DOCKER; no docker, no container or a mount that is not AUDIT_DIR return non-zero and the
# caller reports LOG_ROTATE_FAIL (there is deliberately no host fallback).
CONTAINER_DOCKER=""
prepare_container_truncate() {
  CONTAINER_DOCKER="$(find_docker)"
  [ -n "$CONTAINER_DOCKER" ] || return 1
  audit_mount_matches "$CONTAINER_DOCKER"
}
# truncate_in_container <host path of an audit log>: returns the exec's status (rotate_one re-checks the file).
truncate_in_container() {
  "$CONTAINER_DOCKER" exec "$GATEWAY_CONTAINER" truncate -s 0 "${AUDIT_CONTAINER_DIR}/$(basename "$1")" >/dev/null 2>&1
}

# The gateway audit log (issue #141, audit/gateway.log) is the attribution record and the
# readers (scripts/model_audit.py, duplicate_recall.py, tag_hygiene.py, bench/runner.py) also
# read the .N.gz generations (#177, #178), and it is the attribution record, so it gets its own
# knobs instead of sharing the sidecar's diagnostic retention: same 10MB default threshold, more
# generations (5).
# gateway/audit.py reopens it with open(path, "a") for every line (single process, one
# lock), so copytruncate needs no gateway restart and no MCP session is cut.
AUDIT_MAX_BYTES="${HC_AUDIT_LOG_MAX_BYTES:-10485760}"   # 10MB
AUDIT_GENERATIONS="${HC_AUDIT_LOG_GENERATIONS:-5}"
case "$AUDIT_GENERATIONS" in ''|*[!0-9]*) AUDIT_GENERATIONS=5 ;; esac   # keep the audit default, not rotate_one's

# rotate_one <path> <max-bytes> <generations> [container]
# With a 4th argument "container" the truncate runs inside the gateway container (audit logs).
# A non-numeric knob (HC_AUDIT_LOG_MAX_BYTES=10M) would make `[ -lt ]` error out and rotate on
# every run, wiping the gz history, so each knob falls back to its default when it is not a
# plain number (or has more than 15 digits); generations below 1 count as 1. Digits are then
# forced to base 10 (08 -> 8): bash arithmetic reads a leading 0 as octal and 08/09 would abort
# the script after the truncate. All arithmetic on the knobs is done here, before the cp.
rotate_one() {
  local path="$1" max="$2" gens="$3" where="${4:-host}" f sz i
  case "$max" in ''|*[!0-9]*) max=10485760 ;; esac
  case "$gens" in ''|*[!0-9]*) gens=3 ;; esac
  [ "${#max}" -le 15 ] || max=10485760
  [ "${#gens}" -le 15 ] || gens=3
  max=$((10#$max))
  gens=$((10#$gens))
  [ "$gens" -ge 1 ] || gens=1
  f="$(basename "$path")"
  [ -f "$path" ] || return 0
  # wc -c, not stat: `stat -f%z` is BSD-only (GNU stat -f means --file-system, so it does not
  # return the byte size and every file would SKIP on Linux/CI).
  sz="$(wc -c < "$path" 2>/dev/null | tr -d '[:space:]')"
  case "$sz" in ''|*[!0-9]*) sz=0 ;; esac
  if [ "$sz" -lt "$max" ]; then
    echo "$(TS) LOG_ROTATE_SKIP ${f} ${sz}"
    return 0
  fi
  # copytruncate: cp preserves the tail for diagnosis, then truncate in place so
  # the O_APPEND writer keeps writing to the same inode. Lines appended between the cp read
  # and the truncate are lost (the pre-existing limit of this scheme). Generations are shifted
  # only after the copy and the truncate succeeded, so a failed rotation (full disk, read-only
  # dir) cannot push old generations out, and it is reported as LOG_ROTATE_FAIL, not OK.
  # For a container-written log the truncate is the in-container one (issue #180).
  if [ "$where" = container ]; then
    # The exec's exit status alone is not trusted either way. Shrunk host file: the truncate really
    # happened, so even a non-zero exit (docker CLI cut off after the container finished) takes the
    # success path - the .1 is then the only copy of the old content and must not be deleted. Not
    # shrunk (or wc failed): FAIL, unless the file was empty to begin with and the exec exited 0.
    local rc=0 after
    if ! { prepare_container_truncate && cp "$path" "${path}.1"; }; then
      rm -f "${path}.1"
      echo "$(TS) LOG_ROTATE_FAIL ${f} ${sz}"
      return 0
    fi
    truncate_in_container "$path" || rc=$?
    after="$(wc -c < "$path" 2>/dev/null | tr -d '[:space:]')"
    case "$after" in ''|*[!0-9]*) after="$sz" ;; esac
    if ! { [ "$after" -lt "$sz" ] || { [ "$sz" -eq 0 ] && [ "$rc" -eq 0 ]; }; }; then
      rm -f "${path}.1"
      echo "$(TS) LOG_ROTATE_FAIL ${f} ${sz}"
      return 0
    fi
  elif ! { cp "$path" "${path}.1" && : > "$path"; }; then
    rm -f "${path}.1"
    echo "$(TS) LOG_ROTATE_FAIL ${f} ${sz}"
    return 0
  fi
  i=$((gens - 1))
  while [ "$i" -ge 1 ]; do
    [ -f "${path}.${i}.gz" ] && mv -f "${path}.${i}.gz" "${path}.$((i + 1)).gz"
    i=$((i - 1))
  done
  if gzip -f "${path}.1"; then
    echo "$(TS) LOG_ROTATE_OK ${f} ${sz}"
  else
    echo "$(TS) LOG_ROTATE_FAIL ${f} ${sz}"
  fi
}

rotate_one "${LOG_DIR}/server.log" "$MAX_BYTES" "$GENERATIONS"
rotate_one "${LOG_DIR}/probe.log" "$MAX_BYTES" "$GENERATIONS"
rotate_one "${AUDIT_DIR}/recall-queries.log" "$MAX_BYTES" "$GENERATIONS" container
rotate_one "${AUDIT_DIR}/gateway.log" "$AUDIT_MAX_BYTES" "$AUDIT_GENERATIONS" container
exit 0
