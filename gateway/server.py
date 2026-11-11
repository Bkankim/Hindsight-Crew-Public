"""Gateway entrypoint — wires create_app() from environment and serves it.

Env:
  HC_ACL_FILE            path to ACL json (default /secrets/acl.json)
  HC_HINDSIGHT_API_KEY   upstream Hindsight shared Bearer key (held ONLY here)
  HC_UPSTREAM_BASE       upstream Hindsight base (default http://hindsight:8888)
  HC_AUDIT_LOG           audit log path (default /audit/gateway.log)
  HC_ATTRIBUTION_MODE    "body-inject" (default) | "audit-only"  (set from contract-probe)
  HC_BANK_POLICIES       per-bank rule profiles (default policies/bank-policies.json;
                         absent = defaults-only + warning, present-but-broken = fail-fast)
  HC_CONTEXT_TRIGGERS    길목 주입 trigger table (issue #12; default policies/context-triggers.json;
                         absent = empty table + warning, present-but-broken = fail-fast)
  HC_BANK_POLICIES_LOCAL / HC_CONTEXT_TRIGGERS_LOCAL
                         per-install overlays merged by the loaders (issue #187; default
                         policies/local/<same name>, gitignored; absent = tracked file alone,
                         present-but-broken = fail-fast)
  HC_RECALL_QUERY_LOG    recall query ledger for trigger drafts (default /audit/recall-queries.log)
  HC_PENDING_TRIGGERS    auto-drafted trigger rows awaiting approval (default /audit/pending-triggers.jsonl)
  HC_NOTIFY_DISCORD_BOT_TOKEN / HC_NOTIFY_DISCORD_CHANNEL_ID
                         Discord bot for recurrence notifications (unset = notification skipped)
  HC_CONTRADICTION_MIN_RERANK / HC_RECURRENCE_MIN_RERANK / HC_RECURRENCE_WINDOW_MIN
                         gate thresholds (defaults 0.90 / 0.92 / 60)
  HC_GATEWAY_PORT        listen port (default 8888)
"""
from __future__ import annotations

import os

import uvicorn

from .acl import ACL
from .audit import Audit
from .bank_policies import BankPolicies, DEFAULT_POLICY_FILE
from .context_triggers import TriggerTable, DEFAULT_TRIGGER_FILE
from .recurrence import RecallLedger, PendingTriggers
from .app import create_app


def build():
    acl = ACL.load(os.environ.get("HC_ACL_FILE", "/secrets/acl.json"))
    audit = Audit(os.environ.get("HC_AUDIT_LOG", "/audit/gateway.log"))
    mode = os.environ.get("HC_ATTRIBUTION_MODE", "body-inject")
    # Validated here (import time, synchronously) so a broken policy file kills startup
    # instead of silently degrading to loose rules.
    # Path resolution only; the tracked + policies/local/ merge lives in the loaders (#187).
    bank_policies = BankPolicies.load(os.environ.get("HC_BANK_POLICIES", DEFAULT_POLICY_FILE),
                                      local_path=os.environ.get("HC_BANK_POLICIES_LOCAL"))
    trigger_table = TriggerTable.load(os.environ.get("HC_CONTEXT_TRIGGERS", DEFAULT_TRIGGER_FILE),
                                      local_path=os.environ.get("HC_CONTEXT_TRIGGERS_LOCAL"))
    return create_app(
        acl=acl,
        audit=audit,
        upstream_base=os.environ.get("HC_UPSTREAM_BASE", "http://hindsight:8888"),
        upstream_key=os.environ.get("HC_HINDSIGHT_API_KEY", ""),
        attribution_enabled=(mode != "audit-only"),
        bank_policies=bank_policies,
        trigger_table=trigger_table,
        recall_ledger=RecallLedger(os.environ.get("HC_RECALL_QUERY_LOG", "/audit/recall-queries.log")),
        pending_triggers=PendingTriggers(os.environ.get("HC_PENDING_TRIGGERS", "/audit/pending-triggers.jsonl")),
    )


app = build()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("HC_GATEWAY_PORT", "8888")))
