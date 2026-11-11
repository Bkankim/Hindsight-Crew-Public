#!/usr/bin/env bash
# restore-test.sh — prove a backup actually restores: extract pg0 into a throwaway volume,
# boot a throwaway hindsight on it (sharing the model cache for speed), and recall the DR
# marker that gate6 seeded before the backup. Success = data survived AND the app works on it.
# The marker text is passed via HC_RESTORE_MARKER (gate6 sets it; falls back for manual runs).
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
[ -f .env.local ] && { set -a; . ./.env.local; set +a; }
F="${1:-$(ls -t backups/*/pg0.tar.gz 2>/dev/null | head -1)}"
[ -f "$F" ] || { echo "restore-test: no backup file"; exit 1; }
IMG="${HC_HINDSIGHT_IMAGE:-ghcr.io/vectorize-io/hindsight@sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06}"
KEY="${HC_HINDSIGHT_API_KEY:?}"
RV=hc-restore-pg0; RC=hc-restore-hs; PORT=18899
HF="$(docker volume ls --format '{{.Name}}' | grep -E 'hindsight-crew_hfcache$' | head -1)"
# Stopping the main hindsight to make room for the throwaway is a production outage, so
# it is now conditional on the VM actually being short of memory, and guarded twice when
# it does happen.
#
#  0. The stop only happens if the VM lacks headroom. That constraint dates from a 2GB
#     VM; measured 2026-07-26 the VM has 10.4GB with 8.0GB available while hindsight is
#     resident in 1.24GB, so the throwaway fits alongside it and nothing is stopped -
#     the outage window is zero on a machine this size.
#  1. When a stop is still required, the cleanup trap covers INT/TERM/HUP, not just
#     EXIT, so only SIGKILL escapes it.
#  2. If something does escape, a marker file survives both the kill and a reboot. The
#     next run sees it and restarts production before doing anything else. Without this,
#     MAIN_WAS_UP below reads a stranded main as "was never up" and cleanup leaves it
#     down forever - which is how a killed run kept production down for two hours on
#     2026-07-26.
STRANDED_MARK="$ROOT/.hc-restore-test-stopped-main"
if [ -f "$STRANDED_MARK" ]; then
  echo "restore-test: previous run left main hindsight stopped - restarting it first" >&2
  docker compose --env-file .env.local start hindsight >/dev/null 2>&1 || true
  rm -f "$STRANDED_MARK"
fi

