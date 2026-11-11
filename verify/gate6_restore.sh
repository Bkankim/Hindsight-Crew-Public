#!/usr/bin/env bash
# 옵션 B로 준수화 (2026-07-26): the DR marker is seeded into team-eng, which is now REGISTERED in
# policies/bank-policies.json with a tags-only profile (tags_required + <allowed-prefix>:<slug>;
# no content-prefix rule — that stays personal-bank only). The seed payload carries compliant
# tags and its status is asserted, so a format rejection can never masquerade as a DR failure.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/lib.sh"
A="$HC_DEMO_TOKEN_ALICE"
# Issue #14: a copy of pg0 taken while postgres was writing is torn, and crash recovery would
# accept it silently. restore-test must refuse any archive that is not a verified base backup,
# and must do so before it boots anything or considers stopping production.
NEG="$ROOT/backups/.gate6-neg-$$"; mkdir -p "$NEG/x/instances/hindsight/data"
echo 18 > "$NEG/x/instances/hindsight/data/PG_VERSION"; tar czf "$NEG/pg0.tar.gz" -C "$NEG/x" .
neg=$(bash "$ROOT/scripts/restore-test.sh" "$NEG/pg0.tar.gz" 2>&1); nrc=$?; rm -rf "$NEG"
[ "$nrc" != 0 ] && echo "$neg" | grep -q "not a verified base backup" \
  || { hc_log "restore-test accepted a non-base-backup archive (rc=$nrc): $(echo "$neg" | tail -1)"; exit 1; }
# Seed a UNIQUE DR marker into team-eng on the main stack, so the restore assertion checks data
# we KNOW is in the backup — deterministic and model-independent (not whatever other gates left).
MARK="restore-marker-$$-$RANDOM"
CONTENT="restore drill marker $MARK"
code=$(rest_code POST "$A" "$(hc_bank_path team-eng)/memories" \
  "{\"items\":[{\"content\":\"$CONTENT\",\"tags\":[\"project:hindsight-crew\",\"kind:probe\"]}]}")
[ "$code" = "200" ] || { hc_log "DR marker retain expected 200 got $code (rule-violation? see gateway audit)"; exit 1; }
# Wait until it is actually recallable on main (=> persisted to pg0) BEFORE taking the backup.
seeded=0
for i in $(seq 1 12); do
  b=$(rest_body POST "$A" "$(hc_bank_path team-eng)/memories/recall" "{\"query\":\"$CONTENT\"}")
  echo "$b" | grep -q "$MARK" && { seeded=1; break; }
  sleep 2
done
[ "$seeded" = 1 ] || { hc_log "restore: DR marker not persisted on main before backup"; exit 1; }
F=$(bash "$ROOT/scripts/backup.sh" 2>/tmp/hc-backup.err) || { hc_log "backup failed: $(tail -1 /tmp/hc-backup.err)"; exit 1; }
hc_log "backup: $(tail -1 /tmp/hc-backup.err)"
HC_RESTORE_MARKER="$CONTENT" bash "$ROOT/scripts/restore-test.sh" "$F" >/tmp/hc-rt.out 2>&1 \
  || { hc_log "restore-test failed: $(tail -1 /tmp/hc-rt.out)"; exit 1; }
# Issue #14: the marker proves one row survived; the stats pinned at backup time prove the
# whole snapshot did. This backup was just taken here, so its stats must have been pinned.
grep -q "bank stats match backup" /tmp/hc-rt.out \
  || { hc_log "restore-test did not match bank stats: $(grep -i stats /tmp/hc-rt.out | tail -1)"; exit 1; }
hc_log "$(tail -1 /tmp/hc-rt.out)"; exit 0
