#!/usr/bin/env bash
# contract-probe.sh — THE LIFELINE. Introspect the pinned Hindsight image's REAL behavior
# so the gateway ACL + attribution are built against fact, not assumption.
#
# Produces fixtures/contract-probe.out.json with:
#   - tool_set: single-bank vs multi-bank tool names (from tools/list)
#   - bank_resolution: does a tool follow the URL bank or arguments.bank? (decides ACL design)
#   - attribution: does retain ACCEPT an attribution/metadata field AND actually persist it?
#         verified by retain -> recall ROUND-TRIP (NOT introspection alone — execution note #1).
#         => mode: "body-inject" (round-trip proves persistence) | "audit-only" (silent-drop/refused)
#
# Requires a real, digest-pinned image. Run in Stage 2 after docker-compose exists, or standalone
# against an already-running Hindsight reachable at $HC_PROBE_BASE (default http://127.0.0.1:8899).
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ -f .env.local ] && set -a && . ./.env.local && set +a || true
OUT="fixtures/contract-probe.out.json"
BASE="${HC_PROBE_BASE:-http://127.0.0.1:8899}"   # probe hits Hindsight directly (pre-gateway)
KEY="${HC_HINDSIGHT_API_KEY:-}"
IMG="${HC_HINDSIGHT_IMAGE:-}"

die() { echo "contract-probe: $*" >&2; exit 1; }
case "$IMG" in ""|*REPLACE*) die "HC_HINDSIGHT_IMAGE must be a real @sha256 digest (set in .env.local). Refused to probe an unpinned image.";; esac
command -v jq >/dev/null || die "jq required"

# auth header (Hindsight uses a single shared Bearer). It goes to curl on stdin (`-H @-`), not argv, so
# `ps` never shows the key (issue #214); no key = empty stdin = no header.
auth_header() { [ -n "$KEY" ] && printf 'Authorization: Bearer %s\n' "$KEY"; return 0; }

mcp() { # <bank-path> <json-rpc-body>  -> raw response
  auth_header | curl -fsS -H @- -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -X POST "$BASE/mcp/$1/" -d "$2" 2>/dev/null
}
# strip SSE framing if present, return last JSON object
unwrap() { grep -E '^data: ' 2>/dev/null <<<"$1" | sed 's/^data: //' | tail -1 || printf '%s' "$1"; }

echo "contract-probe: image=$IMG base=$BASE" >&2

# 1) tool_set via tools/list on a probe bank
LIST_RAW="$(mcp probe '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')" || die "tools/list failed (is Hindsight up at $BASE?)"
LIST="$(unwrap "$LIST_RAW")"
TOOLS="$(jq -c '[.result.tools[].name] // []' <<<"$LIST" 2>/dev/null || echo '[]')"

# 2) bank_resolution: retain into bank "alpha" via URL, but set arguments.bank="beta",
#    then recall from each via URL to see which bank actually received it.
MARK="probe-$(date +%s)-$RANDOM"
mcp alpha "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"sync_retain\",\"arguments\":{\"bank\":\"beta\",\"content\":\"$MARK\"}}}" >/dev/null 2>&1 || true
RA="$(unwrap "$(mcp alpha "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"recall\",\"arguments\":{\"query\":\"$MARK\"}}}")")"
RB="$(unwrap "$(mcp beta  "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\",\"params\":{\"name\":\"recall\",\"arguments\":{\"query\":\"$MARK\"}}}")")"
IN_ALPHA=$(grep -qF "$MARK" <<<"$RA" && echo true || echo false)
IN_BETA=$(grep -qF "$MARK" <<<"$RB" && echo true || echo false)
if [ "$IN_BETA" = true ] && [ "$IN_ALPHA" = false ]; then RES="arguments.bank";   # body wins -> gateway MUST sanitize body
elif [ "$IN_ALPHA" = true ] && [ "$IN_BETA" = false ]; then RES="url-path";        # URL wins -> path-pin sufficient for this tool
else RES="ambiguous"; fi

# 3) attribution: retain WITH an attribution field, then recall and check it ROUND-TRIPS.
AMARK="attr-$(date +%s)-$RANDOM"
mcp probe "{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"tools/call\",\"params\":{\"name\":\"sync_retain\",\"arguments\":{\"content\":\"$AMARK\",\"metadata\":{\"hc_member\":\"prober-1\"}}}}" >/dev/null 2>&1 || true
AR="$(unwrap "$(mcp probe "{\"jsonrpc\":\"2.0\",\"id\":6,\"method\":\"tools/call\",\"params\":{\"name\":\"recall\",\"arguments\":{\"query\":\"$AMARK\"}}}")")"
if grep -qF 'prober-1' <<<"$AR"; then ATTR_MODE="body-inject"; else ATTR_MODE="audit-only"; fi

mkdir -p fixtures
jq -n --argjson tools "$TOOLS" --arg res "$RES" --arg attr "$ATTR_MODE" \
      --arg img "$IMG" --arg ts "$(date -u +%FT%TZ)" \
      --argjson in_alpha "$IN_ALPHA" --argjson in_beta "$IN_BETA" '{
  schemaVersion: 1, probedImage: $img, probedAt: $ts,
  toolSet: $tools,
  bankResolution: { winner: $res, recalledInAlpha: $in_alpha, recalledInBeta: $in_beta,
    note: "if arguments.bank wins, gateway MUST strip/overwrite body bank, not just rewrite URL" },
  attribution: { mode: $attr,
    note: "body-inject only when retain->recall round-trip PROVES metadata persisted; else audit-only fallback" }
}' > "$OUT"
echo "contract-probe: wrote $OUT" >&2
jq . "$OUT"
