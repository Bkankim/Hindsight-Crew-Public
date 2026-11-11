#!/usr/bin/env bash
# next-stack.sh - run the CURRENT worktree's gateway against a v1.x hindsight image on a copy of the production
# data ("hc-next"), next to the untouched production stack, and remove every trace of it again.
#
#   next-stack.sh up [--llm none|opus] [--break-primary] [--backup <dir>]
#   next-stack.sh env | status | rebuild | down
#
# Run it from a worker worktree: the gateway image is built from this worktree's code (docker-compose.next.yml is
# an override over docker-compose.yml). The production checkout (HC_PROD_ROOT, default: the parent of
# `git rev-parse --git-common-dir`) is only READ: .env.local, secrets/, vendor/claude-cli/claude, policies/,
# backups/. Everything the copy writes lives in docker resources named hc-next* and in the state directory
# (HC_NEXT_STATE_DIR, default ~/.local/state/hc-next, 0700: the copy env file, the policy copy, the audit logs).
#
# Contract of #242 (target selection): every docker/compose call carries `-p hc-next` and the two compose files by
# absolute path; every resource this script creates or deletes starts with `hc-next` (anything else ends the run
# with exit 1 first); one stderr line `target: gw=... project=... container=... pg0=...` per run.
#
# Restore source: `up` restores <dir>/pg0.tar.gz of the directory named by `--backup <dir>` or HC_NEXT_BACKUP_DIR (any
# name; missing dir or archive = exit 1, never a fallback), else the newest YYYYMMDD-HHMMSS directory of the production
# backups/. HC_NEXT_BACKUP_SHA256 (64 hex) is compared with the archive before anything is created. stdout carries
# exactly one line `restore: backup=<absolute>/pg0.tar.gz sha256=<hex>`.
#
# Standard call for the other tools (a failing `env` prints nothing and exits 1, so the chain stops):
#   next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"; bash verify/gate1_health.sh
#
# exit codes: 0 ok; 1 refused / failed; 3 (status only) no hc-next resource at all.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/hc-python.sh"

NEXT_PROJECT=hc-next
NEXT_GW_BIND=127.0.0.1:18888
NEXT_UI_BIND=127.0.0.1:9998
NEXT_PG0=hc-next_pg0
NEXT_HINDSIGHT=hc-next-hindsight-1
NEXT_GATEWAY=hc-next-gateway-1
NEXT_TUNNEL=hc-next-ui-tunnel-1
# v1.1.0, pinned by digest (2026-10-29 ghcr lookup: tag 1.1.0 = image index sha256:50311d02...). The v1.0.0 digest
# production runs until the upgrade window is sha256:a88678c0...; select it (or a 1.1.x) by HC_NEXT_IMAGE.
NEXT_DEFAULT_IMAGE='ghcr.io/vectorize-io/hindsight@sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06'
HINDSIGHT_BUNDLED=/app/api/.venv/lib/python3.11/site-packages/claude_agent_sdk/_bundled
INVALID_PRIMARY='hc-next-invalid-primary'

die() { echo "next-stack: $*" >&2; exit 1; }
info() { echo "next-stack: $*" >&2; }
target_line() { echo "target: gw=$NEXT_GW_BIND project=$NEXT_PROJECT container=$NEXT_HINDSIGHT pg0=$NEXT_PG0" >&2; }

# ---------------------------------------------------------------- paths

