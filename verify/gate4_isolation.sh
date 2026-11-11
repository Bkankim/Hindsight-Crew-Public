#!/usr/bin/env bash
# 옵션 B로 준수화 (2026-07-26): team-eng is now REGISTERED in policies/bank-policies.json with a
# tags-only profile (tags_required + <allowed-prefix>:<slug> grammar; content prefix stays a
# personal-bank rule). The retain below therefore carries compliant tags, and its status is
# asserted — an unnoticed 422 rule-violation would otherwise surface as the much vaguer
# "attribution probe not recalled" failure. A tagged payload is valid on the pre-N3 gateway too.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/lib.sh"
A="$HC_DEMO_TOKEN_ALICE"
c=$(rest_code POST "$A" "$(hc_bank_path personal-bob)/memories/recall" '{"query":"x"}')
[ "$c" = "403" ] || { hc_log "cross-tenant alice->bob expected 403 got $c"; exit 1; }
c=$(rest_code POST "$A" "$(hc_bank_path personal-alice)/memories/recall" '{"query":"x"}')
[ "$c" = "200" ] || { hc_log "own bank expected 200 got $c"; exit 1; }
# team-retain attribution round-trip: gateway stamps hc_member=alice and it must persist+recall
M="attr-$$-$RANDOM"
code=$(rest_code POST "$A" "$(hc_bank_path team-eng)/memories" \
  "{\"items\":[{\"content\":\"attribution probe $M\",\"tags\":[\"project:hindsight-crew\",\"kind:probe\"],\"metadata\":{\"hc_member\":\"bob\"}}]}")   # forge bob
[ "$code" = "200" ] || { hc_log "team retain expected 200 got $code (rule-violation? see gateway audit)"; exit 1; }
sleep 3
body=$(rest_body POST "$A" "$(hc_bank_path team-eng)/memories/recall" "{\"query\":\"attribution probe $M\"}")
echo "$body" | grep -q "$M" || { hc_log "attribution probe not recalled"; exit 1; }
echo "$body" | grep -q '"hc_member":"alice"' || { hc_log "attribution NOT alice (forgery not overwritten): $(echo "$body"|head -c 160)"; exit 1; }
echo "$body" | grep -q '"hc_member":"bob"' && { hc_log "forged bob attribution leaked!"; exit 1; }
hc_log "isolation + team-retain attribution (forgery overwritten, round-trip) ok"; exit 0
