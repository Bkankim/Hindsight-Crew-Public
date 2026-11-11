"""Authorization policy + body sanitization (pure, unit-testable).

The gateway is body-aware deny-by-default. Bank is resolved ONLY from the
client-requested path bank checked against the ACL; the request body's
`arguments.bank` is never trusted — it is stripped/overwritten. Team-retain
attribution is overwritten with the caller's identity (no forgery).

Order contract (enforced by app.py): **decide() FIRST → rules (validate/autofill) →
sanitize (mutate)**. Nothing is corrected or rewritten before authorization passes for
EVERY element, and format rules (gateway.rules, N2) judge the client's own arguments
before this module's mutations, so a model is never told it broke a rule about a value
the gateway itself wrote. sanitize_arguments() therefore always runs last and has the
final word on bank pinning and attribution.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

from .acl import Principal
from .bank_policies import BankPolicy

# Tools that mutate/destroy — allowed ONLY on the caller's personal bank (never team).
DESTRUCTIVE_TOOLS = frozenset({
    "delete_bank", "clear_memories", "delete_document", "update_bank",
})
# Corrective/curation mutators (fix or retire a single fact, manage mental models/
# directives/ops) — allowed ONLY on the caller's personal bank; team facts stay
# immutable through the gateway.
CORRECTIVE_TOOLS = frozenset({
    "update_memory", "invalidate_memory", "cancel_operation",
    "create_mental_model", "update_mental_model", "refresh_mental_model",
    "clear_mental_model", "delete_mental_model",
    "create_directive", "delete_directive",
})
# Retain-family tools carry content; on a team bank we inject member attribution.
RETAIN_TOOLS = frozenset({"retain", "sync_retain"})
# Read/recall family + bank-scoped introspection - allowed on any bank the principal
# may access. Bank is pinned by URL path + ACL, so none of these reach another bank.
READ_TOOLS = frozenset({
    "recall", "reflect", "list_memories", "get_memory",
    "get_bank", "get_bank_stats", "list_documents", "get_document", "list_tags",
    "list_mental_models", "get_mental_model", "list_directives",
    # Async-op status reads — retain is async; completion must be verifiable.
    "list_operations", "get_operation",
})
# upstream v1.1.0 knowledge base READ tools (issue #302). Deliberately NOT in READ_TOOLS: they are
# allowed only while HC_KB_READ_TOOLS is on (default off), so the static allow lists - and the
# #247 pin that no knowledge tool is on them - stay as they were. Once on they follow the same
# rule as the other reads (any bank the principal may access, bank pinned by URL path + ACL).
# The 4 knowledge WRITE tools (create_knowledge_folder/page, update/delete_knowledge_node) stay
# in the unknown-tool bucket below: pages are created in an operations window, never by agents.
KB_READ_TOOLS = frozenset({"get_knowledge_base_tree", "search_knowledge_base", "get_knowledge_page"})
KB_READ_ENV_FLAG = "HC_KB_READ_TOOLS"


def kb_read_enabled() -> bool:
    """Default OFF. Only an explicit on-value (1/true/yes/on) opens the 3 knowledge read tools."""
    return os.environ.get(KB_READ_ENV_FLAG, "0").strip().lower() in {"1", "true", "yes", "on"}


# MCP lifecycle handshake - no tool arguments, no bank data; still gated by the
# principal+bank ACL checks that run before the method branch in decide().
HANDSHAKE_METHODS = frozenset({"initialize", "notifications/initialized", "ping"})

# Field in tools/call arguments that names a bank (client-controlled → must sanitize).
BANK_ARG_KEYS = ("bank", "bank_id", "bankId")
# Where we stamp the authenticated member identity for team-retain attribution.
ATTRIBUTION_KEY = "hc_member"


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    resolved_bank: Optional[str] = None
    tool: Optional[str] = None


def blocked_operation_reason(
    bank_policy: BankPolicy, operation: str
) -> Optional[str]:
    if operation in bank_policy.blocked_operations:
        return f"blocked-by-bank-policy:{operation}"
    return None


def _acl_gate(
    principal: Optional[Principal], requested_bank: Optional[str], tool: Optional[str]
) -> Optional[Decision]:
    """Token + bank ACL checks shared by both surfaces. None = passed."""
    if principal is None:
        return Decision(False, "unknown-or-missing-token", None, tool)
    # Root multi-bank surface (no bank in path) → enumeration risk, always deny.
    if not requested_bank:
        return Decision(False, "root-multibank-blocked", None, tool)
    if requested_bank not in principal.allowed_banks():
        return Decision(False, f"bank-not-in-acl:{requested_bank}", None, tool)
    return None


def owns_bank(principal: Principal, bank: Optional[str]) -> bool:
    """The ONE owner check (issue #473 a): a bank-mutating operation passes only on the caller's own
    personal bank. MCP destructive/corrective tools and the REST bank-root PUT all ask this."""
    return bool(bank) and bank == principal.personal


def _decide_tool(
    principal: Principal,
    requested_bank: str,
    tool: str,
    *,
    bank_policy: BankPolicy,
    kb_read_enabled: bool,
) -> Decision:
    """Operation verdict for an ACL-passed principal. ONE function for MCP tools/call and the
    REST surface (issue #458): REST resolves method+path to the same tool name first."""
    if tool in DESTRUCTIVE_TOOLS:
        # Destructive only on the caller's own personal bank.
        if not owns_bank(principal, requested_bank):
            return Decision(False, f"destructive-on-nonpersonal:{tool}", requested_bank, tool)
        blocked_reason = blocked_operation_reason(bank_policy, tool)
        if blocked_reason:
            return Decision(False, blocked_reason, requested_bank, tool)
        return Decision(True, f"destructive-own:{tool}", requested_bank, tool)

    if tool in CORRECTIVE_TOOLS:
        # Curation only on the caller's own personal bank.
        if not owns_bank(principal, requested_bank):
            return Decision(False, f"corrective-on-nonpersonal:{tool}", requested_bank, tool)
        return Decision(True, f"corrective-own:{tool}", requested_bank, tool)

    if tool in RETAIN_TOOLS or tool in READ_TOOLS or (kb_read_enabled and tool in KB_READ_TOOLS):
        return Decision(True, f"allow:{tool}", requested_bank, tool)

    # Unknown tool → deny-by-default (fail-closed; new tools are not auto-trusted).
    # Cross-bank admin (list_banks, create_bank) intentionally stays in this bucket.
    return Decision(False, f"tool-not-whitelisted:{tool}", requested_bank, tool)


def decide(
    principal: Optional[Principal],
    requested_bank: Optional[str],
    method: str,
    tool: Optional[str],
    *,
    bank_policy: BankPolicy,
    kb_read_enabled: bool = False,
) -> Decision:
    """Deny-by-default authorization. requested_bank is the URL-path bank.
    kb_read_enabled: the HC_KB_READ_TOOLS state (issue #302), passed in so decide() stays pure."""
    gate = _acl_gate(principal, requested_bank, tool)
    if gate is not None:
        return gate

    if method in HANDSHAKE_METHODS:
        return Decision(True, f"handshake:{method}", requested_bank, tool)
    if method == "tools/list":
        return Decision(True, "list", requested_bank, tool)
    if method != "tools/call":
        return Decision(False, f"method-not-allowed:{method}", requested_bank, tool)
    if not tool:
        return Decision(False, "missing-tool-name", requested_bank, tool)
    return _decide_tool(principal, requested_bank, tool,
                        bank_policy=bank_policy, kb_read_enabled=kb_read_enabled)


# --- REST surface (issue #458) ---------------------------------------------------------
# The REST API carries the operation in METHOD + PATH. Each row names the MCP tool whose
# verdict that REST call gets, so both surfaces are judged by the same _decide_tool() and the
# same tool sets above: add a REST row only by pointing it at a tool. Anything not in this
# table (path or method) is denied (deny-by-default), the knowledge base writes (#305)
# included. Path segments are matched one by one; "{id}" matches exactly one segment. The
# first matching row wins, so literal rows come before "{id}" rows of the same shape.
#
# DELETE memories/<id> rides invalidate_memory: retiring one fact is the corrective class
# (owner's personal bank only), and the owner's single-memory delete has always passed.
# GET/PATCH config ride get_bank/update_bank: the bank configuration is the REST face of the
# bank profile (read: any bank the principal may access; write: owner only, blocked list).
# PROVISION_BANK (PUT on the bank root) is the one REST-only operation: it creates or overwrites the
# bank profile, so it gets the owner check of the MCP bank-profile write (update_bank, issue #473 a):
# the caller's own personal bank only. A member can no longer overwrite a team bank's profile; team
# banks are provisioned by the operator path (verify/gate2_banks.sh: upstream inside the hindsight
# container, upstream key on stdin), which never passes the gateway. It is in no MCP tool set, so the
# MCP surface can never reach it.
PROVISION_BANK = "provision_bank"
PROVISION_BANK_BLOCK_AS = "update_bank"   # blocked_operations name the PUT is judged by (rework #473-1)
REST_OPERATIONS: Tuple[Tuple[str, str, str], ...] = (
    ("GET", "", "get_bank"),
    ("PUT", "", PROVISION_BANK),
    ("PATCH", "", "update_bank"),
    ("DELETE", "", "delete_bank"),
    ("GET", "config", "get_bank"),
    ("PATCH", "config", "update_bank"),
    ("GET", "stats", "get_bank_stats"),
    ("POST", "memories", "retain"),
    ("DELETE", "memories", "clear_memories"),
    ("POST", "memories/recall", "recall"),
    ("GET", "memories/list", "list_memories"),
    ("GET", "memories/{id}", "get_memory"),
    ("PATCH", "memories/{id}", "update_memory"),
    ("DELETE", "memories/{id}", "invalidate_memory"),
    ("POST", "reflect", "reflect"),
    ("GET", "documents", "list_documents"),
    ("GET", "documents/{id}", "get_document"),
    ("DELETE", "documents/{id}", "delete_document"),
    ("GET", "tags", "list_tags"),
    ("GET", "mental-models", "list_mental_models"),
    ("POST", "mental-models", "create_mental_model"),
    ("GET", "mental-models/{id}", "get_mental_model"),
    ("PATCH", "mental-models/{id}", "update_mental_model"),
    ("DELETE", "mental-models/{id}", "delete_mental_model"),
    ("POST", "mental-models/{id}/refresh", "refresh_mental_model"),
    ("POST", "mental-models/{id}/clear", "clear_mental_model"),
    ("GET", "directives", "list_directives"),
    ("POST", "directives", "create_directive"),
    ("DELETE", "directives/{id}", "delete_directive"),
    ("GET", "operations", "list_operations"),
    ("GET", "operations/{id}", "get_operation"),
    ("DELETE", "operations/{id}", "cancel_operation"),
    ("GET", "knowledge-base/tree", "get_knowledge_base_tree"),
    ("GET", "knowledge-base/search", "search_knowledge_base"),
    ("GET", "knowledge-base/pages/{id}", "get_knowledge_page"),
)
_REST_ID = "{id}"


def resolve_rest_operation(http_method: str, rest: str) -> Optional[str]:
    """The tool name a REST call is judged as, or None (not in REST_OPERATIONS → deny).
    `rest` is the canonical sub-path under /v1/<tenant>/banks/<bank> (no leading/trailing '/')."""
    method = (http_method or "").upper()
    segs = rest.split("/") if rest else []
    for row_method, pattern, tool in REST_OPERATIONS:
        if row_method != method:
            continue
        pat = pattern.split("/") if pattern else []
        if len(pat) == len(segs) and all(p == _REST_ID or p == s for p, s in zip(pat, segs)):
            return tool
    return None


def decide_rest(
    principal: Optional[Principal],
    requested_bank: Optional[str],
    http_method: str,
    rest: str,
    *,
    bank_policy: BankPolicy,
    kb_read_enabled: bool = False,
) -> Decision:
    """REST twin of decide(): same ACL gate, same _decide_tool() on the resolved tool name."""
    gate = _acl_gate(principal, requested_bank, None)
    if gate is not None:
        return gate
    tool = resolve_rest_operation(http_method, rest)
    if tool is None:
        return Decision(False, f"rest-op-not-whitelisted:{(http_method or '').upper()}",
                        requested_bank, None)
    if tool == PROVISION_BANK:
        if not owns_bank(principal, requested_bank):
            return Decision(False, f"provision-on-nonpersonal:{tool}", requested_bank, tool)
        # Rework #473-1: the bank's blocked_operations apply as to the other profile writes. The PUT
        # is judged by the MCP name of a profile write, update_bank (MCP update_bank and REST PATCH
        # on the root use it), so one policy entry closes every surface.
        blocked_reason = blocked_operation_reason(bank_policy, PROVISION_BANK_BLOCK_AS)
        if blocked_reason:
            return Decision(False, blocked_reason, requested_bank, tool)
        return Decision(True, f"provision-own:{tool}", requested_bank, tool)
    return _decide_tool(principal, requested_bank, tool,
                        bank_policy=bank_policy, kb_read_enabled=kb_read_enabled)


# --- Upstream write restriction (issue #473 b) --------------------------------------------
# upstream narrows a bank's MCP tools with its bank config `mcp_enabled_tools` (None = every tool,
# a list = allowlist), and enforces it on MCP tool calls only: hindsight_api/mcp_tools.py
# _apply_bank_tool_filtering. Its REST retain route (hindsight_api/api/http.py, POST
# /v1/default/banks/{bank_id}/memories) never reads the setting, so a bank whose retain is closed on
# MCP stayed writable over REST. The gateway judges REST writes by the SAME datum, read from upstream
# (GET .../config -> config.mcp_enabled_tools) on every request: no cache, and no gateway policy
# field, so the two surfaces can never disagree. Anything but a readable null or list of names is a
# failed lookup and denies (fail-closed).
MCP_ENABLED_TOOLS_KEY = "mcp_enabled_tools"
WRITE_RESTRICTION_DENY_PREFIX = "blocked-by-mcp-enabled-tools:"
BANK_CONFIG_LOOKUP_FAILED_PREFIX = "bank-config-lookup-failed:"


def write_restriction_reason(config_body: object, tool: str) -> Optional[str]:
    """Verdict of upstream's bank config on one write tool. None = allowed. Otherwise a deny reason:
    WRITE_RESTRICTION_DENY_PREFIX (the list leaves the tool out) or BANK_CONFIG_LOOKUP_FAILED_PREFIX
    (the body is not the config shape this function can judge). config_body is the parsed JSON of
    upstream's GET /v1/<tenant>/banks/<bank>/config ({bank_id, config:{...}, overrides:{...}})."""
    config = config_body.get("config") if isinstance(config_body, dict) else None
    if not isinstance(config, dict):
        return BANK_CONFIG_LOOKUP_FAILED_PREFIX + "no-config-object"
    if MCP_ENABLED_TOOLS_KEY not in config:
        # upstream always returns the resolved key (null when unset, fixtures/rest/bank_config.json):
        # a body without it is not a config this verdict can trust.
        return BANK_CONFIG_LOOKUP_FAILED_PREFIX + "no-" + MCP_ENABLED_TOOLS_KEY
    enabled = config[MCP_ENABLED_TOOLS_KEY]
    if enabled is None:
        return None
    if not isinstance(enabled, list) or not all(isinstance(t, str) for t in enabled):
        return BANK_CONFIG_LOOKUP_FAILED_PREFIX + "bad-" + MCP_ENABLED_TOOLS_KEY
    if tool not in enabled:
        return WRITE_RESTRICTION_DENY_PREFIX + tool
    return None


def sanitize_arguments(
    arguments: Optional[dict],
    *,
    resolved_bank: str,
    principal: Principal,
    tool: Optional[str],
    attribution_enabled: bool,
) -> dict:
    """Strip client bank hints; stamp team-retain attribution. Returns a NEW dict."""
    args = dict(arguments or {})
    # 1) Remove any client-supplied bank field (gateway pins bank via URL path).
    for k in BANK_ARG_KEYS:
        args.pop(k, None)
    # 2) Team-retain attribution: overwrite (never trust) member identity.
    if attribution_enabled and tool in RETAIN_TOOLS and principal.is_team_bank(resolved_bank):
        md = dict(args.get("metadata") or {})
        md[ATTRIBUTION_KEY] = principal.identity  # overwrite any client-provided value
        args["metadata"] = md
    return args


# Header allowlist: only these are forwarded upstream; everything else is dropped.
# Authorization is injected by the gateway (upstream key), never forwarded from client.
FORWARD_HEADER_ALLOWLIST = frozenset({"content-type", "accept", "mcp-session-id", "mcp-protocol-version"})