# norm_path <path> - absolute, lexical (. .. // collapsed, no symlink lookup).
norm_path() {
  local p="$1" seg out="" oldifs="$IFS"
  case "$p" in /*) : ;; *) p="$PWD/$p" ;; esac
  IFS=/; set -f
  for seg in $p; do
    case "$seg" in ""|.) : ;; ..) out="${out%/*}" ;; *) out="$out/$seg" ;; esac
  done
  set +f; IFS="$oldifs"
  printf '%s' "${out:-/}"
}

# phys_path <path> - norm_path with the longest existing prefix resolved through symlinks.
phys_path() {
  local p d rest="" real
  p="$(norm_path "$1")"; d="$p"
  while [ ! -e "$d" ] && [ "$d" != / ]; do rest="/${d##*/}$rest"; d="${d%/*}"; [ -n "$d" ] || d=/; done
  real="$(cd -P "$d" 2>/dev/null && pwd -P)" || real="$d"
  printf '%s%s' "${real%/}" "$rest"
}

# inside_or_same <path> <base> - success when <path> is <base> or below it.
inside_or_same() { case "$1/" in "$2"/*) return 0 ;; esac; return 1; }

resolve_paths() {
  case "$ROOT" in *:*) die "the worktree path contains ':' (HC_COMPOSE_FILES is colon separated): $ROOT" ;; esac
  local prod
  if [ -n "${HC_PROD_ROOT:-}" ]; then prod="$HC_PROD_ROOT"
  else
    prod="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || die "cannot find the production checkout (set HC_PROD_ROOT)"
    prod="${prod%/*}"
  fi
  [ -d "$prod" ] || die "HC_PROD_ROOT is not a directory: $prod"
  PROD_ROOT="$(phys_path "$prod")"
  case "$PROD_ROOT" in *:*) die "HC_PROD_ROOT contains ':' : $PROD_ROOT" ;; esac
  [ -n "${HOME:-}" ] || [ -n "${HC_NEXT_STATE_DIR:-}" ] || die "HOME is unset (set HC_NEXT_STATE_DIR)"
  local raw="${HC_NEXT_STATE_DIR:-$HOME/.local/state/hc-next}" lex phys
  lex="$(norm_path "$raw")"; phys="$(phys_path "$raw")"
  # The state directory is deleted by `down`: it must be a directory of its own named hc-next, outside the
  # production checkout and this worktree, and must not contain them either.
  [ "${lex##*/}" = hc-next ] && [ "${phys##*/}" = hc-next ] || die "HC_NEXT_STATE_DIR must be an absolute path whose last part is hc-next (got $raw)"
  local base p
  for base in "$PROD_ROOT" "$(phys_path "$ROOT")" "$(norm_path "$ROOT")"; do
    for p in "$lex" "$phys"; do
      if inside_or_same "$p" "$base" || inside_or_same "$base" "$p"; then
        die "HC_NEXT_STATE_DIR ($p) overlaps the production checkout or this worktree ($base)"
      fi
    done
  done
  [ ! -L "$lex" ] || die "HC_NEXT_STATE_DIR is a symlink: $lex"
  STATE="$phys"
  case "$STATE" in *:*|*[\'\"\\]*|*" "*) die "HC_NEXT_STATE_DIR must not contain quotes, backslashes, colons or spaces: $STATE" ;; esac
  SECRETS_DIR="$(phys_path "${HC_SECRETS_DIR:-$PROD_ROOT/secrets}")"
  COMPOSE_FILES="$ROOT/docker-compose.yml:$ROOT/docker-compose.next.yml"
  ENV_FILE="$STATE/env"
}

# load_from_env_file - env/status/rebuild take the paths the copy was started with from its env file, not from the
# caller's shell (a different HC_SECRETS_DIR there must not change which secrets the next rebuild mounts).
load_from_env_file() {
  local v
  [ -f "$ENV_FILE" ] || return 0
  v="$(unquote "$(sed -n 's/^HC_SECRETS_DIR=//p' "$ENV_FILE" | tail -1)")"; [ -z "$v" ] || SECRETS_DIR="$v"
}

# sha256_of <file> - the hex sha256 of a file (sha256sum, shasum or python, whichever exists).
sha256_of() {
  local h
  # stdin, not a file name: GNU sha256sum prefixes the line with a backslash when the name holds a backslash or newline
  if command -v sha256sum >/dev/null 2>&1; then h="$(sha256sum < "$1")" || return 1
  elif command -v shasum >/dev/null 2>&1; then h="$(shasum -a 256 < "$1")" || return 1
  else
    hc_find_python || return 1
    h="$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$1")" || return 1
  fi
  printf '%s' "${h%% *}"
}

