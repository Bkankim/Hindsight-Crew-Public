#!/usr/bin/env bash
# Drift probe: confirm the live image still honors the contract the gateway depends on —
# bank-in-path routing AND data-layer cross-bank isolation (a marker in one bank must not
# surface in another). If this RED, the gateway's whole isolation model diverged from reality.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/lib.sh"
# personal-alice is a STRICT bank (policies/bank-policies.json): the seed payload carries the
# required "[label] " content prefix + <allowed-prefix>:<slug> tags so the write is judged on
# the CONTRACT under test (bank routing/isolation), never on a format rejection (N3, HTTP 422).
# Compliant payloads are accepted by the pre-N3 gateway as well.
A="$HC_DEMO_TOKEN_ALICE"; M="drift-$$-$RANDOM"
code=$(rest_code POST "$A" "$(hc_bank_path personal-alice)/memories" \
  "{\"items\":[{\"content\":\"[검증] drift marker $M lives only in personal\",\"tags\":[\"project:hindsight-crew\",\"kind:probe\"]}]}")
[ "$code" = "200" ] || { hc_log "seed retain expected 200 got $code (rule-violation? see gateway audit)"; exit 1; }
sleep 3
inA=$(rest_body POST "$A" "$(hc_bank_path personal-alice)/memories/recall" "{\"query\":\"drift marker $M\"}")
echo "$inA" | grep -q "$M" || { hc_log "marker not recalled in its own bank (path routing broke)"; exit 1; }
inT=$(rest_body POST "$A" "$(hc_bank_path team-eng)/memories/recall" "{\"query\":\"drift marker $M\"}")
echo "$inT" | grep -q "$M" && { hc_log "DRIFT: marker leaked into another bank (data isolation broke)"; exit 1; }
hc_log "contract holds: bank-in-path routing + data-layer cross-bank isolation"; exit 0
