#!/usr/bin/env bash
# Gate 2 - personal and team bank provisioned.
# The personal bank goes through the gateway with its owner's token. A team bank profile is not a
# member's to write (issue #473 a: the bank-root PUT gets the MCP owner check, own personal bank only),
# so the gate first checks the gateway refuses alice's PUT on team-eng, then provisions team-eng the
# operator way: upstream inside the hindsight container (localhost:8888), upstream key on stdin only.
HC_GATE_NEEDS_CONTAINER=1     # the team bank is provisioned by an exec into the hindsight container lib.sh resolves
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/lib.sh"

# Runs INSIDE the hindsight container: PUT the bank profile on the container's own API and print the
# HTTP status. argv = tenant bank; the key arrives on stdin so no process list ever shows it.
HC_GATE2_PUT_PY='
import json, sys, urllib.error, urllib.parse, urllib.request
tenant, bank = sys.argv[1], sys.argv[2]
key = sys.stdin.read().strip()
url = "http://localhost:8888/v1/%s/banks/%s" % (urllib.parse.quote(tenant, safe=""), urllib.parse.quote(bank, safe=""))
req = urllib.request.Request(url, data=json.dumps({"name": bank}).encode("utf-8"), method="PUT",
                             headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(req, timeout=25).status)
except urllib.error.HTTPError as e:
    print(e.code)
except urllib.error.URLError as e:   # refused, DNS, timeout inside urlopen: the kind only, never the key or a body
    print("error:URLError:%s" % type(e.reason).__name__)
except Exception as e:  # noqa: BLE001 - socket timeout while reading, anything else: one line, the kind only
    print("error:%s" % type(e).__name__)
'

c=$(rest_code PUT "$HC_DEMO_TOKEN_ALICE" "$(hc_bank_path personal-alice)" '{"name":"personal-alice"}')
case "$c" in 200|201) ;; *) hc_log "create personal-alice (owner, via gateway) -> $c"; exit 1;; esac

c=$(rest_code PUT "$HC_DEMO_TOKEN_ALICE" "$(hc_bank_path team-eng)" '{"name":"team-eng"}')
[ "$c" = "403" ] || { hc_log "member PUT on team-eng via gateway -> $c (want 403: a member must not overwrite a team bank profile)"; exit 1; }

[ -n "${HC_HINDSIGHT_API_KEY:-}" ] || { hc_log "HC_HINDSIGHT_API_KEY missing - the team bank is provisioned with the upstream key (HC_ENV_FILE)"; exit 1; }
hc_require_container
c=$(printf '%s' "$HC_HINDSIGHT_API_KEY" | docker exec -i "$HC_HINDSIGHT_CONTAINER" python3 -c "$HC_GATE2_PUT_PY" "$HC_TENANT" team-eng 2>/dev/null | tr -d '\r\n')
case "$c" in 200|201) ;; *) hc_log "create team-eng (operator path, upstream in $HC_HINDSIGHT_CONTAINER) -> ${c:-no status}"; exit 1;; esac

# confirm provisioned: recall on the bank responds 200 (empty bank returns {"results":[]})
for b in personal-alice team-eng; do
  g=$(rest_code POST "$HC_DEMO_TOKEN_ALICE" "$(hc_bank_path "$b")/memories/recall" '{"query":"_provision_check_"}')
  [ "$g" = "200" ] || { hc_log "confirm $b -> $g"; exit 1; }
done
hc_log "personal-alice (owner PUT) + team-eng (member PUT 403, operator PUT) provisioned, recall 200"; exit 0
