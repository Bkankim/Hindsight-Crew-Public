#!/usr/bin/env bash
# backup.sh — back up the embedded pg0 data volume with a HARD 0-byte guard (the silent
# empty-backup incident is the direct motivation) + retention. Prints the backup file path.
#
# Issue #14: tarring the live data directory produced torn snapshots that crash recovery
# would accept silently (BusyBox tar cannot even warn "file changed as we read it"). The
# data directory now comes from pg_basebackup against the running server - online, no pause
# (measured 1s / 245MB) - and is checked with pg_verifybackup before it is packed. The
# archive keeps the old .pg0 layout (installation/ + instances/hindsight/{instance.json,data})
# because restore-test.sh and bench/replica.py mount it as .pg0 as-is.
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
. "$ROOT/scripts/hc-python.sh"

# Target selection (issue #245, contract of #242): a value the caller put in the environment beats the env
# file, which beats the default. Sourcing the file would overwrite the caller's value, so save and restore.
_kept=""
for v in HC_GATEWAY_BIND HC_PG0_VOLUME HC_HINDSIGHT_CONTAINER HC_COMPOSE_PROJECT HC_COMPOSE_FILES; do
  if [ -n "$(eval "echo \${$v+x}")" ]; then eval "_keep_$v=\"\$$v\""; _kept="$_kept $v"; fi
done
_env_explicit=0; if [ -n "${HC_ENV_FILE:-}" ]; then _env_explicit=1; fi
HC_ENV_FILE="${HC_ENV_FILE:-$ROOT/.env.local}"
case "$HC_ENV_FILE" in /*) ;; *) HC_ENV_FILE="$ROOT/$HC_ENV_FILE" ;; esac
# Only the default .env.local may be absent; a file the caller named and that is missing would silently select
# the default target instead.
if [ "$_env_explicit" = 1 ] && [ ! -f "$HC_ENV_FILE" ]; then
  echo "backup: HC_ENV_FILE $HC_ENV_FILE does not exist" >&2; exit 1
fi
[ -f "$HC_ENV_FILE" ] && { set -a; . "$HC_ENV_FILE"; set +a; }
for v in $_kept; do eval "$v=\"\$_keep_$v\"; export $v"; done

# docker compose ... [-f ...] - the same call shape every target-aware tool uses.
COMPOSE=(docker compose)
if [ -f "$HC_ENV_FILE" ]; then COMPOSE+=(--env-file "$HC_ENV_FILE"); fi
COMPOSE+=(--project-directory "$ROOT")
if [ -n "${HC_COMPOSE_PROJECT:-}" ]; then COMPOSE+=(-p "$HC_COMPOSE_PROJECT"); fi
IFS=: read -r -a _files <<< "${HC_COMPOSE_FILES:-docker-compose.yml}"
for f in ${_files[@]+"${_files[@]}"}; do
  [ -n "$f" ] || continue
  case "$f" in /*) COMPOSE+=(-f "$f") ;; *) COMPOSE+=(-f "$ROOT/$f") ;; esac
done

# The project compose resolves: explicit, else COMPOSE_PROJECT_NAME, else the repo directory name in compose's
# normal form. Only used for the target line and the guard below.
PROJECT="${HC_COMPOSE_PROJECT:-${COMPOSE_PROJECT_NAME:-}}"
[ -n "$PROJECT" ] || PROJECT="$(basename "$ROOT" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-' | sed 's/^[-_]*//')"

# Source volume: HC_PG0_VOLUME (caller or env file), else what the compose file renders for volumes.pg0.
VOL="${HC_PG0_VOLUME:-}"
if [ -z "$VOL" ]; then
  hc_find_python || { echo "backup: no python3 to read the compose config" >&2; exit 1; }
  VOL="$("${COMPOSE[@]}" config --format json \
    | "$PY" -c "import json,sys;print(json.load(sys.stdin)['volumes']['pg0']['name'])" 2>/dev/null)" \
    || { echo "backup: cannot read volumes.pg0.name from docker compose config" >&2; exit 1; }
  [ -n "$VOL" ] || { echo "backup: docker compose config gave an empty volumes.pg0.name" >&2; exit 1; }
fi
# An empty name, or one that starts with "_", means the project name did not interpolate (_pg0).
case "$VOL" in ""|_*) echo "backup: resolved pg0 volume name '$VOL' is empty or has no project prefix" >&2; exit 1 ;; esac

# The running container that mounted exactly this volume as the pg0 directory. Nothing else qualifies, so a
# stale or renamed volume can never be backed up by accident.
VOL_FOUND=0; C=""; CNAME="-"
if docker volume ls --format '{{.Name}}' | grep -Fxq -- "$VOL"; then
  VOL_FOUND=1
  for cand in $(docker ps -q --filter "volume=$VOL"); do
    info="$(docker inspect --format '{{.Name}}|{{range .Mounts}} {{.Name}}:{{.Destination}}{{end}} ' "$cand")" || continue
    case "${info#*|}" in
      *" $VOL:/home/hindsight/.pg0 "*) C="$cand"; CNAME="${info%%|*}"; CNAME="${CNAME#/}"; break ;;
    esac
  done
