"""Adversarial retain-effect integration tests, with tmp files and a mock upstream only."""
import asyncio
import json
import time

import httpx
import pytest

from conftest import bank_config_reply
from gateway import gate_effects, recurrence
from gateway.context_triggers import TriggerTable
from test_retain_gates import (
    BANK, GOTCHA_NEW, GOTCHA_OLD, RESCUE_QUERY, Notifier, Upstream,
    audit_lines, make, mem, retain_args, sse,
)


CORRECTION = "[정정] previous diagnosis was wrong; shared projectx operation now works"


@pytest.mark.parametrize("synthetic_secret", [
    "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
    "mt_0123456789abcdefghijklmno",
    "ghp_0123456789abcdefghijklmnop",
    "sk-0123456789abcdefghijklmnop",
    "eyJabcdefghijklmnopqrstuv.abcdefghijk.signature",
    "otpauth://totp/example?secret=JBSWY3DPEHPK3PXP",
])
def test_secrets_never_reach_pending_keywords_or_discord(tmp_path, synthetic_secret):
    old = mem("old-1", GOTCHA_OLD + " " + synthetic_secret, 0.99)
    up = Upstream(gate_results=[old], mcp_recall_results=[old])
    notifier = Notifier()
    client, _ = make(up, tmp_path, notifier)
    with client:
        rpc(client, "recall", {"query": RESCUE_QUERY})
        rpc(client, "retain", retain_args(GOTCHA_NEW + " " + synthetic_secret))
    draft, = pending(tmp_path)
    assert draft["state"] == "ready-for-review"
    all_output = json.dumps(draft) + recurrence.discord_message(notifier.drafts[0])
    assert synthetic_secret.lower() not in all_output.lower()
    keywords = draft["trigger"]["all_of"][0]
    assert not any(kw.lower() in synthetic_secret.lower() for kw in keywords)