MAIN_WAS_UP=0
if docker compose --env-file .env.local ps --status running 2>/dev/null | grep -q hindsight; then MAIN_WAS_UP=1; fi
cleanup(){ docker rm -f "$RC" >/dev/null 2>&1 || true; docker volume rm "$RV" >/dev/null 2>&1 || true; if [ "$MAIN_WAS_UP" = 1 ]; then docker compose --env-file .env.local start hindsight >/dev/null 2>&1 || true; rm -f "$STRANDED_MARK"; fi; }
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM
trap 'cleanup; exit 129' HUP
trap cleanup EXIT
cleanup
docker volume create "$RV" >/dev/null
docker run --rm -v "$RV":/data -v "$(cd "$(dirname "$F")" && pwd)":/b alpine sh -c "tar xzf /b/$(basename "$F") -C /data" || { echo "restore extract failed"; exit 1; }
# Issue #14: a copy of a live data directory can be torn, and postgres crash recovery would
# boot it without complaint - so a clean boot proves nothing. Only a base backup whose
# manifest verifies (files + WAL) counts. This runs before the stop decision below, so a bad
# archive is rejected without ever touching production. The binaries come from the archive's
# own .pg0/installation (the image does not ship postgres), matched to the data's PG_VERSION.
PGD=/home/hindsight/.pg0/instances/hindsight/data
vout=$(docker run --rm --network none --entrypoint sh -v "$RV":/home/hindsight/.pg0 "$IMG" -c "
  [ -f $PGD/backup_manifest ] || { echo 'no backup_manifest'; exit 1; }
  v=\$(cat $PGD/PG_VERSION); b=\$(ls -d /home/hindsight/.pg0/installation/\$v*/bin 2>/dev/null | head -1)
  [ -n \"\$b\" ] || { echo \"no postgres \$v binaries in archive\"; exit 1; }
  \$b/pg_verifybackup -q $PGD" 2>&1) \
  || { echo "restore-test FAIL: $F is not a verified base backup: $(echo "$vout" | tail -1)"; exit 1; }
# Decide whether the stop is actually necessary. HC_RESTORE_MIN_AVAIL_MB is the headroom
# the throwaway needs beside production; 3000 is ~2.4x hindsight's measured 1.24GB
# resident set, which covers the model-load peak. If MemAvailable cannot be read we fall
# back to stopping, because guessing wrong toward "keep both up" risks an OOM kill of
# production - a worse outage than the planned stop.
AVAIL_MB=$(docker run --rm --network none alpine sh -c "awk '/MemAvailable/{print int(\$2/1024)}' /proc/meminfo" 2>/dev/null | tr -dc '0-9')
MIN_AVAIL_MB="${HC_RESTORE_MIN_AVAIL_MB:-3000}"
if [ "$MAIN_WAS_UP" = 1 ]; then
  if [ -n "$AVAIL_MB" ] && [ "$AVAIL_MB" -ge "$MIN_AVAIL_MB" ]; then
    echo "restore-test: ${AVAIL_MB}MB available >= ${MIN_AVAIL_MB}MB, keeping main hindsight up (no outage)" >&2
  elif [ -n "$(docker ps -q --filter label=hc-bench=1)" ]; then
    # The #13 bench replica is what is eating the headroom. Stopping production to make room
    # for a restore drill is never the right trade while a bench holds memory - refuse instead.
    echo "restore-test: only ${AVAIL_MB:-unknown}MB available and a bench replica (label hc-bench=1) is running - refusing to stop production; run 'bench/replica.py down' first" >&2
    exit 1
  else
    echo "restore-test: only ${AVAIL_MB:-unknown}MB available (< ${MIN_AVAIL_MB}MB), stopping main hindsight for the throwaway" >&2
    : > "$STRANDED_MARK"
    docker compose --env-file .env.local stop hindsight >/dev/null 2>&1 || true
  fi
fi
HFARG=""; [ -n "$HF" ] && HFARG="-v $HF:/home/hindsight/.cache/huggingface"
# `-e NAME` without a value takes it from docker's own environment, so the key never lands in docker's argv
# (visible to `ps`, issue #214). The values are set for this one command only: exporting them script-wide would
# also feed the `docker compose start` in cleanup().
HINDSIGHT_API_MCP_AUTH_TOKEN="$KEY" \
HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL="${HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL:-BAAI/bge-m3}" \
HINDSIGHT_API_RERANKER_LOCAL_MODEL="${HINDSIGHT_API_RERANKER_LOCAL_MODEL:-BAAI/bge-reranker-v2-m3}" \
docker run -d --name "$RC" \
  -e HINDSIGHT_API_MCP_AUTH_TOKEN -e HINDSIGHT_API_LLM_PROVIDER=none \
  -e HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL \
  -e HINDSIGHT_API_RERANKER_LOCAL_MODEL \
  -v "$RV":/home/hindsight/.pg0 $HFARG -p 127.0.0.1:$PORT:8888 "$IMG" >/dev/null || { echo "restore boot failed"; exit 1; }
ok=0
for i in $(seq 1 50); do
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/health" 2>/dev/null || echo 000)
  [ "$c" = "200" ] && { ok=1; break; }; sleep 3
done
[ "$ok" = "1" ] || { echo "restored stack not healthy"; docker logs "$RC" 2>&1 | tail -8; exit 1; }
# Issue #14: hold the restored server to the stats backup.sh pinned at backup time. Older or
# unpinned backups have no stats.json; say so rather than pass silently on a missing check.
SJ="$(dirname "$F")/stats.json"
if [ -f "$SJ" ]; then
  got=$(printf '%s' "$KEY" | docker exec -i "$RC" python3 -c "$(cat "$ROOT/scripts/bank_stats.py")" 2>&1) \
    || { echo "restore-test FAIL: could not read bank stats on restored stack: $(echo "$got" | tail -1)"; exit 1; }
  diff=$(python3 -c 'import json,sys;a=json.load(open(sys.argv[1]));b=json.loads(sys.argv[2]);print(" ".join(sorted(k for k in set(a)|set(b) if a.get(k)!=b.get(k))))' "$SJ" "$got")
  [ -z "$diff" ] || { echo "restore-test FAIL: bank stats differ from backup for: $diff"; exit 1; }
  echo "restore-test: bank stats match backup ($(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "$SJ") banks)"
else
  echo "restore-test: no stats.json beside $F - bank stats comparison skipped"
fi
MARK="${HC_RESTORE_MARKER:-release freeze}"
body=$(printf 'authorization: Bearer %s\n' "$KEY" | curl -s --max-time 25 -X POST "http://127.0.0.1:$PORT/v1/default/banks/team-eng/memories/recall" \
  -H @- -H 'content-type: application/json' -d "{\"query\":\"$MARK\"}")
echo "$body" | grep -qi "$MARK" && { echo "restore-test ok: data survived + recall works on restored stack"; exit 0; }
echo "restore-test FAIL: marker not recalled: $(echo "$body" | head -c 160)"; exit 1
