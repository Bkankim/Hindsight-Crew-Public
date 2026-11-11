#!/usr/bin/env bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/lib.sh"
A="$HC_DEMO_TOKEN_ALICE"; bad=0
chk(){ [ "$3" = "$2" ] || { hc_log "FAIL $1: expected $2 got $3"; bad=$((bad+1)); }; }
chk no-token       403 "$(rest_code POST ""        "$(hc_bank_path personal-alice)/memories/recall" '{"query":"x"}')"
chk unknown-token  403 "$(rest_code POST "nope-tok" "$(hc_bank_path personal-alice)/memories/recall" '{"query":"x"}')"
chk cross-tenant   403 "$(rest_code POST "$A"      "$(hc_bank_path personal-bob)/memories/recall" '{"query":"x"}')"
chk bank-collection 403 "$(rest_code GET "$A" "/v1/$HC_TENANT/banks")"
chk traversal-bank 403 "$(hc_curl_auth "$A" -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$HC_GW/v1/$HC_TENANT/banks/%2e%2e/memories/recall" -H 'content-type: application/json' -d '{}')"
chk destructive-on-team 403 "$(rest_code DELETE "$A" "$(hc_bank_path team-eng)/memories/some-id")"
[ "$bad" = "0" ] && { hc_log "all adversarial inputs rejected"; exit 0; }
hc_log "$bad adversarial checks failed"; exit 1