def pending(tmp_path):
    path = tmp_path / "pending.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def rpc(client, name, args, *, session=None, el_id=1, batch=None):
    body = batch if batch is not None else {
        "jsonrpc": "2.0", "id": el_id, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    headers = {"authorization": "Bearer tok-alice", "content-type": "application/json"}
    if session is not None:
        headers["mcp-session-id"] = session
    return client.post(f"/mcp/{BANK}/", json=body, headers=headers)


def rest_retain(client, content=GOTCHA_NEW):
    return client.post(f"/v1/default/banks/{BANK}/memories",
                       json={"items": [retain_args(content)]},
                       headers={"authorization": "Bearer tok-alice"})


def result_element(el_id, payload, *, is_error=False):
    return {"jsonrpc": "2.0", "id": el_id, "result": {
        "isError": is_error, "structuredContent": payload,
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }}


class ScenarioUpstream(Upstream):
    def __init__(self, *, mode="success", failed_recall=False, operations=None, **kwargs):
        super().__init__(**kwargs)
        self.mode = mode
        self.failed_recall = failed_recall
        self.operations = list(operations or [])
        self.operation_reads = 0
        self.before_operation = None
        self.batch_modes = {}

    def element(self, el_id, mode):
        if mode == "jsonrpc-error":
            return {"jsonrpc": "2.0", "id": el_id,
                    "error": {"code": -32000, "message": "synthetic rejection"}}
        if mode == "isError":
            return result_element(el_id, {"status": "failed"}, is_error=True)
        if mode == "failure-body":
            return {"jsonrpc": "2.0", "id": el_id, "result": {
                "isError": False,
                "content": [{"type": "text", "text": "Error: synthetic failure"}]}}
        if mode == "structured-failure":
            return result_element(el_id, {"success": False, "status": "failed"})
        if mode == "accepted":
            return result_element(el_id, {"status": "accepted", "operation_id": "operation-1"})
        return result_element(el_id, {"status": "accepted", "memory_ids": ["new-1"]})

    def handler(self, request):
        if (config := bank_config_reply(request)) is not None:   # #473 b upstream config read
            return config
        body = json.loads(request.content or b"{}")
        if request.method == "GET" and "/operations/" in request.url.path:
            self.operation_reads += 1
            if self.before_operation:
                self.before_operation(self.operation_reads)
            response = self.operations[min(self.operation_reads - 1, len(self.operations) - 1)]
            return httpx.Response(200, json=response)
        if self.failed_recall and request.url.path.endswith("/memories/recall") \
                and body.get("query") == RESCUE_QUERY:
            return httpx.Response(503, json={"error": "synthetic recall failure"})
        if self.failed_recall and request.url.path == "/mcp" and isinstance(body, dict) \
                and body.get("params", {}).get("name") == "recall":
            return httpx.Response(200, json=self.element(body["id"], "isError"))
        if request.url.path == "/mcp" and isinstance(body, list):
            frames = [self.element(el["id"], self.batch_modes.get(el["id"], self.mode))
                      for el in body]
            return httpx.Response(200, content=sse(frames),
                                  headers={"content-type": "text/event-stream"})
        if request.url.path == "/mcp" and body.get("params", {}).get("name") == "retain":
            if self.mode == "http503":
                return httpx.Response(503, json={"error": "synthetic upstream failure"})
            return httpx.Response(200, content=sse(self.element(body["id"], self.mode)),
                                  headers={"content-type": "text/event-stream"})
        if request.url.path.endswith("/memories") and self.mode != "success":
            if self.mode == "http503":
                return httpx.Response(503, json={"error": "synthetic upstream failure"})
            if self.mode == "accepted":
                return httpx.Response(202, json={"status": "accepted", "operation_id": "operation-1"})
            if self.mode == "error":
                return httpx.Response(200, json={"error": "synthetic upstream failure"})
            return httpx.Response(200, json={"success": False, "status": "failed"})
        return super().handler(request)


@pytest.mark.parametrize("transport", ["mcp", "rest"])
def test_failed_recall_never_becomes_correction_evidence(tmp_path, transport):
    old = mem("old-1", GOTCHA_OLD, 0.99)
    up = ScenarioUpstream(failed_recall=True, gate_results=[old], mcp_recall_results=[old])
    notifier = Notifier()
    client, log = make(up, tmp_path, notifier)
    with client:
        if transport == "mcp":
            rpc(client, "recall", {"query": RESCUE_QUERY})
        else:
            client.post(f"/v1/default/banks/{BANK}/memories/recall",
                        json={"query": RESCUE_QUERY},
                        headers={"authorization": "Bearer tok-alice"})
        rpc(client, "retain", retain_args(CORRECTION, tags=("kind:decision",)))
    assert pending(tmp_path) == [] and notifier.drafts == []
    assert not (tmp_path / "recall-queries.log").exists()
    assert any(e["reason"] == "skip:no-verified-recall" for e in audit_lines(log, "recurrence"))


def test_unrelated_low_score_correction_does_not_draft_even_after_real_recall(tmp_path):
    old = mem("old-1", GOTCHA_OLD, 0.01)
    up = ScenarioUpstream(gate_results=[old], mcp_recall_results=[old])
    notifier = Notifier()
    client, log = make(up, tmp_path, notifier)
    with client:
        rpc(client, "recall", {"query": RESCUE_QUERY})
        rpc(client, "retain", retain_args(CORRECTION, tags=("kind:decision",)))
    assert pending(tmp_path) == [] and notifier.drafts == []
    assert any(e["reason"] == "skip:no-related-memory" for e in audit_lines(log, "recurrence"))


def test_gotcha_without_verified_recall_is_only_a_needs_evidence_candidate(tmp_path):
    up = ScenarioUpstream(gate_results=[mem("old-1", GOTCHA_OLD, 0.99)])
    notifier = Notifier()
    client, _ = make(up, tmp_path, notifier)
    with client:
        rpc(client, "retain", retain_args(GOTCHA_NEW))
    drafts = pending(tmp_path)
    assert len(drafts) == 1 and len(notifier.drafts) == 1
    assert drafts[0]["state"] == "needs-evidence" and drafts[0]["trigger"] is None
    assert drafts[0]["evidence"]["rescue_query_source"] == "needs-evidence"


def test_mcp_session_mismatch_rejected_then_same_session_succeeds(tmp_path):
    old = mem("old-1", GOTCHA_OLD, 0.99)
    up = ScenarioUpstream(gate_results=[old], mcp_recall_results=[old])
    notifier = Notifier()
    client, _ = make(up, tmp_path, notifier)
    with client:
        rpc(client, "recall", {"query": RESCUE_QUERY}, session="session-a")
        rpc(client, "retain", retain_args(CORRECTION, tags=("kind:decision",)), session="session-b")
        assert pending(tmp_path) == [] and notifier.drafts == []
        rpc(client, "retain", retain_args(CORRECTION, tags=("kind:decision",)), session="session-a")
    drafts = pending(tmp_path)
    assert len(drafts) == 1 and drafts[0]["state"] == "ready-for-review"
    assert drafts[0]["evidence"]["recall_scope"] == "session"
    assert drafts[0]["trigger"]["recall"]["query"] == RESCUE_QUERY


@pytest.mark.parametrize("transport,mode", [
    ("mcp", "http503"), ("mcp", "isError"), ("mcp", "jsonrpc-error"),
    ("mcp", "failure-body"), ("mcp", "structured-failure"),
    ("rest", "http503"), ("rest", "error"), ("rest", "structured-failure"),
])
def test_failed_retain_has_no_effects_and_does_not_consume_retry(tmp_path, transport, mode):
    up = ScenarioUpstream(mode=mode, gate_results=[mem("old-1", GOTCHA_OLD, 0.99)])
    notifier = Notifier()
    client, _ = make(up, tmp_path, notifier)
    with client:
        call = (lambda: rpc(client, "retain", retain_args(GOTCHA_NEW))) \
            if transport == "mcp" else lambda: rest_retain(client)
        call()
        assert pending(tmp_path) == [] and notifier.drafts == []
        up.mode = "success"
        call()
    assert len(pending(tmp_path)) == 1 and len(notifier.drafts) == 1


@pytest.mark.parametrize("transport", ["mcp", "rest"])
@pytest.mark.parametrize("terminal,expected", [("completed", 1), ("failed", 0)])
def test_async_retain_waits_for_completed_operation(tmp_path, monkeypatch, transport, terminal, expected):
    monkeypatch.setattr(gate_effects, "OPERATION_POLL_SECONDS", 0)
    monkeypatch.setattr(gate_effects, "OPERATION_WAIT_SECONDS", 1)
    up = ScenarioUpstream(mode="accepted", gate_results=[mem("old-1", GOTCHA_OLD, 0.99)],
                          operations=[{"status": "running"}, {"status": terminal}])
    notifier = Notifier()
    client, _ = make(up, tmp_path, notifier)
    preterminal_counts = []
    up.before_operation = lambda count: preterminal_counts.append(
        (count, len(pending(tmp_path)), len(notifier.drafts)))
    with client:
        if transport == "mcp":
            rpc(client, "retain", retain_args(GOTCHA_NEW))
        else:
            rest_retain(client)
    assert preterminal_counts == [(1, 0, 0), (2, 0, 0)]
    assert len(pending(tmp_path)) == expected and len(notifier.drafts) == expected


@pytest.mark.parametrize("operation", [
    {"status": "completed", "result_metadata": {"extraction_errors_count": 1}},
    {"status": "completed", "result_metadata": {"unit_ids_count": 0}},
    {"status": "completed", "child_operations": [{"status": "failed"}]},
])
def test_completed_operation_with_failed_extraction_does_not_publish(tmp_path, monkeypatch, operation):
    monkeypatch.setattr(gate_effects, "OPERATION_POLL_SECONDS", 0)
    monkeypatch.setattr(gate_effects, "OPERATION_WAIT_SECONDS", 1)
    up = ScenarioUpstream(mode="accepted", gate_results=[mem("old-1", GOTCHA_OLD, 0.99)],
                          operations=[operation])
    notifier = Notifier()
    client, _ = make(up, tmp_path, notifier)
    with client:
        rpc(client, "retain", retain_args(GOTCHA_NEW))
    assert up.operation_reads == 1
    assert pending(tmp_path) == [] and notifier.drafts == []


@pytest.mark.parametrize("transport", ["mcp", "rest"])
def test_completed_operation_with_null_children_publishes(tmp_path, monkeypatch, transport):
    # FastAPI OperationStatusResponse emits child_operations:null for a leaf retain.
    monkeypatch.setattr(gate_effects, "OPERATION_POLL_SECONDS", 0)
    monkeypatch.setattr(gate_effects, "OPERATION_WAIT_SECONDS", 0.05)
    up = ScenarioUpstream(mode="accepted", gate_results=[mem("old-1", GOTCHA_OLD, 0.99)],
                          operations=[{"status": "completed", "error_message": None,
                                       "result_metadata": {"unit_ids_count": 1},
                                       "child_operations": None}])
    notifier = Notifier()
    client, _ = make(up, tmp_path, notifier)
    with client:
        if transport == "mcp":
            rpc(client, "retain", retain_args(GOTCHA_NEW))
        else:
            rest_retain(client)
    assert up.operation_reads == 1
    assert len(pending(tmp_path)) == 1 and len(notifier.drafts) == 1


@pytest.mark.parametrize("filters", [{"min_scores": {"reranker": 0.95}},
                                      {"types": ["world"]},
                                      {"tag_groups": [{"tags": ["project:test"]}]}])
def test_unrepresentable_recall_filters_do_not_become_trigger_queries(tmp_path, filters):
    old = mem("old-1", GOTCHA_OLD, 0.99)
    up = ScenarioUpstream(gate_results=[old], mcp_recall_results=[old])
    notifier = Notifier()
    client, _ = make(up, tmp_path, notifier)
    with client:
        rpc(client, "recall", {"query": RESCUE_QUERY, **filters})
        rpc(client, "retain", retain_args(GOTCHA_NEW))
    draft, = pending(tmp_path)
    assert draft["state"] == "needs-evidence" and draft["trigger"] is None


def test_mixed_mcp_batch_commits_only_successful_element(tmp_path):
    up = ScenarioUpstream(gate_results=[mem("old-1", GOTCHA_OLD, 0.99)])
    up.batch_modes = {11: "isError", 12: "success"}
    notifier = Notifier()
    client, _ = make(up, tmp_path, notifier)
    batch = [{"jsonrpc": "2.0", "id": el_id, "method": "tools/call",
              "params": {"name": "retain", "arguments": retain_args(GOTCHA_NEW + suffix)}}
             for el_id, suffix in [(11, " rejected item"), (12, " accepted item")]]
    with client:
        rpc(client, "retain", {}, batch=batch)
    drafts = pending(tmp_path)
    assert len(drafts) == 1 and len(notifier.drafts) == 1
    assert drafts[0]["evidence"]["new_content_preview"].endswith("accepted item")


def test_pending_write_failure_releases_dedup_for_retry(tmp_path, monkeypatch):
    up = ScenarioUpstream(gate_results=[mem("old-1", GOTCHA_OLD, 0.99)])
    notifier = Notifier()
    client, _ = make(up, tmp_path, notifier)
    writer = client.app.state.pending_triggers
    actual_append = writer.append
    attempts = []

    def flaky_append(draft):
        attempts.append(draft)
        return False if len(attempts) == 1 else actual_append(draft)

    monkeypatch.setattr(writer, "append", flaky_append)
    with client:
        rpc(client, "retain", retain_args(GOTCHA_NEW))
        assert pending(tmp_path) == [] and notifier.drafts == []
        rpc(client, "retain", retain_args(GOTCHA_NEW))
    assert len(attempts) == 2
    assert len(pending(tmp_path)) == 1 and len(notifier.drafts) == 1


def test_response_body_is_sent_before_notifier_starts(tmp_path):
    up = ScenarioUpstream(gate_results=[mem("old-1", GOTCHA_OLD, 0.99)])
    events = []

    async def notifier(draft):
        events.append("notify-start")
        await asyncio.sleep(0.01)
        events.append("notify-end")
        return "ok"

    client, _ = make(up, tmp_path, notifier)
    request_body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "retain", "arguments": retain_args(GOTCHA_NEW)}}).encode()

    async def run_request():
        async def receive():
            return {"type": "http.request", "body": request_body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                events.append("response-complete")

        await client.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                          "method": "POST", "scheme": "http", "path": f"/mcp/{BANK}/",
                          "raw_path": f"/mcp/{BANK}/".encode(), "query_string": b"",
                          "root_path": "", "server": ("test", 80), "client": ("test", 1),
                          "headers": [(b"authorization", b"Bearer tok-alice"),
                                      (b"content-type", b"application/json")]}, receive, send)
        await client.app.state.http_client.aclose()

    asyncio.run(run_request())
    assert events == ["response-complete", "notify-start", "notify-end"]


