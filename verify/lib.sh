#!/usr/bin/env bash
# verify/lib.sh — shared helpers. Gates run on the HOST and hit the published gateway
# (the single front door); they NEVER touch hindsight 8888 directly. bash 3.2 compatible.
# One setup exception (#473): gate2 creates the team bank upstream inside the hindsight container (upstream
# key on stdin), since no member may provision a team bank through the gateway. Every check stays on the gateway.
set -o pipefail
HC_GATE_PASS=0; HC_GATE_FAIL=1; HC_GATE_NOTIMPL=2

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

hc_log() { printf '    %s\n' "$*" >&2; }
_hc_fatal() { echo "verify/lib.sh: $*" >&2; exit 1; }

# ---- target selection (contract of #242) ---------------------------------------------------------------
# Order: a value the caller put in the environment beats HC_ENV_FILE, which beats the defaults. The env file is
# sourced with `set -a`, so it would overwrite the caller's value: save the caller's (an empty value counts as set)
# before sourcing and put it back after. Same method as scripts/backup.sh and scripts/mcp-probe.sh.
_hc_kept=""
for _hc_v in HC_GATEWAY_BIND HC_PG0_VOLUME HC_HINDSIGHT_CONTAINER HC_COMPOSE_PROJECT HC_COMPOSE_FILES HC_SECRETS_DIR; do
  if [ -n "${!_hc_v+x}" ]; then
    printf -v "_hc_keep_$_hc_v" '%s' "${!_hc_v}"
    _hc_kept="$_hc_kept $_hc_v"
  fi
done
# Only the default .env.local may be absent. A file the caller NAMED that is missing would silently fall back to
# the default target (production), so it ends the gate instead.
_hc_env_explicit=0; if [ -n "${HC_ENV_FILE:-}" ]; then _hc_env_explicit=1; fi
HC_ENV_FILE="${HC_ENV_FILE:-$ROOT/.env.local}"
case "$HC_ENV_FILE" in /*) : ;; *) HC_ENV_FILE="$ROOT/$HC_ENV_FILE" ;; esac
if [ "$_hc_env_explicit" = 1 ] && [ ! -f "$HC_ENV_FILE" ]; then _hc_fatal "HC_ENV_FILE $HC_ENV_FILE does not exist"; fi
# A default this file filled in must not reach child tools as if the caller had named it (an exported-but-empty
# HC_ENV_FILE keeps its export attribute through the assignment above, and a child would then refuse the absent default).
[ "$_hc_env_explicit" = 1 ] || export -n HC_ENV_FILE
[ -f "$HC_ENV_FILE" ] && { set -a; . "$HC_ENV_FILE"; set +a; }
for _hc_v in $_hc_kept; do
  _hc_s="_hc_keep_$_hc_v"
  printf -v "$_hc_v" '%s' "${!_hc_s}"
  unset "$_hc_s"
done
unset _hc_v _hc_s _hc_kept

HC_SECRETS_DIR="${HC_SECRETS_DIR:-$ROOT/secrets}"
case "$HC_SECRETS_DIR" in /*) : ;; *) HC_SECRETS_DIR="$ROOT/$HC_SECRETS_DIR" ;; esac
[ -f "$HC_SECRETS_DIR/demo-tokens.env" ] && { set -a; . "$HC_SECRETS_DIR/demo-tokens.env"; set +a; }
HC_GW="http://${HC_GATEWAY_BIND:-127.0.0.1:8888}"
HC_TENANT="${HC_TENANT:-default}"

# hc_project - the compose project this run targets: HC_COMPOSE_PROJECT > COMPOSE_PROJECT_NAME > the repo
# directory name in compose's normal form (lowercase, [a-z0-9_-], leading -_ removed).
hc_project() {
  local p="${HC_COMPOSE_PROJECT:-${COMPOSE_PROJECT_NAME:-}}"
  [ -n "$p" ] || p="$(basename "$ROOT" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-' | sed 's/^[-_]*//')"
  printf '%s' "$p"
}

