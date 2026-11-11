#!/usr/bin/env bash
# gate7_mcp_enforce.sh — live invariants for MCP-side rule enforcement (N2) and response
# honesty (N5), exercised through the published gateway with a demo principal.
#
# verify-all is a LOCAL, MANUAL invariant suite (it is NOT run in CI): it needs a live
# gateway + hindsight + real demo tokens, none of which exist on a CI runner. CI covers the
# same rules with unit tests (tests/); this gate is what proves the DEPLOYED build behaves.
# So a RED here means "the running container predates/regressed the enforcement", never
# "the code is wrong" on its own — check `docker compose ps` and the image tag first.
#
# alice -> personal-alice, because personal-alice is a STRICT bank in
# policies/bank-policies.json (tags_required, tag_prefixes, content_prefix_pattern,
# allowed_strategies:[]) AND a demo token exists for it. personal-demo is DEMO's real bank and
# is never written to from a gate.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/lib.sh"

A="${HC_DEMO_TOKEN_ALICE:-}"
[ -n "$A" ] || { hc_log "HC_DEMO_TOKEN_ALICE missing (secrets/demo-tokens.env)"; exit 1; }
command -v python3 >/dev/null || { hc_log "python3 required"; exit 1; }
BANK="personal-alice"
M="g7-$$-$RANDOM"

# rewrite_failures from /healthz, or the literal "absent" when the key is not published.
# Tolerant on read, strict on the assertion (④): the pre-N4 gateway has no such key, and
# the honest way to say that is "the counter could not be compared", not a vacuous 0==0.
hz_failures() {
  curl -s --max-time 10 "$HC_GW/healthz" | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin).get("rewrite_failures")
except Exception:
    v = None
print("absent" if v is None else v)'
}

START_RF="$(hz_failures)"
hc_log "healthz rewrite_failures at start: $START_RF"

# ---- ① request-side rejection: no tags on a tags_required bank ----------------
# The content prefix IS compliant on purpose, so the only rule that can fire is
# tags-required and the assertion cannot pass for the wrong reason.
r="$(mcp_call "$A" "$BANK" "$(printf '{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"sync_retain","arguments":{"content":"[검증] gate7 untagged probe %s"}}}' "$M")")" \
  || { hc_log "① MCP call failed (handshake/transport)"; exit 1; }
code="${r##*$'\n'}"; body="${r%$'\n'*}"
# A rule rejection is an MCP-level error, not an HTTP one: the model must read it on the
# isError channel it already handles (403/422 would be invisible to the tool loop).
[ "$code" = "200" ] || { hc_log "① expected HTTP 200 (isError channel), got $code"; exit 1; }
echo "$body" | grep -q '"isError":[[:space:]]*true' \
  || { hc_log "① untagged retain was NOT rejected: $(echo "$body" | head -c 220)"; exit 1; }
echo "$body" | grep -q 'RULE:tags-required' \
  || { hc_log "① rejected but without RULE:tags-required: $(echo "$body" | head -c 220)"; exit 1; }
hc_log "① untagged sync_retain -> isError + RULE:tags-required (HTTP 200)"

# ---- ② correction is visible: timestamp autofill annotates the success ---------
# Fully compliant payload minus "timestamp" -> upstream stores it, and the gateway must say
# out loud which instant it recorded (P5: a correction the client cannot see is a silent one).
r="$(mcp_call "$A" "$BANK" "$(printf '{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"sync_retain","arguments":{"content":"[검증] gate7 autofill probe %s","tags":["project:hindsight-crew","kind:probe"]}}}' "$M")")" \
  || { hc_log "② MCP call failed (handshake/transport)"; exit 1; }
code="${r##*$'\n'}"; body="${r%$'\n'*}"
[ "$code" = "200" ] || { hc_log "② expected HTTP 200, got $code"; exit 1; }
echo "$body" | grep -q '"isError":[[:space:]]*true' \
  && { hc_log "② compliant retain was rejected: $(echo "$body" | head -c 220)"; exit 1; }
# Substring, not the whole note: gateway.honesty.AUTOFILL_NOTE carries the timestamp value.
echo "$body" | grep -q '\[gateway: timestamp 자동 기록' \
  || { hc_log "② no autofill annotation on the success: $(echo "$body" | head -c 220)"; exit 1; }
hc_log "② timestamp-less compliant sync_retain -> success + autofill annotation"

