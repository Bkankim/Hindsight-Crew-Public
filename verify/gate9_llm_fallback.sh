#!/usr/bin/env bash
# LLM chain gate (hindsight v1.0.0: claude-code/claude-sonnet-5-5 on EVERY LLM slot, failover account1 -> account2;
# opus-5-5 until 2026-11-01).
#
# Why this gate exists: every failure mode here is silent. A slot that quietly keeps an old provider, a chain
# that collapsed to "account1, then account1", or a leftover xai setting only shows on the day the first account runs dry -
# exactly when the fallback is needed. It judges, per slot (default, retain, reflect, consolidation,
# mental_model_refresh):
#   1. provider/model  - every chain member is claude-code / claude-sonnet-5-5
#   2. chain order     - primary is the account1 token, the fallback is the account2 token (member key fingerprints are
#                        compared with the two keys of the env file; values are never printed)
#   3. strategy        - failover, and a chain of exactly two members (a slot without one is uncovered)
#   4. tokens          - non-empty and different from each other
#   5. xai leftovers   - no xai key in the env file or the container environment, no xai provider anywhere
#   6. CLI             - the pinned CLI is >= 2.1.280 (the bundled 2.1.150 is refused for claude-opus-5-5 with a 400)
# Chains are resolved with the engine's own memory_engine._build_llm, not a re-statement of its inherit-from-
# global rule, so an upstream change to that rule is judged live. The one thing the engine does inline in its
# constructor, the primary of a slot (slot value, else global), is resolved the same way here.
#
# The single-account profile (HINDSIGHT_API_LLM_PROVIDER=claude-code with no member key and no slot key, what
# ./install --claude-token-env writes) has no chain to judge: provider/model per slot, xai and the CLI only.
# The none profile (CI, bootstrap) is consistent without touching the container: the env file says
# HINDSIGHT_API_LLM_PROVIDER=none and declares no other LLM key (slot provider, member key...). lib.sh may still
# look the container up read-only (`compose ps`) while it reports the target; the gate runs no `docker exec`/`run`.
# A global none with any slot-level LLM key is judged in full.
#
# It inspects the hindsight container with `docker exec` (config, CLI version) - there is no gateway surface for
# server-level LLM wiring. It never prints token values; the container gets only one-way fingerprints (12 hex of
# sha256) of the two expected tokens as arguments.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HC_GATE_NEEDS_CONTAINER=1     # the full judgment execs into the hindsight container lib.sh resolves
. "$HERE/lib.sh"

CLI_PATH=/app/api/.venv/lib/python3.11/site-packages/claude_agent_sdk/_bundled/claude
MIN_CLI="${HC_GATE9_MIN_CLI:-2.1.280}"
WANT_MODEL="${HC_GATE9_MODEL:-claude-sonnet-5-5}"

# env file keys (names only): ^HINDSIGHT_API_([A-Z0-9]+_)*LLM_ and anything with XAI in its name
env_keys() { [ -f "$HC_ENV_FILE" ] && sed -nE 's/^(export[[:space:]]+)?([A-Za-z0-9_]+)=.*/\2/p' "$HC_ENV_FILE"; true; }
llm_keys="$(env_keys | grep -E '^HINDSIGHT_API_([A-Z0-9]+_)*LLM_' || true)"
declared() { printf '%s\n' "$llm_keys" | grep -qx "$1"; }

# none profile: nothing but the global provider=none
other_llm_keys="$(printf '%s\n' "$llm_keys" | grep -vx 'HINDSIGHT_API_LLM_PROVIDER' | grep -v '^$' || true)"
if declared HINDSIGHT_API_LLM_PROVIDER && [ "${HINDSIGHT_API_LLM_PROVIDER:-}" = none ] && [ -z "$other_llm_keys" ]; then
  hc_log "LLM none profile - consistent"; exit 0
fi

# single-account profile (what ./install --claude-token-env writes, and what the gate accepted before the chain): global
# provider claude-code and no member key and no slot key at all. Only provider/model per slot, xai and the CLI are judged.
# One member key or one slot key anywhere (the mental-model-refresh chain keys count) makes it the full chain judgment.
member_keys="$(printf '%s\n' "$llm_keys" | grep -E '^HINDSIGHT_API_([A-Z0-9]+_)*LLM_[0-9]+_' || true)"
slot_keys="$(printf '%s\n' "$llm_keys" | grep -E '^HINDSIGHT_API_([A-Z0-9]+_)+LLM_' || true)"
MODE=chain
if declared HINDSIGHT_API_LLM_PROVIDER && [ "${HINDSIGHT_API_LLM_PROVIDER:-}" = claude-code ] && [ -z "$member_keys" ] && [ -z "$slot_keys" ]; then
  MODE=single
  hc_log "single-account profile - no failover chain"
fi

hc_require_container
CONTAINER="$HC_HINDSIGHT_CONTAINER"
problems=""
add_problem() { problems="${problems:+$problems; }$1"; }

# env file side: the two member tokens must be declared, and no xai key may remain (key names only are printed)
if [ "$MODE" = chain ]; then
  for k in HINDSIGHT_API_LLM_API_KEY HINDSIGHT_API_LLM_1_API_KEY; do
    declared "$k" || add_problem "$k is not declared in the env file"
  done
fi
xai_keys="$(env_keys | grep -iE 'xai' | sort -u | tr '\n' ' ' || true)"
[ -z "$xai_keys" ] || add_problem "xai keys remain in the env file: ${xai_keys% }"
xai_prov="$(sed -nE 's/^(export[[:space:]]+)?(HINDSIGHT_API_([A-Z0-9]+_)*LLM_([0-9]+_)?PROVIDER)=["'\'']?xai.*/\2/p' "$HC_ENV_FILE" 2>/dev/null | sort -u | tr '\n' ' ' || true)"
[ -z "$xai_prov" ] || add_problem "xai provider declared in the env file: ${xai_prov% }"