# select_backup <dir from --backup> - sets BACKUP_DIR and BACKUP_SHA256 (pre-check: nothing is created yet).
select_backup() {
  local flag="$1" env="${HC_NEXT_BACKUP_DIR:-}" want="${HC_NEXT_BACKUP_SHA256:-}" named latest
  if [ -n "$want" ]; then
    want="$(printf '%s' "$want" | tr 'A-F' 'a-f')"
    [[ "$want" =~ ^[0-9a-f]{64}$ ]] || die "HC_NEXT_BACKUP_SHA256 must be 64 hex characters"
  fi
  if [ -n "$flag" ] && [ -n "$env" ] && [ "$(norm_path "$flag")" != "$(norm_path "$env")" ]; then
    die "--backup ($flag) and HC_NEXT_BACKUP_DIR ($env) name different backups"
  fi
  named="${flag:-$env}"
  if [ -n "$named" ]; then
    [ -n "$flag" ] || case "$env" in /*) : ;; *) die "HC_NEXT_BACKUP_DIR must be an absolute path (got $env)" ;; esac
    BACKUP_DIR="$(phys_path "$named")"
    [ -d "$BACKUP_DIR" ] || die "the named backup directory does not exist: $BACKUP_DIR (no fallback to the latest backup)"
    [ -f "$BACKUP_DIR/pg0.tar.gz" ] || die "the named backup has no pg0.tar.gz: $BACKUP_DIR (no fallback to the latest backup)"
  else
    latest="$(ls -1 "$PROD_ROOT/backups" 2>/dev/null | grep -E '^[0-9]{8}-[0-9]{6}$' | sort | tail -1)"
    [ -n "$latest" ] || die "no backup directory YYYYMMDD-HHMMSS in $PROD_ROOT/backups"
    BACKUP_DIR="$PROD_ROOT/backups/$latest"
    [ -f "$BACKUP_DIR/pg0.tar.gz" ] || die "the latest backup has no pg0.tar.gz: $BACKUP_DIR"
  fi
  case "$BACKUP_DIR" in *:*) die "the backup path contains ':' (docker -v): $BACKUP_DIR" ;; esac
  BACKUP_SHA256="$(sha256_of "$BACKUP_DIR/pg0.tar.gz")" || die "cannot compute the sha256 of $BACKUP_DIR/pg0.tar.gz"
  [[ "$BACKUP_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "unexpected sha256 output for $BACKUP_DIR/pg0.tar.gz"
  if [ -n "$want" ] && [ "$want" != "$BACKUP_SHA256" ]; then
    die "sha256 mismatch for $BACKUP_DIR/pg0.tar.gz: expected $want (HC_NEXT_BACKUP_SHA256), got $BACKUP_SHA256 - nothing restored"
  fi
}

# ---------------------------------------------------------------- docker helpers

# All names this script creates or deletes pass through here.
assert_next_name() {
  case "$1" in
    hc-next|hc-next[-_.:@/]*) return 0 ;;
    *) die "refusing to touch '$1': not a hc-next resource" ;;
  esac
}

# The compose environment: the copy env file is the only source of values, so a variable the caller has in its shell
# (a production HC_HINDSIGHT_IMAGE, COMPOSE_PROJECT_NAME ...) must not beat it - shell values win over --env-file.
prep_compose_env() {
  local k
  if [ -f "$ENV_FILE" ]; then
    for k in $(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$ENV_FILE"); do unset "$k"; done
  fi
  for k in COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PATH_SEPARATOR COMPOSE_ENV_FILES COMPOSE_PROFILES HC_COMPOSE_PROJECT HC_COMPOSE_FILES; do unset "$k"; done
  export HC_ENV_FILE="$ENV_FILE" HC_PROD_ROOT="$PROD_ROOT" HC_SECRETS_DIR="$SECRETS_DIR" HC_NEXT_STATE_DIR="$STATE"
  [ -z "${COMMIT:-}" ] || export HC_NEXT_COMMIT="$COMMIT"
}

# dc <compose args...> - the contract's compose shape for the copy. Never without -p hc-next.
dc() {
  docker compose --env-file "$ENV_FILE" --project-directory "$ROOT" -p "$NEXT_PROJECT" \
    -f "$ROOT/docker-compose.yml" -f "$ROOT/docker-compose.next.yml" "$@"
}

need_tools() {
  command -v docker >/dev/null 2>&1 || die "docker not found"
  docker info >/dev/null 2>&1 || die "docker is not reachable"
}

# ---- hc-next resource listing (client-side prefix filters: docker's name filter is a substring match)

list_containers() {
  local out
  out="$(docker ps -a --format '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Label "hc-next"}}')" || die "docker ps failed"
  printf '%s\n' "$out" | awk -F'|' 'NF && ($1 ~ /^hc-next/ || $2 == "hc-next" || $3 == "1") {print $1}'
}
list_volumes() {
  local out
  out="$(docker volume ls --format '{{.Name}}|{{.Label "com.docker.compose.project"}}|{{.Label "hc-next"}}')" || die "docker volume ls failed"
  printf '%s\n' "$out" | awk -F'|' 'NF && ($1 ~ /^hc-next/ || $2 == "hc-next" || $3 == "1") {print $1}'
}
list_networks() {
  local out
  out="$(docker network ls --format '{{.Name}}|{{.Label "com.docker.compose.project"}}|{{.Label "hc-next"}}')" || die "docker network ls failed"
  printf '%s\n' "$out" | awk -F'|' 'NF && ($1 ~ /^hc-next/ || $2 == "hc-next" || $3 == "1") {print $1}'
}
# list_images - image IDs of images tagged hc-next*, labelled with the compose project hc-next or stamped with
# hc.next.commit. Measured: compose does NOT label the images it builds with the project (only
# com.docker.compose.image.builder and the build labels of docker-compose.next.yml), so the label filter that
# catches the untagged previous build left by a rebuild is hc.next.commit; the project filter is kept as well.
# A docker failure ends the run: a listing that failed must never read as "nothing there".
list_images() {
  local tagged byproject bycommit
  tagged="$(docker images --format '{{.ID}}|{{.Repository}}')" || die "docker images failed"
  byproject="$(docker images -q --filter label=com.docker.compose.project=hc-next)" || die "docker images (label) failed"
  bycommit="$(docker images -q --filter label=hc.next.commit)" || die "docker images (label) failed"
  { printf '%s\n' "$tagged" | awk -F'|' 'NF && $2 ~ /^hc-next/ {print $1}'; printf '%s\n%s\n' "$byproject" "$bycommit"; } \
    | awk 'NF && !seen[$0]++'
}

# any_resources - success when anything of hc-next exists. Each listing is captured with its status, because a
# failure inside $(...) of a test would otherwise read as an empty list.
any_resources() {
  local c v n
  [ -e "$STATE" ] && return 0
  c="$(list_containers)" || exit 1
  v="$(list_volumes)" || exit 1
  n="$(list_networks)" || exit 1
  [ -n "$c$v$n" ]
}

# ---------------------------------------------------------------- env file assembly

# prod_raw KEY - the raw text after "=" of the last KEY= line of the production .env.local (empty if none)
prod_raw() { sed -n "s/^$1=//p" "$PROD_ROOT/.env.local" | tail -1; }
prod_has() { grep -q "^$1=" "$PROD_ROOT/.env.local"; }
unquote() { local v="$1"; v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"; printf '%s' "$v"; }

# q <value> - sets Q to 'value' (single quoted: the shell's `. file` and compose env_file read it the same way, and a
# JSON value like {"mode":"failover"} survives; unquoted it would lose its quotes in the shell). It sets a variable
# instead of printing, so a refusal (die) ends the calling shell and not just a $(...) subshell that would leave an
# empty value in the file.
q() { case "$1" in *\'*) die "a value to write into the copy env file contains a single quote (key withheld)" ;; esac; Q="'$1'"; }

excluded_key() {
  local k="$1" u
  [[ "$k" =~ ^HINDSIGHT_API_([A-Z0-9]+_)*LLM_ ]] && return 0
  u="$(printf '%s' "$k" | tr 'a-z' 'A-Z')"
  case "$u" in *XAI*) return 0 ;; esac
  case "$k" in
    HC_NOTIFY_DISCORD_*) return 0 ;;
    # production target selection, replaced by the copy's own values below
    HC_GATEWAY_BIND|HC_UI_BIND|HC_PG0_VOLUME|HC_HINDSIGHT_IMAGE|COMPOSE_PROJECT_NAME) return 0 ;;
    # policy paths: the compose environment pins the first four, but the *_LOCAL ones would be read from here
    HC_BANK_POLICIES|HC_CONTEXT_TRIGGERS|HC_ACL_FILE|HC_AUDIT_LOG|HC_BANK_POLICIES_LOCAL|HC_CONTEXT_TRIGGERS_LOCAL) return 0 ;;
    # contract variables: the copy writes its own (HC_TARGET is deliberately NEVER in a file: `next-stack.sh env` only)
    HC_TARGET|HC_ENV_FILE|HC_COMPOSE_PROJECT|HC_COMPOSE_FILES|HC_SECRETS_DIR|HC_PROD_ROOT|HC_HINDSIGHT_CONTAINER|HC_NEXT_*) return 0 ;;
  esac
  [ "$k" = HINDSIGHT_API_RERANKER_TEI_URL ] && [ -n "${HC_NEXT_TEI_URL:-}" ] && return 0
  return 1
}

# write_env LLM BREAK_PRIMARY - assemble $ENV_FILE from the production .env.local. Key NAMES only are ever printed.
write_env() {
  local llm="$1" brk="$2" line key tmp="$ENV_FILE.tmp.$$"
  ( umask 077
    {
      while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
          export\ *) die "$PROD_ROOT/.env.local has an 'export' line; the copy env builder reads plain KEY=value lines" ;;
          [A-Za-z_]*=*) key="${line%%=*}" ;;
          *) continue ;;
        esac
        case "$key" in *[!A-Za-z0-9_]*) continue ;; esac
        excluded_key "$key" && continue
        printf '%s\n' "$line"
      done < "$PROD_ROOT/.env.local"
      echo "# hc-next copy env, assembled by scripts/next-stack.sh from the production .env.local (LLM credentials, xai,"
      echo "# Discord and production bind/policy keys removed). Not a production file; down deletes it."
      echo "HC_NEXT_ENV=1"
      q "$IMAGE"; echo "HC_HINDSIGHT_IMAGE=$Q"
      echo "HC_PG0_VOLUME=$NEXT_PG0"
      echo "HC_GATEWAY_BIND=$NEXT_GW_BIND"
      echo "HC_UI_BIND=$NEXT_UI_BIND"
      q "$PROD_ROOT"; echo "HC_PROD_ROOT=$Q"
      q "$SECRETS_DIR"; echo "HC_SECRETS_DIR=$Q"
      q "$STATE"; echo "HC_NEXT_STATE_DIR=$Q"
      echo "HC_NEXT_COMMIT=$COMMIT"
      if [ -n "${HC_NEXT_TEI_URL:-}" ]; then q "$HC_NEXT_TEI_URL"; echo "HINDSIGHT_API_RERANKER_TEI_URL=$Q"; fi
      if [ "$llm" = none ]; then
        echo "HINDSIGHT_API_LLM_PROVIDER=none"
      else
        echo "HINDSIGHT_API_LLM_PROVIDER=claude-code"
        echo "HINDSIGHT_API_LLM_MODEL=claude-sonnet-5-5"
        # 2026-11-01: 운영과 같이 sonnet-5-5 effort medium (opus 재통합 arm 이 account1 주간 한도를 소진).
        # claude-code 공급자는 REASONING_EFFORT 를 무시하므로 effort 는 CLI env 로 넘긴다. 플래그 이름 --llm opus 는 호환을 위해 유지.
        echo "CLAUDE_CODE_EFFORT_LEVEL=medium"
        if [ "$brk" = 1 ]; then q "$INVALID_PRIMARY"; echo "HINDSIGHT_API_LLM_API_KEY=$Q"
        else echo "HINDSIGHT_API_LLM_API_KEY=$ACCOUNT1_RAW"; fi
        echo "HINDSIGHT_API_LLM_STRATEGY='{\"mode\":\"failover\"}'"
        echo "HINDSIGHT_API_LLM_1_PROVIDER=claude-code"
        echo "HINDSIGHT_API_LLM_1_MODEL=claude-sonnet-5-5"
        echo "HINDSIGHT_API_LLM_1_API_KEY=$ACCOUNT2_RAW"
        # The automatic mental-model refresh builds its own LLM (reflect_llm_timeout 30 != llm_timeout 120) and takes
        # chain members from the REFLECT_ slot only (v1.0.0 memory_engine.py 2885-2956): without these four keys it runs
        # on the first member with no fallback. Same list as scripts/llm-migrate-opus.py (test_next_stack asserts it).
        echo "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_STRATEGY='{\"mode\":\"failover\"}'"
        echo "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_PROVIDER=claude-code"
        echo "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_MODEL=claude-sonnet-5-5"
        echo "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_API_KEY=$ACCOUNT2_RAW"
      fi
    } > "$tmp"
  ) || { rm -f "$tmp"; exit 1; }
  chmod 600 "$tmp" && mv "$tmp" "$ENV_FILE"
}

# set_commit <sha> - update HC_NEXT_COMMIT in the copy env file (up/rebuild), so `--env-file` supplies it.
set_commit() {
  local tmp="$ENV_FILE.tmp.$$"
  ( umask 077; { grep -v '^HC_NEXT_COMMIT=' "$ENV_FILE"; echo "HC_NEXT_COMMIT=$1"; } > "$tmp" ) || { rm -f "$tmp"; die "cannot update $ENV_FILE"; }
  chmod 600 "$tmp" && mv "$tmp" "$ENV_FILE"
}

# ---------------------------------------------------------------- policies

required_bank() {
  local b="${HC_NEXT_REQUIRED_BANK:-}"
  if [ -z "$b" ] && [ -f "$PROD_ROOT/.env.host.local" ]; then
    b="$(sed -n 's/^HC_PERSONAL_BANK=//p' "$PROD_ROOT/.env.host.local" | tail -1)"; b="$(unquote "$b")"
  fi
  printf '%s' "$b"
}

copy_policies() {
  local pol="$PROD_ROOT/policies" f n
  mkdir -p "$STATE/policies/local"
  if [ -d "$pol/local" ]; then
    # Same rule as bench/replica.py inject_local_policies, plus the tracked pair: the two tracked files and the
    # local files (those that exist) are copied as they are.
    for n in bank-policies.json context-triggers.json; do
      [ -f "$pol/$n" ] || die "missing tracked policy file $pol/$n"
      cp "$pol/$n" "$STATE/policies/$n"
      [ ! -f "$pol/local/$n" ] || cp "$pol/local/$n" "$STATE/policies/local/$n"
    done
  else
    # Not split yet (one file per kind): split into tracked + local in the state directory, then prove the merge
    # is the same as the original through the gateway loader (needs httpx: the uv environment).
    hc_find_python || die "python3 not found (set HC_PYTHON)"
    "$PY" "$ROOT/scripts/policy-split.py" split --src "$pol" --tracked-dir "$STATE/policies" --local-dir "$STATE/policies/local" >/dev/null \
      || die "policy-split split failed"
    uv run --python 3.12 --with-requirements "$ROOT/gateway/requirements.txt" python "$ROOT/scripts/policy-split.py" check \
      --src "$pol" --policies-dir "$STATE/policies" >/dev/null || die "policy-split check failed: the split differs from the original policies"
  fi
  # The merged result (the gateway loader, same uv environment) must carry a policy for the bank the stats are read for.
  uv run --python 3.12 --with-requirements "$ROOT/gateway/requirements.txt" python -c '
import sys
sys.path.insert(0, sys.argv[1])
from gateway.bank_policies import BankPolicies
p = BankPolicies.from_files(sys.argv[2], sys.argv[3])
sys.exit(0 if sys.argv[4] in p.bank_names() else 1)' \
    "$ROOT" "$STATE/policies/bank-policies.json" "$STATE/policies/local/bank-policies.json" "$REQUIRED_BANK" \
    || die "the merged policies have no entry for the required bank (HC_NEXT_REQUIRED_BANK / HC_PERSONAL_BANK)"
}

# ---------------------------------------------------------------- render check

render_check() {
  hc_find_python || die "python3 not found (set HC_PYTHON)"
  local rc=0 json
  # The render holds key values (env files are folded into `environment`): keep it in a shell variable, parse it in
  # memory only, and report key names and mount paths, never values.
  json="$(dc config --format json 2>/dev/null)" || die "render check: docker compose config failed"
  printf '%s' "$json" | "$PY" -c '
import json, sys
exp_image, repo, prod, state, secrets = sys.argv[1:6]
d = json.load(sys.stdin)
bad = []
vols = d.get("volumes") or {}
svc = d.get("services") or {}
def vname(k):
    return (vols.get(k) or {}).get("name") or k
def under(p, base):
    return p == base or p.startswith(base.rstrip("/") + "/")
if vname("pg0") != "hc-next_pg0":
    bad.append("volumes.pg0.name is %s, not hc-next_pg0" % vname("pg0"))
if (svc.get("hindsight") or {}).get("image") != exp_image:
    bad.append("hindsight image is %s, not the expected %s" % ((svc.get("hindsight") or {}).get("image"), exp_image))
allowed_vols = {"hc-next_pg0", "hindsight-crew_hfcache"}
allowed_bind = [prod + "/vendor/claude-cli/claude", secrets, state + "/policies", state + "/audit"]
seen_pg0 = seen_hf = False
for sname, s in svc.items():
    for m in s.get("volumes") or []:
        src, tgt, typ = m.get("source") or "", m.get("target"), m.get("type")
        res = vname(src) if typ == "volume" else src
        where = "%s mount %s" % (sname, tgt)
        if res in ("hindsight-crew_pg0", "hindsight-crew_hsauth") or src == "hsauth":
            bad.append("%s uses the production volume %s" % (where, res))
        if "/overlays/" in res + "/":
            bad.append("%s mounts an overlay (%s)" % (where, res))
        for forbidden in (repo + "/audit", prod + "/audit"):
            if under(res, forbidden):
                bad.append("%s mounts the production audit directory (%s)" % (where, res))
        if typ == "volume":
            if res not in allowed_vols:
                bad.append("%s mounts the unexpected volume %s" % (where, res))
            if res == "hc-next_pg0" and tgt == "/home/hindsight/.pg0":
                seen_pg0 = True
            if res == "hindsight-crew_hfcache":
                seen_hf = True
                if not m.get("read_only"):
                    bad.append("%s: the hfcache mount is not read only" % where)
        elif not any(under(res, b) for b in allowed_bind):
            bad.append("%s mounts the unexpected path %s" % (where, res))
if not seen_pg0:
    bad.append("hindsight does not mount hc-next_pg0 at /home/hindsight/.pg0")
if not seen_hf:
    bad.append("hindsight does not mount the hfcache volume")
for sname in ("hindsight", "gateway"):
    env = (svc.get(sname) or {}).get("environment") or {}
    if env.get("HC_NEXT_ENV") != "1":
        bad.append("%s has no HC_NEXT_ENV=1 marker (the copy env file is not the one in use)" % sname)
    for k in sorted(env):
        if k.startswith("HC_NOTIFY_DISCORD_") or k.startswith("HINDSIGHT_API_LLM_2_") or "XAI" in k.upper() \
                or k in ("HC_BANK_POLICIES_LOCAL", "HC_CONTEXT_TRIGGERS_LOCAL"):
            bad.append("%s environment carries the key %s" % (sname, k))
for line in bad:
    sys.stderr.write("next-stack: render check: " + line + "\n")
sys.exit(1 if bad else 0)' "$IMAGE" "$ROOT" "$PROD_ROOT" "$STATE" "$SECRETS_DIR" || rc=1
  json=""
  # Secondary: before interpolation the env_file entries are visible as written; neither production file may be named.
  json="$(dc config --no-interpolate --format json 2>/dev/null)" || die "render check: docker compose config --no-interpolate failed"
  printf '%s' "$json" | "$PY" -c '
import json, sys
svc = json.load(sys.stdin).get("services") or {}
bad = 0
for sname in ("hindsight", "gateway"):
    text = json.dumps((svc.get(sname) or {}).get("env_file"))
    for name in (".env.local", ".env.gateway.local"):
        if name in text:
            sys.stderr.write("next-stack: render check: %s env_file names %s\n" % (sname, name)); bad = 1
sys.exit(bad)' || rc=1
  json=""
  [ "$rc" = 0 ] || die "render check failed - nothing was started"
}

# ---------------------------------------------------------------- waiting

wait_gateway_ready() {
  local timeout="${HC_NEXT_UP_TIMEOUT:-900}" deadline now health body
  case "$timeout" in ""|*[!0-9]*) die "HC_NEXT_UP_TIMEOUT must be a number of seconds" ;; esac
  deadline=$(( $(date +%s) + timeout ))
  while :; do
    health="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$NEXT_HINDSIGHT" 2>/dev/null)"
    case "$health" in
      "exited "*|"dead "*) die "$NEXT_HINDSIGHT is $health (docker logs $NEXT_HINDSIGHT)" ;;
      "running healthy")
        body="$(curl -fsS --max-time 5 "http://$NEXT_GW_BIND/healthz" 2>/dev/null)" || body=""
        if printf '%s' "$body" | grep -Eq '"upstream_ok" *: *true'; then return 0; fi ;;
    esac
    now=$(date +%s)
    [ "$now" -lt "$deadline" ] || die "not ready after ${timeout}s (hindsight: ${health:-unknown}); docker logs $NEXT_HINDSIGHT / $NEXT_GATEWAY"
    sleep 5
  done
}

# ---------------------------------------------------------------- commands

cmd_up() {
  local llm=none brk=0 backup=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --llm) [ $# -ge 2 ] || die "--llm needs none|opus"; llm="$2"; shift 2 ;;
      --break-primary) brk=1; shift ;;
      --backup) [ $# -ge 2 ] && [ -n "$2" ] || die "--backup needs a directory"; backup="$2"; shift 2 ;;
      *) die "up: unknown argument $1" ;;
    esac
  done
  case "$llm" in none|opus) : ;; *) die "--llm must be none or opus" ;; esac
  [ "$brk" = 0 ] || [ "$llm" = opus ] || die "--break-primary needs --llm opus"
  resolve_paths
  target_line
  need_tools
  command -v git >/dev/null 2>&1 || die "git not found"
  command -v uv >/dev/null 2>&1 || die "uv not found (the policy loader check needs it)"
  command -v curl >/dev/null 2>&1 || die "curl not found"

  # ---- 1. pre-checks: nothing is created, so a failure leaves nothing behind
  local f
  for f in .env.local secrets/acl.json secrets/demo-tokens.env vendor/claude-cli/claude; do
    [ -e "$PROD_ROOT/$f" ] || die "production file missing: $PROD_ROOT/$f"
  done
  [ -f "$SECRETS_DIR/acl.json" ] && [ -f "$SECRETS_DIR/demo-tokens.env" ] || die "HC_SECRETS_DIR needs acl.json and demo-tokens.env: $SECRETS_DIR"
  REQUIRED_BANK="$(required_bank)"
  [ -n "$REQUIRED_BANK" ] || die "no required bank: set HC_NEXT_REQUIRED_BANK (or HC_PERSONAL_BANK in $PROD_ROOT/.env.host.local)"
  IMAGE="${HC_NEXT_IMAGE:-$NEXT_DEFAULT_IMAGE}"
  select_backup "$backup"
  if [ "$llm" = opus ]; then
    # The production file is either the pre-migration shape (LLM_1 = account1, LLM_2 = account2) or the target shape
    # (global key = account1, LLM_1 = account2, after scripts/llm-migrate-opus.py). The judgment is that script's own
    # (--classify), so the two cannot drift apart; anything else is refused. Key names only come back.
    local layout rc
    hc_find_python || die "python3 not found (set HC_PYTHON)"
    layout="$("$PY" "$ROOT/scripts/llm-migrate-opus.py" --env-file "$PROD_ROOT/.env.local" --classify 2>&1)"; rc=$?
    [ "$rc" = 0 ] || die "--llm opus needs the production .env.local in the pre-migration layout (HINDSIGHT_API_LLM_1/2 = claude-code members) or in the target layout (claude-code/claude-opus-5-5, LLM_1 = account2): $layout"
    case "$layout" in
      "layout: old")    ACCOUNT1_RAW="$(prod_raw HINDSIGHT_API_LLM_1_API_KEY)"; ACCOUNT2_RAW="$(prod_raw HINDSIGHT_API_LLM_2_API_KEY)" ;;
      "layout: target") ACCOUNT1_RAW="$(prod_raw HINDSIGHT_API_LLM_API_KEY)";   ACCOUNT2_RAW="$(prod_raw HINDSIGHT_API_LLM_1_API_KEY)" ;;
      *) die "--llm opus: unexpected answer from llm-migrate-opus.py --classify" ;;
    esac
    [ -n "$ACCOUNT1_RAW" ] && [ -n "$ACCOUNT2_RAW" ] || die "--llm opus: a member token is empty or missing in the production .env.local"
    info "production LLM layout: ${layout#layout: }"
  fi
  local min="${HC_NEXT_MIN_AVAIL_MB:-3500}" avail
  case "$min" in ""|*[!0-9]*) die "HC_NEXT_MIN_AVAIL_MB must be a number" ;; esac
  avail="$(docker run --rm --network none --name "hc-next-precheck-$$" --label hc-next=1 alpine \
    awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo 2>/dev/null | tr -d '[:space:]')"
  case "$avail" in ""|*[!0-9]*) die "cannot read the VM's MemAvailable (got '${avail}'): refusing to start a second stack" ;; esac
  [ "$avail" -ge "$min" ] || die "VM MemAvailable ${avail}MB < ${min}MB (HC_NEXT_MIN_AVAIL_MB): not enough room next to production"
  if any_resources; then
    die "hc-next resources or the state directory already exist: run 'bash scripts/next-stack.sh down' first"
  fi

  # ---- 2-4. state directory, copy env, policy copy
  COMMIT="$(git -C "$ROOT" rev-parse HEAD)" || die "git rev-parse HEAD failed"
  trap 'rc=$?; [ "$rc" = 0 ] || info "up failed (exit $rc); clean up with: bash scripts/next-stack.sh down"' EXIT
  ( umask 077; mkdir -p "$STATE/policies" "$STATE/audit" ) || die "cannot create $STATE"
  chmod 700 "$STATE"
  write_env "$llm" "$brk"
  info "copy env written: $ENV_FILE (keys only: $(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$ENV_FILE" | grep -c .) keys)"
  copy_policies
  prep_compose_env

  # ---- 7. render check (before anything is started; moved ahead of the restore so a bad render leaves no volume)
  render_check

  # ---- 6. image check: the vendor CLI mount point must exist in the image (Docker would create a directory otherwise)
  info "checking the image (the first pull is ~6GB): $IMAGE"
  docker run --rm --name "hc-next-precheck-image-$$" --label hc-next=1 --entrypoint ls "$IMAGE" "$HINDSIGHT_BUNDLED/" >/dev/null \
    || die "the image has no $HINDSIGHT_BUNDLED (cannot mount the vendor CLI)"

  # ---- 5. restore the selected backup into hc-next_pg0 (extract + pg_verifybackup, as restore-test.sh does)
  local bdir="$BACKUP_DIR"
  assert_next_name "$NEXT_PG0"
  docker volume create --label hc-next=1 --label com.docker.compose.project=hc-next --label com.docker.compose.volume=pg0 "$NEXT_PG0" >/dev/null \
    || die "cannot create volume $NEXT_PG0"
  docker run --rm --name hc-next-restore-extract --label hc-next=1 -v "$NEXT_PG0":/data -v "$bdir":/b:ro alpine \
    sh -c 'tar xzf /b/pg0.tar.gz -C /data' || die "restore: extracting $bdir/pg0.tar.gz failed"
  local pgd=/home/hindsight/.pg0/instances/hindsight/data vout
  vout="$(docker run --rm --name hc-next-restore-verify --label hc-next=1 --network none --entrypoint sh \
    -v "$NEXT_PG0":/home/hindsight/.pg0 "$IMAGE" -c "
      [ -f $pgd/backup_manifest ] || { echo 'no backup_manifest'; exit 1; }
      v=\$(cat $pgd/PG_VERSION); b=\$(ls -d /home/hindsight/.pg0/installation/\$v*/bin 2>/dev/null | head -1)
      [ -n \"\$b\" ] || { echo \"no postgres \$v binaries in archive\"; exit 1; }
      \$b/pg_verifybackup -q $pgd" 2>&1)" \
    || die "restore: $bdir is not a verified base backup: $(printf '%s' "$vout" | tail -1)"
  echo "restore: backup=$bdir/pg0.tar.gz sha256=$BACKUP_SHA256"
  echo "pg_verifybackup ok"

  # ---- 8. start (the gateway is always built from this worktree)
  info "starting (build + up)"
  dc up -d --build || die "docker compose up failed"
  wait_gateway_ready
  echo "up ok: commit=$COMMIT gateway=http://$NEXT_GW_BIND ui=http://$NEXT_UI_BIND llm=$llm$([ "$brk" = 1 ] && echo ' break-primary')"
}