@pytest.mark.parametrize("scenario,expected", [
    ("served-success", 1), ("not-served", 0), ("low-score", 0),
    ("different-id", 0), ("failed-retain", 0), ("not-correction", 0),
    ("async-failed", 0), ("async-completed", 1),
])
def test_injection_correction_requires_served_id_relatedness_and_success(tmp_path, monkeypatch,
                                                                       scenario, expected):
    old = mem("old-1", GOTCHA_OLD, 0.99)
    up = ScenarioUpstream(gate_results=[old])
    client, log = make(up, tmp_path, Notifier())
    client.app.state.trigger_table = TriggerTable.from_dict({"version": 1, "triggers": [{
        "id": "test-trigger", "bank": BANK, "tools": ["aside"],
        "all_of": [["projectx"]], "recall": {"query": "synthetic projectx rescue query"},
    }]})
    with client:
        if scenario != "not-served":
            served = client.post(f"/context-triggers/{BANK}?format=json",
                                 json={"text": "projectx operation", "tool": "aside"},
                                 headers={"authorization": "Bearer tok-alice"})
            assert served.json()["memory_ids"] == ["old-1"]
        if scenario == "low-score":
            up.gate_results = [mem("old-1", GOTCHA_OLD, 0.01)]
        elif scenario == "different-id":
            up.gate_results = [mem("other-id", GOTCHA_OLD, 0.99)]
        elif scenario == "failed-retain":
            up.mode = "isError"
        elif scenario.startswith("async-"):
            monkeypatch.setattr(gate_effects, "OPERATION_POLL_SECONDS", 0)
            monkeypatch.setattr(gate_effects, "OPERATION_WAIT_SECONDS", 1)
            up.mode = "accepted"
            up.operations = [{"status": "running"}, {"status": scenario.split("-", 1)[1]}]
        content = "[완료] unrelated successful update" if scenario == "not-correction" else CORRECTION
        rpc(client, "retain", retain_args(content, tags=("kind:decision",)))
    events = audit_lines(log, "injection-correction")
    assert len(events) == expected
    if expected:
        assert events[0]["injection_correction"]["memory_ids"] == ["old-1"]
        assert events[0]["injection_correction"]["trigger_ids"] == ["test-trigger"]
        assert events[0]["injection_correction"]["evidence_method"] == "recent-served-id+reranker"


