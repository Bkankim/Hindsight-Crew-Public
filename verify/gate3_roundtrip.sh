#!/usr/bin/env bash
# personal-alice is a STRICT bank (policies/bank-policies.json): retain must carry a
# "[label] " content prefix and <allowed-prefix>:<slug> tags, or the gateway answers 422
# rule-violation (N3). A compliant payload is valid on the pre-N3 gateway too, so this gate
# does not fork by version. The retain status is asserted, not discarded: a silently failed
# write would otherwise surface as the (much vaguer) "recall missing marker" failure.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/lib.sh"
MARK="rt-$$-$RANDOM"
code=$(rest_code POST "$HC_DEMO_TOKEN_ALICE" "$(hc_bank_path personal-alice)/memories" \
  "{\"items\":[{\"content\":\"[검증] verify roundtrip marker $MARK regarding quarterly budget\",\"tags\":[\"project:hindsight-crew\",\"kind:probe\"]}]}")
[ "$code" = "200" ] || { hc_log "retain expected 200 got $code (rule-violation? see gateway audit)"; exit 1; }
sleep 3
body=$(rest_body POST "$HC_DEMO_TOKEN_ALICE" "$(hc_bank_path personal-alice)/memories/recall" \
  '{"query":"quarterly budget roundtrip marker"}')
echo "$body" | grep -q "$MARK" && { hc_log "sync_retain->recall round-trip ok"; exit 0; }
hc_log "recall missing marker: $(echo "$body" | head -c 160)"; exit 1