cmd_env() {
  resolve_paths
  load_from_env_file
  [ -d "$STATE" ] && [ -f "$ENV_FILE" ] || exit 1
  command -v docker >/dev/null 2>&1 || exit 1
  local running commit
  running="$(docker inspect -f '{{.State.Running}}' "$NEXT_HINDSIGHT" "$NEXT_GATEWAY" 2>/dev/null)" || exit 1
  [ "$running" = "$(printf 'true\ntrue')" ] || exit 1
  commit="$(sed -n 's/^HC_NEXT_COMMIT=//p' "$ENV_FILE" | tail -1)"
  [ -n "$commit" ] || exit 1
  local out="" kv k v
  for kv in HC_TARGET=next "HC_ENV_FILE=$ENV_FILE" "HC_COMPOSE_PROJECT=$NEXT_PROJECT" "HC_COMPOSE_FILES=$COMPOSE_FILES" \
            "HC_GATEWAY_BIND=$NEXT_GW_BIND" "HC_PG0_VOLUME=$NEXT_PG0" "HC_HINDSIGHT_CONTAINER=$NEXT_HINDSIGHT" \
            "HC_SECRETS_DIR=$SECRETS_DIR" "HC_PROD_ROOT=$PROD_ROOT" "HC_NEXT_STATE_DIR=$STATE" "HC_NEXT_COMMIT=$commit"; do
    k="${kv%%=*}"; v="${kv#*=}"
    out="$out$(printf 'export %s=%q' "$k" "$v")"$'\n'
  done
  printf '%s' "$out"
}