# --- review P3 (2026-09-23): operation wait semantics, task cap, unhashable JSON-RPC ids -------

def _wait(seq, budget=2.0):
    reads = []

    def handler(request):
        i = min(len(reads), len(seq) - 1)
        reads.append(i)
        status, body = seq[i] if isinstance(seq[i], tuple) else (200, seq[i])
        return httpx.Response(status, json=body)

    async def go():
        async with httpx.AsyncClient(base_url="http://m", transport=httpx.MockTransport(handler)) as c:
            return await gate_effects.wait_for_storage(c, "", "b", "op", deadline=time.monotonic() + budget)
    return asyncio.run(go()), len(reads)


def test_completed_after_retry_counts_as_stored_despite_stale_error_message(monkeypatch):
    monkeypatch.setattr(gate_effects, "OPERATION_POLL_SECONDS", 0.01)
    ok, reads = _wait([{"status": "pending", "error_message": "RateLimitError 429", "retry_count": 1},
                       {"status": "completed", "error_message": "RateLimitError 429", "retry_count": 1,
                        "result_metadata": {"unit_ids_count": 2, "extraction_errors_count": 0}}])
    assert ok is True and reads == 2


@pytest.mark.parametrize("row", [
    {"status": "not_found"},
    (404, {"detail": "operation not found"}),
    {"status": "completed", "result_metadata": "x"},
    {"status": "completed", "result_metadata": {}, "child_operations": ["x"]},
])
def test_unknown_or_malformed_operation_is_decided_on_first_read(monkeypatch, row):
    monkeypatch.setattr(gate_effects, "OPERATION_POLL_SECONDS", 0.01)
    ok, reads = _wait([row], budget=1.0)
    assert ok is False and reads == 1