# ---- ③ failure honestification (N5 step 4) — NOT assertable live ---------------
# Flipping a success-shaped frame that carries a failure body requires INJECTING such a
# response, and upstream only emits it under a fault we cannot provoke on a healthy box
# (that is the whole point of D1-②). Faking it here would mean pointing the gate at a stub,
# i.e. asserting against ourselves. Covered by unit tests instead — tests/ drives
# gateway.honesty.annotate_element with the recorded fixtures (fixtures/mcp/), including the
# false-positive regression ('failed' mid-sentence must stay a success).

# ---- ④ no silent fail-open: rewrite_failures delta must be 0 -------------------
END_RF="$(hz_failures)"
case "$START_RF|$END_RF" in
  absent*|*absent)
    hc_log "④ /healthz has no rewrite_failures counter (start=$START_RF end=$END_RF) — pre-N4 gateway?"; exit 1 ;;
esac
[ "$START_RF" = "$END_RF" ] \
  || { hc_log "④ rewrite_failures delta $START_RF -> $END_RF: a response was passed through UNCORRECTED (see audit 'response-rewrite:')"; exit 1; }
hc_log "④ rewrite_failures delta 0 ($END_RF)"

# ---- ⑤ policy/upstream drift: allowed_strategies vs upstream retain_strategies --
cfg_code="$(rest_code GET "$A" "$(hc_bank_path "$BANK")/config")"
if [ "$cfg_code" != "200" ]; then
  hc_log "⑤ SKIP: GET bank config returned $cfg_code (drift comparison needs the live config)"
else
  # Same logic as gateway.bank_policies.strategy_drift (single source of truth for the
  # runtime); re-stated here because a host gate must not import the container's package.
  # KEY EXISTENCE, never truthiness: upstream returns retain_strategies as a present key
  # with a null value when nothing is configured, so `if not cfg.get(...)` would report the
  # healthy case and the genuinely-missing-key case identically.
  out="$(rest_body GET "$A" "$(hc_bank_path "$BANK")/config" | python3 -c '
import json, sys, pathlib

policy_file, bank = sys.argv[1], sys.argv[2]
pol = json.loads(pathlib.Path(policy_file).read_text(encoding="utf-8"))
defaults = pol.get("defaults", {})
declared = pol.get("banks", {}).get(bank, {}).get(
    "allowed_strategies", defaults.get("allowed_strategies"))

try:
    cfg = json.load(sys.stdin)
except Exception as exc:
    print("SKIP config response is not JSON (%s)" % exc); raise SystemExit(2)
if isinstance(cfg, dict) and isinstance(cfg.get("config"), dict):
    cfg = cfg["config"]
if not isinstance(cfg, dict):
    print("SKIP config payload is %s, not an object" % type(cfg).__name__); raise SystemExit(2)
if "retain_strategies" not in cfg:
    print("SKIP upstream config carries no retain_strategies key"); raise SystemExit(2)

raw = cfg["retain_strategies"]
if raw is None:
    names = None
elif isinstance(raw, dict):
    names = {str(k) for k in raw}
elif isinstance(raw, (list, tuple)):
    names = set()
    for it in raw:
        if isinstance(it, str):
            names.add(it)
        elif isinstance(it, dict) and isinstance(it.get("name"), str):
            names.add(it["name"])
        else:
            print("SKIP unexpected retain_strategies element %r" % (it,)); raise SystemExit(2)
else:
    print("SKIP unexpected retain_strategies type %s" % type(raw).__name__); raise SystemExit(2)

if names is None:
    if declared:
        print("DRIFT upstream retain_strategies is null but policy declares %s" % sorted(declared))
        raise SystemExit(1)
    print("OK upstream retain_strategies null, policy allowed_strategies %r" % (declared,))
    raise SystemExit(0)
if declared is None:
    print("OK policy allowed_strategies is null (unrestricted), upstream %s" % sorted(names))
    raise SystemExit(0)
missing = sorted(set(declared) - names)
extra = sorted(names - set(declared))
if missing or extra:
    print("DRIFT missing=%s extra=%s" % (missing, extra)); raise SystemExit(1)
print("OK retain_strategies in sync %s" % sorted(names))
' "$ROOT/policies/bank-policies.json" "$BANK")"
  rc=$?
  case $rc in
    0) hc_log "⑤ $out" ;;
    2) hc_log "⑤ SKIP: ${out#SKIP }" ;;   # non-failing by design (N1 gate condition)
    *) hc_log "⑤ $out"; exit 1 ;;
  esac
fi

hc_log "MCP rule enforcement holds (marker $M)"
exit 0