cmd_status() {
  resolve_paths
  target_line
  need_tools
  if ! any_resources; then echo "hc-next: no resources"; exit 3; fi
  local n st ok=1 running body up="unknown"
  for n in "$NEXT_HINDSIGHT" "$NEXT_GATEWAY" "$NEXT_TUNNEL"; do
    st="$(docker inspect -f '{{.State.Status}}{{if .State.Health}} ({{.State.Health.Status}}){{end}} started={{.State.StartedAt}}' "$n" 2>/dev/null)" || st="missing"
    echo "$n: $st"
    case "$st" in running*) : ;; *) ok=0 ;; esac
  done
  body="$(curl -fsS --max-time 5 "http://$NEXT_GW_BIND/healthz" 2>/dev/null)" || body=""
  if printf '%s' "$body" | grep -Eq '"upstream_ok" *: *true'; then up=true; else up=false; ok=0; fi
  echo "gateway healthz: upstream_ok=$up"
  [ "$ok" = 1 ] || exit 1
}

cmd_rebuild() {
  resolve_paths
  load_from_env_file
  target_line
  need_tools
  [ -f "$ENV_FILE" ] || die "no copy env file ($ENV_FILE): the copy is not up"
  command -v git >/dev/null 2>&1 || die "git not found"
  command -v curl >/dev/null 2>&1 || die "curl not found"
  IMAGE="$(sed -n "s/^HC_HINDSIGHT_IMAGE=//p" "$ENV_FILE" | tail -1)"; IMAGE="$(unquote "$IMAGE")"
  COMMIT="$(git -C "$ROOT" rev-parse HEAD)" || die "git rev-parse HEAD failed"
  [ "$(docker inspect -f '{{.State.Running}}' "$NEXT_HINDSIGHT" 2>/dev/null)" = true ] || die "$NEXT_HINDSIGHT is not running: bring the copy up first"
  prep_compose_env          # exports HC_NEXT_COMMIT to compose; the env file keeps the OLD commit until the build has succeeded
  render_check
  # hindsight is not touched (--no-deps); --force-recreate because the same code would otherwise leave the container as is.
  dc up -d --build --no-deps --force-recreate gateway || die "docker compose up gateway failed"
  # Only now does the running gateway carry $COMMIT: `env` must never name code that is not deployed.
  set_commit "$COMMIT"
  wait_gateway_ready
  echo "rebuild ok: commit=$COMMIT"
}