# hc_compose <args...> - docker compose in the shape every target-aware tool uses:
#   docker compose [--env-file <HC_ENV_FILE> (only when it exists)] --project-directory <repo root>
#                  [-p <HC_COMPOSE_PROJECT> (only when set)] -f <file>...
hc_compose() {
  local a=(docker compose) f files
  if [ -f "$HC_ENV_FILE" ]; then a+=(--env-file "$HC_ENV_FILE"); fi
  a+=(--project-directory "$ROOT")
  if [ -n "${HC_COMPOSE_PROJECT:-}" ]; then a+=(-p "$HC_COMPOSE_PROJECT"); fi
  IFS=: read -r -a files <<< "${HC_COMPOSE_FILES:-docker-compose.yml}"   # read never globs
  for f in ${files[@]+"${files[@]}"}; do
    [ -n "$f" ] || continue
    case "$f" in /*) : ;; *) f="$ROOT/$f" ;; esac
    a+=(-f "$f")
  done
  "${a[@]}" "$@"
}

# hc_resolve_container - HC_HINDSIGHT_CONTAINER when given, else the hindsight container of the compose shape
# above (`ps -q hindsight`, converted to its NAME). Read-only. Never ends the shell: an empty
# HC_HINDSIGHT_CONTAINER afterwards means "not found" and HC_CONTAINER_LOOKUP_RC holds compose's exit status.
# compose's stderr is dropped on purpose - a bad env file makes it quote the offending line, which can be a secret.
HC_CONTAINER_LOOKUP_RC=0
hc_resolve_container() {
  HC_CONTAINER_LOOKUP_RC=0
  [ -z "${HC_HINDSIGHT_CONTAINER:-}" ] || return 0
  local out id name
  out="$(hc_compose ps -q hindsight 2>/dev/null)"; HC_CONTAINER_LOOKUP_RC=$?
  id="$(printf '%s\n' "$out" | head -1)"
  [ -n "$id" ] || return 1
  name="$(docker inspect -f '{{.Name}}' "$id" 2>/dev/null)" || return 1
  name="${name#/}"
  [ -n "$name" ] || return 1
  HC_HINDSIGHT_CONTAINER="$name"; export HC_HINDSIGHT_CONTAINER
}

# hc_require_container - for gates that run `docker exec`: end the gate with a reason when nothing was found.
hc_require_container() {
  [ -n "${HC_HINDSIGHT_CONTAINER:-}" ] && return 0
  if [ "$HC_CONTAINER_LOOKUP_RC" -ne 0 ]; then
    hc_log "hindsight container not found: 'docker compose ps -q hindsight' failed (exit $HC_CONTAINER_LOOKUP_RC; docker unreachable, or compose cannot read ${HC_ENV_FILE} and the compose files)"
  else
    hc_log "hindsight container not found: 'docker compose ps -q hindsight' in $ROOT printed nothing (stack down) or the container could not be inspected"
  fi
  hc_log "start the stack, or name the container explicitly with HC_HINDSIGHT_CONTAINER"
  exit 1
}

# Target line (stderr, once per process) and the production/copy guard. Both come before any request or exec of
# the gate. Only gates that look up the container (HC_GATE_NEEDS_CONTAINER=1, set before sourcing this file) fill the
# container column; every other column this file does not resolve is `-`.
_hc_container="-"
if [ "${HC_GATE_NEEDS_CONTAINER:-0}" = 1 ]; then
  hc_resolve_container || true
  [ -z "${HC_HINDSIGHT_CONTAINER:-}" ] || _hc_container="$HC_HINDSIGHT_CONTAINER"
fi
_hc_bind="${HC_GATEWAY_BIND:-127.0.0.1:8888}"
_hc_proj="$(hc_project)"
printf 'target: gw=%s project=%s container=%s pg0=-\n' "$_hc_bind" "$_hc_proj" "$_hc_container" >&2
# HC_TARGET=next marks the copy stack: every column resolved here must be the copy's. Without the marker anything
# that looks like the copy is refused, so neither side is reached by accident.
if [ "${HC_TARGET:-}" = next ]; then
  [ "$_hc_bind" = 127.0.0.1:18888 ] || _hc_fatal "HC_TARGET=next but the gateway is $_hc_bind, not 127.0.0.1:18888"
  [ "$_hc_proj" = hc-next ] || _hc_fatal "HC_TARGET=next but the project is $_hc_proj, not hc-next"
  # gate6 hands the production way of working to restore-test.sh (production hfcache mounted read-write, production
  # hindsight stopped when the VM is short of memory): not a gate of the copy, alone or inside verify-all.
  case "${0##*/}" in gate6_restore.sh) _hc_fatal "HC_TARGET=next: gate6 (restore-test) is not run against the copy stack" ;; esac
  if [ "${HC_GATE_NEEDS_CONTAINER:-0}" = 1 ]; then
    case "$_hc_container" in
      hc-next-*) : ;;
      -) _hc_fatal "HC_TARGET=next but the hindsight container was not found (the copy stack is down?)" ;;
      *) _hc_fatal "HC_TARGET=next but the container is $_hc_container, not hc-next-*" ;;
    esac
  fi