fi
echo "target: gw=- project=$PROJECT container=$CNAME pg0=$VOL" >&2

# HC_TARGET=next marks the copy stack: everything this run touches must be the copy's. Without the marker,
# anything that looks like the copy is refused, so neither side can be reached by accident.
if [ "${HC_TARGET:-}" = next ]; then
  [ "$PROJECT" = hc-next ] || { echo "backup: HC_TARGET=next but project is $PROJECT, not hc-next" >&2; exit 1; }
  [ "$VOL" = hc-next_pg0 ] || { echo "backup: HC_TARGET=next but pg0 volume is $VOL, not hc-next_pg0" >&2; exit 1; }
else
  case "$PROJECT" in hc-next*) echo "backup: project $PROJECT is a copy stack - set HC_TARGET=next" >&2; exit 1 ;; esac
  case "$VOL" in hc-next*) echo "backup: pg0 volume $VOL is a copy stack volume - set HC_TARGET=next" >&2; exit 1 ;; esac
fi
[ "$VOL_FOUND" = 1 ] || { echo "backup: pg0 volume $VOL not found" >&2; exit 1; }
[ -n "$C" ] || { echo "backup: no running container mounts $VOL at /home/hindsight/.pg0 - a base backup needs the live server" >&2; exit 1; }
if [ "${HC_TARGET:-}" = next ]; then
  case "$CNAME" in hc-next-*) ;; *) echo "backup: HC_TARGET=next but container is $CNAME, not hc-next-*" >&2; exit 1 ;; esac
else
  case "$CNAME" in hc-next-*) echo "backup: container $CNAME is a copy stack container - set HC_TARGET=next" >&2; exit 1 ;; esac
fi

TS="$(date +%Y%m%d-%H%M%S)"; DEST="backups/$TS"; mkdir -p "$DEST"
take(){
# Runs inside the hindsight container, which has the postgres binaries (in the volume) and
# GNU tar. Connection details come from pg0's instance.json and never leave the container.
# The basebackup dir is grafted in as instances/hindsight/data; tar exits non-zero on any
# read problem and set -e turns that into a failed backup.
docker exec "$C" sh -c '
  set -eu
  P=/home/hindsight/.pg0; I=$P/instances/hindsight; BB=/tmp/hc-backup-$$
  trap "rm -rf $BB" EXIT
  v=$(cat $I/data/PG_VERSION); B=$(ls -d $P/installation/$v*/bin | head -1)
  eval "$(python3 -c "import json,shlex;d=json.load(open(\"$I/instance.json\"));print(\"export PGPASSWORD=%s PGUSER=%s PGPORT=%s\" % (shlex.quote(d[\"password\"]), shlex.quote(d[\"username\"]), int(d[\"port\"])))")"
  $B/pg_basebackup -h 127.0.0.1 -D $BB -Fp -X stream -c fast --no-password >&2
  $B/pg_verifybackup -q $BB >&2
  tar czf - -C $P --exclude=./instances/hindsight/data . \
    -C $(dirname $BB) --transform="s,^$(basename $BB),./instances/hindsight/data," $(basename $BB)
' > "$DEST/pg0.tar.gz" || { echo "backup: base backup failed" >&2; exit 1; }
}
# Pin the bank stats to the snapshot: read them just before and just after the base backup.
# If nothing changed in between, they describe exactly what the archive holds, and
# restore-test.sh can hold the restored server to them. A write in between means we cannot
# tell which side of the backup it landed on, so take the backup again. The backup itself is
# valid either way; only the stats record is withheld when it cannot be pinned.
stats(){ printf '%s' "${HC_HINDSIGHT_API_KEY:-}" | docker exec -i "$C" python3 -c "$(cat "$ROOT/scripts/bank_stats.py")"; }
pinned=0
for try in 1 2 3 4 5; do
  S1="$(stats 2>/dev/null)" || S1=""
  take
  [ -n "$S1" ] || { echo "backup: bank stats unavailable - stats.json not written" >&2; break; }
  S2="$(stats 2>/dev/null)" || S2=""
  [ "$S1" = "$S2" ] && { printf '%s\n' "$S1" > "$DEST/stats.json"; pinned=1; break; }
  sleep 3
done
[ "$pinned" = 1 ] || [ -z "$S1" ] || echo "backup: bank stats did not hold still across a base backup - stats.json not written" >&2
F="$DEST/pg0.tar.gz"
SZ="$(stat -f%z "$F" 2>/dev/null || stat -c%s "$F" 2>/dev/null || echo 0)"
# HARD 0-byte guard: a near-empty archive means the backup silently failed.
[ "$SZ" -gt 2000 ] || { echo "backup GUARD FAIL: $F is only $SZ bytes" >&2; exit 1; }
# retention (default 14 days)
find backups -maxdepth 1 -type d -name '20*' -mtime +"${HC_BACKUP_RETENTION_DAYS:-14}" -exec rm -rf {} + 2>/dev/null || true
echo "backup ok: $F ($SZ bytes)" >&2
echo "$F"