cmd_down() {
  resolve_paths
  target_line
  need_tools
  # Collect first, check every name, only then delete: a single foreign name stops the whole run before it removes anything.
  local containers volumes networks images n id tags t
  containers="$(list_containers)" || exit 1
  volumes="$(list_volumes)" || exit 1
  networks="$(list_networks)" || exit 1
  images="$(list_images)" || exit 1
  for n in $containers $volumes $networks; do
    case "$n" in hindsight-crew*) die "refusing to delete '$n': it belongs to the production stack" ;; esac
    assert_next_name "$n"
  done
  for id in $images; do
    tags="$(docker image inspect -f '{{range .RepoTags}}{{.}} {{end}}' "$id" 2>/dev/null)" || tags=""
    for t in $tags; do
      case "$t" in hindsight-crew*) die "refusing to delete image $id: it carries the production tag $t" ;; esac
      assert_next_name "$t"
    done
  done
  for n in $containers; do docker rm -f -v "$n" >/dev/null || die "cannot remove container $n"; echo "removed container $n"; done
  for n in $networks; do docker network rm "$n" >/dev/null || die "cannot remove network $n"; echo "removed network $n"; done
  for n in $volumes; do docker volume rm -f "$n" >/dev/null || die "cannot remove volume $n"; echo "removed volume $n"; done
  for id in $images; do docker rmi -f "$id" >/dev/null || die "cannot remove image $id"; echo "removed image $id"; done
  if [ -e "$STATE" ]; then
    assert_next_name "${STATE##*/}"
    if ! rm -rf "$STATE" 2>/dev/null; then
      # files the gateway container created as root on a bind mount
      docker run --rm --name hc-next-cleanup --label hc-next=1 -v "${STATE%/*}":/p alpine rm -rf /p/hc-next >/dev/null \
        || die "cannot remove $STATE"
    fi
    [ ! -e "$STATE" ] || die "$STATE is still there"
    echo "removed state $STATE"
  fi
  echo "down ok"
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  env) shift; cmd_env "$@" ;;
  status) shift; cmd_status "$@" ;;
  rebuild) shift; cmd_rebuild "$@" ;;
  down) shift; cmd_down "$@" ;;
  *) echo "usage: next-stack.sh up [--llm none|opus] [--break-primary] [--backup <dir>] | env | status | rebuild | down" >&2; exit 1 ;;
esac