fp() { printf '%s' "$1" | shasum -a 256 | cut -c1-12; }
G9_FP0="$(fp "${HINDSIGHT_API_LLM_API_KEY:-}")"; G9_FP1="$(fp "${HINDSIGHT_API_LLM_1_API_KEY:-}")"
if [ "$MODE" = chain ]; then
  [ -n "${HINDSIGHT_API_LLM_API_KEY:-}" ] || add_problem "HINDSIGHT_API_LLM_API_KEY (account1) is empty"
  [ -n "${HINDSIGHT_API_LLM_1_API_KEY:-}" ] || add_problem "HINDSIGHT_API_LLM_1_API_KEY (account2) is empty"
  [ "$G9_FP0" != "$G9_FP1" ] || add_problem "the env file gives account1 and account2 the same token"
fi

cli="$(docker exec "$CONTAINER" "$CLI_PATH" -v 2>/dev/null | awk '{print $1}')"
[ -n "$cli" ] || { hc_log "CLI at $CLI_PATH did not report a version"; exit 1; }
lowest="$(printf '%s\n%s\n' "$MIN_CLI" "$cli" | sort -t. -k1,1n -k2,2n -k3,3n | head -1)"
[ "$lowest" = "$MIN_CLI" ] || add_problem "CLI $cli < $MIN_CLI (claude-opus-5-5 refused)"

report="$(docker exec "$CONTAINER" python -c '
import sys
try:
    import hashlib, os
    from types import SimpleNamespace
    from hindsight_api.config import get_config
    from hindsight_api.engine.memory_engine import _build_llm, _LLMCallDefaults
    c = get_config()
    want_fp, want_model, single = sys.argv[1:3], sys.argv[3], sys.argv[4] == "single"   # single: one account, no chain
    fp = lambda k: hashlib.sha256((k or "").encode()).hexdigest()[:12]
    problems, lines = [], []
    # slot -> (config prefix, fallback prefix the engine passes). The primary of a slot is the slot value, else the
    # fallback slot (mental_model_refresh -> reflect), else the global one - the engine does this inline.
    SLOTS = (("default", "", ""), ("retain", "retain_", ""), ("reflect", "reflect_", ""),
             ("consolidation", "consolidation_", ""), ("mental_model_refresh", "mental_model_refresh_", "reflect_"))
    def eff(prefix, fallback, name):
        for p in (prefix, fallback, ""):
            v = getattr(c, p + "llm_" + name, None)
            if v:
                return v
        return ""
    for slot, prefix, fallback in SLOTS:
        base = SimpleNamespace(provider=eff(prefix, fallback, "provider"), model=eff(prefix, fallback, "model"),
                               api_key=eff(prefix, fallback, "api_key"))
        built = _build_llm(base, c, prefix, _LLMCallDefaults(None, None, None, None), fallback_prefix=fallback)
        members = list(getattr(built, "members", None) or [base])
        strategy = getattr(built, "strategy", None)
        mode = getattr(strategy, "mode", None)
        lines.append("%s=%s [%s]" % (slot, ">".join("%s/%s" % (m.provider, m.model) for m in members), mode or "no-chain"))
        if not single and len(members) != 2:
            problems.append("%s: chain has %d member(s), want 2 (account1, account2)" % (slot, len(members)))
        if not single and len(members) > 1 and mode != "failover":
            problems.append("%s: strategy=%s (want failover)" % (slot, mode))
        for i, m in enumerate(members):
            if m.provider != "claude-code":
                problems.append("%s: member %d provider=%s (want claude-code)" % (slot, i, m.provider))
            if m.model != want_model:
                problems.append("%s: member %d model=%s (want %s)" % (slot, i, m.model, want_model))
            if single:
                continue
            if not m.api_key:
                problems.append("%s: member %d has no token (shares container auth)" % (slot, i))
            elif i < 2 and fp(m.api_key) != want_fp[i]:
                problems.append("%s: member %d token is not the env file %s key" % (slot, i, ("account1", "account2")[i]))
        keys = [m.api_key for m in members if m.api_key]
        if not single and len(set(keys)) != len(keys):
            problems.append("%s: chain members share a token (chain collapses)" % slot)
    xai = sorted(k for k in os.environ if "XAI" in k.upper())
    if xai:
        problems.append("xai keys in the container environment: " + " ".join(xai))
    print("\n".join("slot " + l for l in lines))
    print("PROBLEMS=" + "; ".join(problems))
except BaseException as e:   # a traceback can quote config values: only the class name leaves the container
    print("ERROR=" + type(e).__name__)
' "$G9_FP0" "$G9_FP1" "$WANT_MODEL" "$MODE" 2>/dev/null)" || { hc_log "config introspection failed (docker exec / python exited non-zero)"; exit 1; }
introspection_error="$(printf '%s\n' "$report" | sed -n 's/^ERROR=//p')"
[ -z "$introspection_error" ] || { hc_log "config introspection failed: $introspection_error"; exit 1; }
printf '%s\n' "$report" | grep '^slot ' | while IFS= read -r l; do hc_log "$l"; done
hc_log "cli=$cli"
container_problems="$(printf '%s\n' "$report" | sed -n 's/^PROBLEMS=//p')"
[ -z "$container_problems" ] || add_problem "$container_problems"
[ -z "$problems" ] || { hc_log "$problems"; exit 1; }
exit 0