else
  case "$_hc_bind" in *:18888) _hc_fatal "the gateway is $_hc_bind, a copy stack address - set HC_TARGET=next" ;; esac
  case "$_hc_proj" in hc-next*) _hc_fatal "the project is $_hc_proj, a copy stack - set HC_TARGET=next" ;; esac
  case "$_hc_container" in hc-next-*) _hc_fatal "the container is $_hc_container, a copy stack container - set HC_TARGET=next" ;; esac
fi
unset _hc_container _hc_bind _hc_proj

# hc_curl_auth TOKEN <curl-args...> - curl with the Bearer header read from stdin (`-H @-`).
# The token must never sit on a curl argv: `ps` shows every argv to every local user. printf is
# a shell builtin, so the header never becomes an argv anywhere. Empty TOKEN = no header (the
# unauthenticated probes of gates 4/5). curl's stdin belongs to the header: pass a body only as a
# -d argument, never as `-d @-` / `--data-binary @-` / `-T -`.
hc_curl_auth() {
  local t="$1"; shift
  if [ -n "$t" ]; then
    printf 'authorization: Bearer %s\n' "$t" | curl -H @- "$@"
  else
    curl "$@"
  fi
}

# rest_code METHOD TOKEN PATH [BODY]  -> prints HTTP status code
rest_code() {
  local m="$1" t="$2" p="$3" b="${4:-}"
  local a=(-s -o /dev/null -w '%{http_code}' --max-time 25 -X "$m" "$HC_GW$p" -H 'content-type: application/json')
  [ -n "$b" ] && a+=(-d "$b")
  hc_curl_auth "$t" "${a[@]}"
}
# rest_body METHOD TOKEN PATH [BODY]  -> prints response body
rest_body() {
  local m="$1" t="$2" p="$3" b="${4:-}"
  local a=(-s --max-time 25 -X "$m" "$HC_GW$p" -H 'content-type: application/json')
  [ -n "$b" ] && a+=(-d "$b")
  hc_curl_auth "$t" "${a[@]}"
}
hc_bank_path() { printf '/v1/%s/banks/%s' "$HC_TENANT" "$1"; }