def test_effect_tasks_over_global_cap_are_skipped_and_audited(tmp_path, monkeypatch):
    up = Upstream(gate_results=[mem("old-1", GOTCHA_OLD, 0.99)])
    client, log = make(up, tmp_path, Notifier())
    monkeypatch.setattr(gate_effects, "_active", gate_effects.MAX_ACTIVE_FINALIZE)
    from test_retain_gates import mcp
    with client:
        assert mcp(client, "retain", retain_args(GOTCHA_NEW)).status_code == 200
    assert [l["reason"] for l in audit_lines(log, recurrence.DECISION)] == ["skip:effects-backlog"]


@pytest.mark.parametrize("name,arguments", [
    ("retain", retain_args(GOTCHA_NEW)),                                   # gate-eligible
    ("retain", retain_args("[완료] plain", tags=("kind:completion",))),    # not eligible
    ("recall", {"query": RESCUE_QUERY}),
])
def test_unhashable_jsonrpc_id_is_forwarded_and_never_500s(tmp_path, name, arguments):
    up = Upstream(gate_results=[mem("old-1", GOTCHA_OLD, 0.99)], mcp_recall_results=[])
    client, log = make(up, tmp_path, Notifier())
    body = {"jsonrpc": "2.0", "id": [3], "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    with client:
        resp = client.post(f"/mcp/{BANK}/", content=json.dumps(body),
                           headers={"content-type": "application/json", "authorization": "Bearer tok-alice"})
    assert resp.status_code == 200
    assert [c["path"] for c in up.calls] == ["/mcp"]      # forwarded once, no gate recall


def test_float_and_bool_ids_are_no_id_like_upstream():
    """Upstream RequestId is int | str: 1.0 / True validate as notifications and never run."""
    from gateway import reframe
    assert reframe.element_id({"id": 1.0}) is None and reframe.element_id({"id": True}) is None
    assert reframe.element_id({"id": 1}) == 1 and reframe.element_id({"id": "a"}) == "a"


def test_recall_prune_skips_unhashable_id_instead_of_raising():
    from gateway import recall_prune
    el = {"jsonrpc": "2.0", "id": [3], "result": {"content": []}}
    assert recall_prune.prune_mcp_element(el, {2}) is el
