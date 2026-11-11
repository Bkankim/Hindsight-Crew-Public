# Threat Model - Hindsight-Crew

## Posture: honest-but-curious

The gateway stops a colleague from **accidentally or curiously** reading another tenant's bank. It is **not** hardened against a determined malicious actor with host access. This is a deliberate, documented v1 scope - stated honestly because a security reviewer distinguishes "RBAC" from "verifiable isolation."

## What is enforced (the single enforcement point)

Hindsight's built-in auth is a **single shared Bearer key** and resolves the target bank from client-controlled inputs (URL path > `X-Bank-Id` header > env, and - per `contract-probe` - possibly the body `arguments.bank`). It has **no identity→bank enforcement**. Therefore the gateway is the *only* place isolation is enforced. It:

1. Holds the upstream Hindsight key; **clients never receive it**.
2. Resolves the bank **only** from the token→ACL mapping; ignores/strips/overwrites client URL bank, `X-Bank-Id`, and body `arguments.bank`.
3. Denies by default: a call passes only if `(method, tool_name, target_bank)` matches the token's ACL; everything else is `403`.
4. Blocks the root multi-bank `/mcp/` (enumeration) and restricts destructive and corrective ops to the owner's personal bank on BOTH surfaces with ONE verdict function: MCP destructive tools (`delete_bank`/`clear_memories`/`delete_document`/`update_bank`) and corrective tools (mental model / directive / memory curation) are denied on shared team banks, and REST resolves METHOD + path to the same tool name first, so team-bank REST admin POSTs (e.g. `mental-models/<id>/clear`) are denied the same way. Members may still retain/recall on team banks. The REST surface (issue #458, #473) adds:
   - **REST deny-by-default.**
     A REST path or method not in the operation table is `403` and never reaches upstream, knowledge base writes included.
     Code: `gateway/policy.py` `REST_OPERATIONS`, `resolve_rest_operation`, `decide_rest`, called from `rest_proxy` in `gateway/app.py`.
   - **Bank-root `PUT` is owner-only.**
     `PUT` on the bank root (bank provisioning, the one REST-only operation) passes only on the caller's own personal bank; on a team bank it is `403`.
     It is also judged as `update_bank` against the bank's `blocked_operations`, so one policy entry closes every bank-profile write on both surfaces.
     Team banks are provisioned by the operator path (`verify/gate2_banks.sh`), which does not pass the gateway.
     Code: `gateway/policy.py` `owns_bank`, `decide_rest` (`PROVISION_BANK`), `gateway/bank_policies.py` `_BLOCKABLE_OPERATIONS`.
   - **Upstream write restriction on REST retain.**
     Upstream applies a bank's `mcp_enabled_tools` config to MCP tool calls only.
     For a REST `POST memories` the gateway reads that config from upstream on every request (no cache) and answers `403` when the list leaves retain out.
     A failed or unreadable config lookup (timeout, non-200, missing bank, unexpected shape) is `503` and nothing is forwarded (fail-closed).
     Code: `gateway/app.py` `_rest_write_restriction`, `gateway/policy.py` `write_restriction_reason`.
   - **REST recall floor.**
     REST `POST memories/recall` gets the same `recall_max_tokens_floor` rule (same rule id `RULE:recall-max-tokens-floor`) as MCP, rejected whole with `422` and never forwarded.
     Code: `gateway/rules.py` `check_recall_budget`, called from `rest_proxy` in `gateway/app.py`.
   - **`hs_` key scrubbing.**
     The shared secret scrubber also redacts this project's upstream key shape (`hs_` + 16 or more hex characters) in context-injection snippets, contradiction warnings and the recurrence ledger.
     Code: `gateway/context_triggers.py` `_SECRET_SHAPES`, `scrub_secrets` (used by `gateway/contradiction.py` and `gateway/recurrence.py`).
5. Overwrites the team-retain attribution field with the token's identity (members cannot forge attribution).

## Residual risks (accepted in v1)

- **Host-local `8888` bypass.** "Single enforcement point" holds **only at the network-exposure boundary.** Hindsight runs on the internal Docker network and is not host-published; but any process/user on the host that can reach the internal network or the upstream key bypasses the gateway. Accepted under honest-but-curious.
- **No transport encryption between client and gateway** beyond the deployment's own network controls.
- **Audit log is append-only at the application layer**, not tamper-proof storage.
- **Host-local plaintext rerank channel (added 2026-07-26).** As of the reranker sidecar transition, recall candidate bodies (sidecar-side cap `RERANK_MAX_TEXTS=256` documents per query; the client, i.e. `hindsight`, batches at most 128 per request - the sidecar cap is deliberately kept at 2x the client batch ceiling) now leave the container on every query and travel over plaintext HTTP to the host at `127.0.0.1:8787` (macOS sidecar, `rerank-server/`). This moves the boundary stated above ("Hindsight runs on the internal Docker network and is not host-published") - candidate text now crosses that boundary in the clear on the host loopback interface.
- **Unauthenticated endpoint - rationale.** The sidecar has no auth. This is accepted because it binds loopback-only and is unreachable from the LAN (measured AC-B2: container reaches it with `200`, the host's `en0` interface `192.0.2.10` (documentation-range placeholder for the host's LAN address) is refused). This puts it in the same threat tier as the existing host-local `8888` gateway. The startup guard itself refuses non-loopback binds (`0.0.0.0` or any non-loopback address).
- **Local DoS.** Any local process running as the same OS user can flood `127.0.0.1:8787` and consume the host GPU (the sidecar serializes on a single worker, so this degrades recall latency for everyone). Mitigations: `MAX_QUEUE_DEPTH=3`, `RERANK_MAX_TEXTS=256`, and a `RERANK_TOTAL_DEADLINE_SECONDS=7.5` sidecar deadline bound the resource envelope. These are not deployment-only conventions - they are the **code defaults** in `rerank-server/server.py`'s `Settings` dataclass, and the committed LaunchAgent template (`rerank-server/launchd/*.plist.example`) pins the same values in `EnvironmentVariables`, so template and code cannot drift silently (35 sidecar tests GREEN against these defaults). The watchdog monitors `device`/`degraded` status. A host `pf` rule can further restrict which local users/processes may reach `8787` if needed.
- **No-payload-logging invariant.** The sidecar never writes query/candidate text to logs, error responses, or disk - structured logs record only candidate count, character count, batch size, latency, device, and error code; validation failures return an opaque `422` with no payload echo. Measured: zero Korean-language (i.e. payload) bytes across the full sidecar log corpus. This invariant is the primary defense for the new host-local channel above, not a secondary control.

## Upgrade path → malicious-resistant (phase 2)

- mTLS or per-bank upstream keys (removes the single-shared-key blast radius).
- Rate-limiting + anomaly alerts on the gateway.
- Network policy so only the gateway can reach Hindsight, plus host-level isolation of the upstream key.
- Tamper-evident audit sink (append-only external store).