# ---- MCP surface (gateway-routed) -------------------------------------------
# The handshake itself is NOT reimplemented here: scripts/mcp-probe.sh owns the one
# initialize -> Mcp-Session-Id -> notifications/initialized sequence in this repo, and is
# written to be sourceable for exactly this reuse. Only its transport hooks are swapped:
#   BASE   = the published gateway (never hindsight 8888 — gates use the front door only)
#            (the only upstream-direct call in verify/ is gate2's team-bank setup, see the header)
#   PATH   = /mcp/<bank>/  and NO X-Bank-Id header: the gateway resolves the bank from the
#            URL path, unlike upstream which reads the header. Sending X-Bank-Id here would
#            be dropped by the gateway's header allowlist anyway.
#   AUTH   = a demo principal's Bearer (secrets/demo-tokens.env), never the upstream key.
# probe's own defaults stay upstream-direct so re-collection can never be polluted by
# gateway-rewritten output (see the ban comment at the top of mcp-probe.sh).
_hc_mcp_probe_loaded=0
_hc_load_mcp_probe() {
  [ "$_hc_mcp_probe_loaded" = 1 ] && return 0
  [ -f "$ROOT/scripts/mcp-probe.sh" ] || { hc_log "scripts/mcp-probe.sh missing"; return 1; }
  # mcp-probe.sh runs `set -u` at the top; sourcing it would leak that option into gates
  # that were written without it, so the caller's -u state is restored right after.
  local had_u=0; case "$-" in *u*) had_u=1 ;; esac
  # The probe has its OWN rest_body/rest_code (single-argument fixture splitters, upstream
  # flavour) that would silently shadow this file's HTTP helpers of the same name. Save the
  # gate-facing definitions and put them back after the source.
  local saved; saved="$(declare -f rest_body rest_code)"
  # mcp-probe treats a HC_ENV_FILE that is set as one the user NAMED (and refuses a missing one). This file fills in
  # the default itself, so hand the probe the variable only when the caller really set it.
  local env_file="$HC_ENV_FILE"
  [ "$_hc_env_explicit" = 1 ] || unset HC_ENV_FILE
  # shellcheck source=scripts/mcp-probe.sh
  . "$ROOT/scripts/mcp-probe.sh" || { HC_ENV_FILE="$env_file"; return 1; }
  HC_ENV_FILE="$env_file"
  eval "$saved"
  [ "$had_u" = 1 ] || set +u
  _hc_mcp_probe_loaded=1
}

# _hc_gw_curl <curl-args...> — host curl against the gateway with the demo Bearer.
_hc_gw_curl() {
  hc_curl_auth "$HC_MCP_TOKEN" -sS --max-time 30 "$@"
}

# mcp_unwrap  (stdin -> stdout) — strip SSE framing when present, else pass through.
# The gateway relays upstream's `text/event-stream` framing verbatim, so every MCP reply
# has to be de-framed before it can be grepped/jq'd. The LAST data: line is the answer.
mcp_unwrap() {
  local raw; raw="$(cat)"
  case "$raw" in
    *"data: "*) printf '%s' "$raw" | tr -d '\r' | sed -n 's/^data: //p' | tail -1 ;;
    *)          printf '%s' "$raw" ;;
  esac
}

# mcp_call TOKEN BANK JSONRPC_BODY -> prints "<SSE-unwrapped response>\n<http status>".
# The status TRAILS the body instead of landing in a variable for the same reason
# mcp-probe.sh's rest_probe does it: callers use $(...), which is a subshell, so a global
# assignment would evaporate. Split with `${r%$'\n'*}` / `${r##*$'\n'}`.
# Handshakes once per TOKEN|BANK pair; the session id lives in a global, so a `$(mcp_call
# ...)` caller re-handshakes on each call (one extra initialize, no correctness cost) while
# calls made in the gate's own shell reuse it. Returns non-zero when the handshake fails,
# so a gate can tell "no session" from "a bad answer".
mcp_call() {
  local token="$1" bank="$2" body="$3"
  _hc_load_mcp_probe || return 1
  HC_MCP_TOKEN="$token"
  HC_MCP_BASE="$HC_GW"
  HC_MCP_PATH="/mcp/$bank/"
  HC_MCP_BANK_HEADER=""          # bank travels in the path, not a header
  HC_MCP_CURL=_hc_gw_curl
  if [ "${HC_MCP_SESSION_KEY:-}" != "$token|$bank" ]; then
    HC_MCP_SESSION=""
    mcp_handshake || { hc_log "MCP handshake failed (bank=$bank via $HC_GW)"; return 1; }
    HC_MCP_SESSION_KEY="$token|$bank"
  fi
  local raw; raw="$(mcp_post "$body" -w '\n%{http_code}')" || return 1
  printf '%s' "${raw%$'\n'*}" | mcp_unwrap
  printf '\n%s' "${raw##*$'\n'}"
}
