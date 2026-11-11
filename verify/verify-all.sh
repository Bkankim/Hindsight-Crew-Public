#!/usr/bin/env bash
# verify-all.sh — release gate orchestrator for Hindsight-Crew.
#
# Runs the 9 acceptance gates + contract-drift probe (10 entries total),
# aggregating a single
# exit code. Exit 0 ONLY when every gate is PASS. Any FAIL or NOT_IMPLEMENTED → non-zero.
# This is the success definition: `./bootstrap` is "green" iff `verify-all.sh` exits 0.
#
# Gate contract (each verify/gate*.sh): exit 0=PASS, 1=FAIL, 2=NOT_IMPLEMENTED.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The copy stack (HC_TARGET=next) is checked gate by gate: gate6 handles the hfcache and restore volumes the
# production way (it may even stop hindsight), which is wrong for a copy. Refuse before anything is resolved.
if [ "${HC_TARGET:-}" = next ]; then
  echo "verify-all: HC_TARGET=next - run the gates of the copy stack one by one (bash verify/gateN_*.sh), never verify-all" >&2
  exit 1
fi
# shellcheck source=verify/lib.sh
. "$HERE/lib.sh"      # also loads HC_ENV_FILE, the caller's environment taking precedence
ROOT="$(cd "$HERE/.." && pwd)"

# gate id | human label
GATES=(
  "gate1_health.sh|① 8888 health (gateway front)"
  "gate2_banks.sh|② personal/team bank provisioned"
  "gate3_roundtrip.sh|③ sync_retain -> recall roundtrip"
  "gate4_isolation.sh|④ tenant isolation + team-retain attribution"
  "gate5_adversarial.sh|⑤ adversarial inputs rejected"
  "gate_contract_drift.sh|✦ contract-drift probe (live contract)"
  "gate7_mcp_enforce.sh|⑦ MCP rule enforcement (reject/autofill/no fail-open)"
  "gate8_rerank_provider.sh|⑧ reranker on Mac MPS sidecar (no silent CPU fallback)"
  "gate9_llm_fallback.sh|⑨ LLM claude-code/opus chain on every slot (account1->account2 failover, CLI>=2.1.280, per-member tokens)"
  "gate6_restore.sh|⑥ backup restore-test"
)

declare -i fail=0 notimpl=0 pass=0
printf '\n=== Hindsight-Crew verify-all ===\n'
for entry in "${GATES[@]}"; do
  script="${entry%%|*}"; label="${entry#*|}"
  path="$HERE/$script"
  if [ ! -x "$path" ] && [ ! -f "$path" ]; then
    printf '  %-6s %s\n' "MISS" "$label (missing $script)"; notimpl+=1; continue
  fi
  bash "$path" >/tmp/hc-gate.$$ 2>&1
  rc=$?
  case $rc in
    0) printf '  %-6s %s\n' "GREEN" "$label"; pass+=1 ;;
    2) printf '  %-6s %s\n' "TODO" "$label"; notimpl+=1 ;;
    *) printf '  %-6s %s\n' "RED" "$label"; fail+=1
       sed 's/^/        /' /tmp/hc-gate.$$ | tail -6 ;;
  esac
done
rm -f /tmp/hc-gate.$$

total=${#GATES[@]}
printf '\n  summary: %d GREEN / %d RED / %d TODO  (of %d)\n' "$pass" "$fail" "$notimpl" "$total"

# artifacts/verify-receipt.json used to be maintained by hand and drifted: it claimed a
# 7-gate cpu-en run long after the suite had nine gates and the deployment had moved to
# ko-full-mac-mps, so it described a configuration nobody was running. A receipt is only
# worth anything as the by-product of a real run, so it is written from the run that just
# happened - gate list from GATES, profile from the live .env.local, commit from git.
#
# Off by default: writing into artifacts/ on every invocation would leave the worktree
# dirty after routine verification, and dirty files get swept into unrelated commits.
if [ "${HC_WRITE_RECEIPT:-0}" = "1" ]; then
  receipt="$ROOT/artifacts/verify-receipt.json"
  mkdir -p "$ROOT/artifacts"
  result="GREEN"; [ "$pass" -eq "$total" ] || result="NOT GREEN"
  {
    printf '{\n'
    printf '  "kind": "typed-receipt",\n'
    printf '  "surface": "api",\n'
    printf '  "endpoint": "%s (gateway)",\n' "${HC_GW:-http://127.0.0.1:8888}"
    printf '  "result": "verify-all %d/%d %s",\n' "$pass" "$total" "$result"
    printf '  "gates": [\n'
    i=0
    for entry in "${GATES[@]}"; do
      i=$((i + 1)); sep=","; [ "$i" -eq "$total" ] && sep=""
      printf '    "%s"%s\n' "${entry%%|*}" "$sep"
    done
    printf '  ],\n'
    printf '  "exitCode": %d,\n' "$([ "$pass" -eq "$total" ] && echo 0 || echo 1)"
    printf '  "profile": "%s",\n' "${HC_PROFILE:-unknown}"
    printf '  "image": "%s",\n' "${HC_HINDSIGHT_IMAGE:-unknown}"
    printf '  "commit": "%s",\n' "$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
    printf '  "worktreeDirtyPaths": %s,\n' "$(git -C "$ROOT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
    printf '  "capturedAt": "%s"\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '}\n'
  } > "$receipt"
  printf '  receipt: %s\n' "${receipt#"$ROOT"/}"
fi

if [ "$pass" -eq "$total" ]; then
  printf '  RESULT: GREEN (exit 0)\n\n'; exit 0
fi
printf '  RESULT: NOT GREEN (exit 1)\n\n'; exit 1
