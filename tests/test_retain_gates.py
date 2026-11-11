"""Retain-side gates (issue #12): 3차 모순 게이트 + 2차 재발 탐지·초안.

Contracts pinned here:
  1. A negative-label retain ([차단]/[불가]/[막힘]) triggers ONE upstream recall with the retain
     content; a positive precedent (kind:completion tag or 완료/성공 label) at reranker >=
     threshold is appended to the SUCCESS response as a "[gateway 경고] 반대 결론의 전례 있음"
     block quoting the precedent. The retain is still forwarded (warn, never reject).
  2. Below threshold / no positive precedent -> no warning, audit "no-precedent".
     Non-eligible retains ([완료], no gotcha tag) cause NO gate recall at all.
  3. REST POST .../memories discloses the same warning as gateway_warnings[{index,text}].
  4. Recurrence: a kind:gotcha retain highly similar to an existing gotcha writes a pending
     trigger draft whose recall QUERY is the recall that surfaced that memory (read from the
     gateway's recall ledger) and notifies once (dedup within TTL).
  5. Correction-after-recall: a [정정] retain within the window of a recall drafts a row;
     without any recall in the window it is skipped.
  6. Audit lines carry ids/scores/decisions only - never retain text or memory text.
  7. (review D2, 2026-09-21) Upstream double-encodes recall results - structuredContent.result
     and content[0].text are JSON *strings*. The recall ledger must still get the result ids.
  8. (review D3) Every app a test builds writes its ledger/pending files under tmp_path, never
     the repo's live audit/ files.
"""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, UPSTREAM_KEY, bank_config_reply
from gateway import recurrence
from gateway.acl import ACL
from gateway.audit import Audit
from gateway.app import create_app

BANK = "personal-alice"
BLOCKED = "[차단] 데모 사이트 CSS 엣지 캐시 퍼지 불가 - 도구 A가 원격 콘솔 로그인의 2단계 화면을 넘지 못한다 RETAIN-SECRET"
PRECEDENT = "[정정+완료] 데모 사이트 CSS 엣지 캐시 퍼지 성공 - 2단계 코드는 호출자가 만들고 코드만 도구 A에 PRECEDENT-SECRET"
GOTCHA_NEW = "[발견] aside 격리세션에서 파일 접근 범위가 좁아져 2단계 코드를 만들 수 없다"
GOTCHA_OLD = "[발견] Aside 격리세션 파일 접근 정책이 좁아져 코드 생성이 불가 - 호출자가 대신 만든다"
RESCUE_QUERY = "도구 A 2단계 코드 분담 구조"


def mem(mid, text, rr, tags=("kind:gotcha",), occurred="2026-09-21T08:55:48+00:00"):
    return {"id": mid, "text": text, "tags": list(tags), "occurred_start": occurred,
            "scores": {"reranker": rr, "final": rr}}


def sse(payload):
    return b"event: message\r\ndata: " + json.dumps(payload).encode("utf-8") + b"\r\n\r\n"


def _approx_cl100k(text):
    return sum(1.15 if ord(ch) > 127 else 0.3 for ch in text)


class Upstream:
    """/mcp answers a retain success (or a recall result); REST recall answers gate_results."""

    def __init__(self, gate_results=None, mcp_recall_results=None):
        self.gate_results = gate_results or []
        self.mcp_recall_results = mcp_recall_results or []
        self.calls = []

    def handler(self, request):
        if (config := bank_config_reply(request)) is not None:   # #473 b upstream config read
            return config
        body = json.loads(request.content or b"{}")
        self.calls.append({"path": request.url.path, "body": body})
        if request.url.path.endswith("/memories/recall"):
            # Real upstream (hindsight_api/api/http.py) 400s a query above 500 cl100k tokens.
            # Approximation calibrated on measured counts (700 Korean-heavy chars -> 804).
            if _approx_cl100k(body.get("query", "")) > 500:
                return httpx.Response(400, json={"detail": "Query too long: exceeds maximum of 500"})
            return httpx.Response(200, json={"results": self.gate_results},
                                  headers={"content-type": "application/json"})
        if request.url.path == "/mcp":
            name = (body.get("params") or {}).get("name")
            if name == "recall":
                # Real v0.8.3 shape (captured 2026-09-21): BOTH carriers are JSON strings.
                encoded = json.dumps({"results": self.mcp_recall_results}, indent=2)
                result = {"content": [{"type": "text", "text": encoded}],
                          "structuredContent": {"result": encoded}, "isError": False}
            else:
                result = {"content": [{"type": "text", "text": json.dumps({"status": "accepted", "memory_ids": ["new-1"]})}],
                          "structuredContent": {"status": "accepted", "memory_ids": ["new-1"]}, "isError": False}
            return httpx.Response(200, content=sse({"jsonrpc": "2.0", "id": body.get("id"), "result": result}),
                                  headers={"content-type": "text/event-stream", "mcp-session-id": "s1"})
        if request.url.path.endswith("/memories"):
            return httpx.Response(200, json={"success": True, "items_count": 1},
                                  headers={"content-type": "application/json"})
        return httpx.Response(404)


class Notifier:
    def __init__(self):
        self.drafts = []

    async def __call__(self, draft):
        self.drafts.append(draft)
        return "ok"


def make(upstream, tmp_path, notifier=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler),
                             base_url="http://upstream", timeout=5.0)
    log = tmp_path / "audit.log"
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(log)),
                     upstream_key=UPSTREAM_KEY, http_client=http,
                     recall_ledger=recurrence.RecallLedger(str(tmp_path / "recall-queries.log")),
                     pending_triggers=recurrence.PendingTriggers(str(tmp_path / "pending.jsonl")),
                     notifier=notifier)
    return TestClient(app), log


def mcp(client, name, arguments, el_id=1):
    body = {"jsonrpc": "2.0", "id": el_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    return client.post(f"/mcp/{BANK}/", content=json.dumps(body),
                       headers={"content-type": "application/json", "authorization": "Bearer tok-alice"})


def retain_args(content, tags=("kind:gotcha", "project:x")):
    return {"content": content, "tags": list(tags), "timestamp": "2026-09-21T10:00:00Z"}


def mcp_texts(resp):
    data = [ln[5:].strip() for ln in resp.text.splitlines() if ln.startswith("data:")][-1]
    return [b["text"] for b in json.loads(data)["result"]["content"] if b["type"] == "text"]


def audit_lines(log, decision):
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
            if json.loads(l)["decision"] == decision]


# --- 3차: contradiction ---------------------------------------------------------------

def test_blocked_retain_with_positive_precedent_is_stored_and_warned(tmp_path):
    up = Upstream(gate_results=[mem("22aebc9e-aaaa", PRECEDENT, 0.957),
                                mem("f435a42c-bbbb", "[함정] 다른 콘솔 함정", 0.86)])
    client, log = make(up, tmp_path)
    resp = mcp(client, "retain", retain_args(BLOCKED, tags=("kind:gotcha", "project:x")))
    assert resp.status_code == 200
    texts = mcp_texts(resp)
    assert json.loads(texts[0])["status"] == "accepted"           # stored: forwarded, not rejected
    warning = [t for t in texts if t.startswith("[gateway 경고] 반대 결론의 전례 있음")]
    assert len(warning) == 1
    assert "id:22aebc9e" in warning[0] and "PRECEDENT-SECRET" in warning[0] and "실제로 시도했는가" in warning[0]
    assert "f435a42c" not in warning[0]                             # below threshold: not a precedent
    # exactly one gate recall (content as query) + one forwarded retain
    paths = [c["path"] for c in up.calls]
    assert paths == [f"/v1/default/banks/{BANK}/memories/recall", "/mcp"]
    assert up.calls[0]["body"]["query"].startswith("[차단] 데모 사이트 CSS")
    ev = audit_lines(log, "contradiction-warn")
    assert len(ev) == 1 and ev[0]["reason"] == "precedent:22aebc9e"
    assert ev[0]["contradiction"]["precedent_ids"] == ["22aebc9e-aaaa"]
    raw = log.read_text(encoding="utf-8")
    assert "RETAIN-SECRET" not in raw and "PRECEDENT-SECRET" not in raw


def test_blocked_retain_without_precedent_has_no_warning(tmp_path):
    up = Upstream(gate_results=[mem("f435a42c-bbbb", "[완료] 같은 주제 다른 일", 0.80),     # below 0.90
                                mem("2ddf0cdd-cccc", "[함정] 부정 극성 이웃", 0.95)])        # not positive
    client, log = make(up, tmp_path)
    resp = mcp(client, "retain", retain_args(BLOCKED, tags=("kind:decision",)))
    assert not any(t.startswith("[gateway 경고]") for t in mcp_texts(resp))
    assert audit_lines(log, "contradiction-warn")[0]["reason"] == "no-precedent"


def test_non_eligible_retain_causes_no_gate_recall(tmp_path):
    up = Upstream(gate_results=[mem("x", PRECEDENT, 0.99)])
    client, log = make(up, tmp_path)
    mcp(client, "retain", retain_args("[완료] 배포 성공", tags=("kind:completion", "project:x")))
    assert [c["path"] for c in up.calls] == ["/mcp"]
    assert audit_lines(log, "contradiction-warn") == [] and audit_lines(log, "recurrence") == []


def test_rest_retain_discloses_gateway_warnings(tmp_path):
    up = Upstream(gate_results=[mem("22aebc9e-aaaa", PRECEDENT, 0.957)])
    client, log = make(up, tmp_path)
    resp = client.post(f"/v1/default/banks/{BANK}/memories",
                       content=json.dumps({"items": [{"content": BLOCKED, "tags": ["kind:gotcha"],
                                                      "timestamp": "2026-09-21T10:00:00Z"}]}),
                       headers={"content-type": "application/json", "authorization": "Bearer tok-alice"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True and data["gateway_warnings"][0]["index"] == 0
    assert "id:22aebc9e" in data["gateway_warnings"][0]["text"]
    assert audit_lines(log, "contradiction-warn")[0]["contradiction"]["transport"] == "rest"


# --- 2차: recurrence -------------------------------------------------------------------

def test_gotcha_recurrence_drafts_trigger_with_rescue_query_and_notifies_once(tmp_path):
    old = mem("fb9fc4e3-dddd", GOTCHA_OLD, 0.958)
    up = Upstream(gate_results=[old, mem("d6ec924c-eeee", "pnpm TTY gotcha", 0.876)],
                  mcp_recall_results=[old])
    notifier = Notifier()
    client, log = make(up, tmp_path, notifier)
    # 1) the session recalls late - THIS query is the one that rescued it
    r = mcp(client, "recall", {"query": RESCUE_QUERY, "max_tokens": 4096, "tags": ["project:aside"]}, el_id=7)
    assert r.status_code == 200
    ledger = [json.loads(l) for l in (tmp_path / "recall-queries.log").read_text(encoding="utf-8").splitlines()]
    assert ledger[0]["query"] == RESCUE_QUERY and ledger[0]["result_ids"] == ["fb9fc4e3-dddd"]
    assert ledger[0]["transport"] == "mcp" and ledger[0]["identity"] == "alice"
    # 2) then retains the same trap again
    resp = mcp(client, "retain", retain_args(GOTCHA_NEW), el_id=8)
    assert resp.status_code == 200 and json.loads(mcp_texts(resp)[0])["status"] == "accepted"
    drafts = [json.loads(l) for l in (tmp_path / "pending.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(drafts) == 1
    d = drafts[0]
    assert d["signal"] == "gotcha-recurrence" and d["bank"] == BANK
    assert d["trigger"]["recall"]["query"] == RESCUE_QUERY                 # proven query reused
    assert d["trigger"]["recall"]["tags"] == ["project:aside"]
    assert d["trigger"]["max_tokens"] == 500 and d["trigger"]["max_memories"] == 2
    assert d["evidence"]["similar_memory_id"] == "fb9fc4e3-dddd"
    assert d["evidence"]["rescue_query_source"] == "recall-log" and d["evidence"]["rescue_result_hit"] is True
    assert d["trigger"]["all_of"][0]                                   # any-of group is never empty
    assert notifier.drafts and notifier.drafts[0]["draft_id"] == d["draft_id"]
    ev = audit_lines(log, "recurrence")
    assert ev[0]["reason"] == "draft:gotcha-recurrence" and ev[0]["recurrence"]["notify"] == "ok"
    assert ev[0]["recurrence"]["similar_memory_id"] == "fb9fc4e3-dddd"
    # 3) same trap a third time -> dedup, no second draft / notification
    mcp(client, "retain", retain_args(GOTCHA_NEW + " 다시"), el_id=9)
    assert len((tmp_path / "pending.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert len(notifier.drafts) == 1
    assert audit_lines(log, "recurrence")[-1]["reason"] == "skip:dedup"
    raw = log.read_text(encoding="utf-8")
    assert "격리세션" not in raw and RESCUE_QUERY not in raw           # audit stays text-free


def test_gotcha_without_similar_existing_gotcha_drafts_nothing(tmp_path):
    up = Upstream(gate_results=[mem("d6ec924c-eeee", "pnpm TTY gotcha", 0.876)])
    notifier = Notifier()
    client, log = make(up, tmp_path, notifier)
    mcp(client, "retain", retain_args(GOTCHA_NEW))
    assert not (tmp_path / "pending.jsonl").exists() and notifier.drafts == []
    assert audit_lines(log, "recurrence") == []


def test_correction_after_recall_drafts_and_correction_alone_is_skipped(tmp_path):
    old = mem("22aebc9e-aaaa", PRECEDENT, 0.95)
    up = Upstream(gate_results=[old], mcp_recall_results=[old])
    notifier = Notifier()
    client, log = make(up, tmp_path, notifier)
    # no recall in the window -> plain correction, skipped
    mcp(client, "retain", retain_args("[정정] 어제 판단은 틀렸다", tags=("kind:decision",)), el_id=1)
    assert audit_lines(log, "recurrence")[-1]["reason"] == "skip:no-verified-recall"
    assert notifier.drafts == []
    # recall first, then a correction -> the late-recall pattern
    mcp(client, "recall", {"query": RESCUE_QUERY, "max_tokens": 4096}, el_id=2)
    mcp(client, "retain", retain_args("[정정] 퍼지 불가 판정은 틀렸다 - 분담 구조로 성공", tags=("kind:decision",)), el_id=3)
    drafts = [json.loads(l) for l in (tmp_path / "pending.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(drafts) == 1 and drafts[0]["signal"] == "correction-after-recall"
    assert drafts[0]["trigger"]["recall"]["query"] == RESCUE_QUERY
    assert len(notifier.drafts) == 1


def test_gate_recall_failure_fails_open(tmp_path):
    class Down(Upstream):
        def handler(self, request):
            if request.url.path.endswith("/memories/recall"):
                return httpx.Response(503)
            return super().handler(request)
    up = Down()
    client, log = make(up, tmp_path)
    resp = mcp(client, "retain", retain_args(BLOCKED))
    assert resp.status_code == 200 and json.loads(mcp_texts(resp)[0])["status"] == "accepted"
    assert not any(t.startswith("[gateway 경고]") for t in mcp_texts(resp))
    line = audit_lines(log, "contradiction-warn")[0]
    assert line["reason"] == "recall-failed" and line["gate"]["recall_error"] == "http-503"


def test_gate_recall_timeout_is_audited_and_keeps_the_8s_budget(tmp_path):
    """#268: the gate names the cause of a failed recall, and unlike the trigger path it keeps
    the 8 s budget - it runs inside the retain request, not under a 6 s hook client. The literal
    is deliberate: comparing against the constant would pass if someone lowered it."""
    seen = []

    class Slow(Upstream):
        def handler(self, request):
            if request.url.path.endswith("/memories/recall"):
                seen.append(request.extensions.get("timeout"))
                raise httpx.ReadTimeout("slow", request=request)
            return super().handler(request)

    client, log = make(Slow(), tmp_path)
    resp = mcp(client, "retain", retain_args(BLOCKED))
    assert resp.status_code == 200 and json.loads(mcp_texts(resp)[0])["status"] == "accepted"
    assert seen and set(seen[0].values()) == {8.0}
    line = audit_lines(log, "contradiction-warn")[0]
    assert line["reason"] == "recall-failed" and line["gate"]["recall_error"] == "timeout"


def test_rest_recall_is_recorded_in_ledger(tmp_path):
    up = Upstream()
    client, _ = make(up, tmp_path)
    resp = client.post(f"/v1/default/banks/{BANK}/memories/recall",
                       content=json.dumps({"query": RESCUE_QUERY, "max_tokens": 4096}),
                       headers={"content-type": "application/json", "authorization": "Bearer tok-alice"})
    assert resp.status_code == 200
    ledger = [json.loads(l) for l in (tmp_path / "recall-queries.log").read_text(encoding="utf-8").splitlines()]
    assert ledger[0]["query"] == RESCUE_QUERY and ledger[0]["transport"] == "rest"


def test_polarity_helpers():
    from gateway import contradiction
    assert contradiction.is_negative("[차단] x") and contradiction.is_negative("[불가] y") and contradiction.is_negative("[막힘] z")
    assert not contradiction.is_negative("[완료] x") and not contradiction.is_negative("no label 차단")
    # a negative word inside a "wall came down" label is positive news, not a contradiction candidate
    assert not contradiction.is_negative("[차단해제] x") and not contradiction.is_negative("[실패 복구] y")
    assert not contradiction.is_negative("[불가 우회] z")
    assert contradiction.is_positive_precedent({"text": "[정정+완료] a", "tags": ["kind:gotcha"]})
    assert contradiction.is_positive_precedent({"text": "plain", "tags": ["kind:completion"]})
    assert not contradiction.is_positive_precedent({"text": "[함정] b", "tags": ["kind:gotcha"]})
    assert recurrence.is_correction("[정정+완료] a") and recurrence.is_correction("[철회] b")
    assert not recurrence.is_correction("[완료] c")


def test_mcp_recall_payload_unwraps_double_encoded_and_dict_shapes():
    payload = {"results": [{"id": "m-1", "text": "x"}, {"id": "m-2", "text": "y"}]}
    double = {"content": [{"type": "text", "text": json.dumps(payload)}],
              "structuredContent": {"result": json.dumps(payload)}, "isError": False}
    assert recurrence.recall_result_ids(recurrence.mcp_recall_payload(double)) == ["m-1", "m-2"]
    as_dict = {"content": [], "structuredContent": {"result": payload}, "isError": False}
    assert recurrence.recall_result_ids(recurrence.mcp_recall_payload(as_dict)) == ["m-1", "m-2"]
    text_only = {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}
    assert recurrence.recall_result_ids(recurrence.mcp_recall_payload(text_only)) == ["m-1", "m-2"]
    assert recurrence.mcp_recall_payload({"content": [{"type": "text", "text": "boom"}], "isError": True}) is None
    assert recurrence.mcp_recall_payload({"structuredContent": {"result": "not json"}, "content": []}) is None


def test_draft_preview_and_query_are_secret_scrubbed():
    ev = recurrence.RecallEvent(ts=0.0, identity="alice", bank=BANK, transport="mcp",
                                query="token mt_abcdefghij0123456789 leaked in query", tags=(), result_ids=())
    draft = recurrence.build_draft(bank=BANK, signal=recurrence.SIGNAL_GOTCHA,
                                   new_content="[발견] seed JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP in content",
                                   similar=None, rescue=ev)
    assert "mt_abcdef" not in draft["trigger"]["recall"]["query"] and "[redacted]" in draft["trigger"]["recall"]["query"]
    assert "JBSWY3DP" not in draft["evidence"]["new_content_preview"]
    assert "JBSWY3DP" not in recurrence.discord_message(draft) and "mt_abcdef" not in recurrence.discord_message(draft)


def test_default_ledger_paths_are_isolated_from_repo_audit_dir(make_app, tmp_path):
    """conftest's autouse fixture must route the DEFAULT ledger/pending paths to tmp (review D3)."""
    app = make_app()
    assert app.state.recall_ledger.path.startswith(str(tmp_path))
    assert app.state.pending_triggers.path.startswith(str(tmp_path))
    assert not os.path.abspath(app.state.recall_ledger.path).startswith(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit"))


# --- review P1 (2026-09-23): gate query must fit upstream's 500-token query limit ----------

LONG_BLOCKED = BLOCKED + (" 원인 추정: 원격 콘솔은 세션마다 2단계 코드 재입력을 요구하고, 도구 A 세션에서는"
                          " 파일 접근이 막혀 코드를 만들 수 없다. 재시도 3회 모두 같은 화면에서 멈췄다.") * 7


def test_real_length_blocked_retain_still_reaches_precedent(tmp_path):
    """Production shape: a 700+ char retain. The old 2000-char cut sent the whole thing, the
    upstream 400'd it, and the gate silently recorded recall-failed for every real retain."""
    assert len(LONG_BLOCKED) > 700 and _approx_cl100k(LONG_BLOCKED) > 500
    up = Upstream(gate_results=[mem("22aebc9e-aaaa", PRECEDENT, 0.957)])
    client, log = make(up, tmp_path)
    texts = mcp_texts(mcp(client, "retain", retain_args(LONG_BLOCKED)))
    assert json.loads(texts[0])["status"] == "accepted"
    assert any(t.startswith("[gateway 경고] 반대 결론의 전례 있음") for t in texts)
    gate = [c for c in up.calls if c["path"].endswith("/memories/recall")]
    assert len(gate) == 1 and _approx_cl100k(gate[0]["body"]["query"]) <= 500
    assert gate[0]["body"]["query"].startswith("[차단] 데모 사이트 CSS")
    assert not [l for l in audit_lines(log, "contradiction-warn") if l["reason"] == "recall-failed"]


def test_query_token_bound_upper_bounds_real_cl100k_counts():
    """Pinned tiktoken cl100k_base counts (measured 2026-09-23); the bound must never be lower.
    The first sample was later rewritten as a synthetic sentence and re-measured with tiktoken cl100k_base."""
    from gateway.context_triggers import QUERY_TOKEN_TARGET, clip_query, query_token_bound
    pinned = [
        ("[차단] 데모 사이트 CSS 엣지 캐시 퍼지 불가 - 도구 A가 원격 콘솔 로그인의 2단계 화면을 넘지 못한다", 63),
        ("똠방각하 뷁쀍햏 " * 8, 137),                       # rare syllables: the worst real case
        ("a1B2c3D4e5" * 6, 60),                              # ASCII at 1 token/char
        ("게이트웨이 retain 규칙 강제 - 형식 위반은 isError+RULE:id로 교정된다. 🚀", 38),
    ]
    for text, real in pinned:
        assert query_token_bound(text, hangul=3.0) >= real, text      # hard bound: never lower
    realistic = [p for p in pinned if "뷁" not in p[0]]
    for text, real in realistic:
        assert query_token_bound(text) >= real, text                  # default estimate
    assert query_token_bound(pinned[1][0]) < pinned[1][1]              # known gap -> retry path
    clipped = clip_query(LONG_BLOCKED)
    assert query_token_bound(clipped) <= QUERY_TOKEN_TARGET and LONG_BLOCKED.startswith(clipped[:40])


def test_trigger_row_with_query_over_token_limit_is_rejected_at_load():
    import pytest
    from gateway.context_triggers import TriggerTable, TriggerTableError
    row = {"id": "long-q", "bank": BANK, "all_of": [["democloud"]],
           "recall": {"query": "데모클라우드 대시보드 2단계 분담 구조 " * 20}}
    with pytest.raises(TriggerTableError, match="recall query limit"):
        TriggerTable.from_dict({"version": 1, "triggers": [row]})


def test_upstream_query_too_long_is_retried_once_with_hard_bound():
    import asyncio
    from gateway.context_triggers import UPSTREAM_QUERY_MAX_TOKENS, query_token_bound, upstream_recall
    seen = []

    def handler(request):
        q = json.loads(request.content)["query"]
        seen.append(q)
        if len(seen) == 1:
            return httpx.Response(400, json={"detail": "Query too long: 612 tokens exceeds maximum of 500"})
        return httpx.Response(200, json={"results": [{"id": "m1", "text": "x"}]})

    async def go():
        async with httpx.AsyncClient(base_url="http://u", transport=httpx.MockTransport(handler)) as c:
            return await upstream_recall(c, upstream_key="", bank="b", query="뷁쀍햏 " * 150)
    assert asyncio.run(go()) == [{"id": "m1", "text": "x"}]
    assert len(seen) == 2 and query_token_bound(seen[1], hangul=3.0) <= UPSTREAM_QUERY_MAX_TOKENS


# --- review P2-1 (2026-09-23): base32 seeds in any case / grouped, Slack, vendor tokens -----

def test_scrub_catches_lowercase_grouped_seeds_and_vendor_tokens_without_eating_prose():
    from gateway.context_triggers import scrub_secrets
    for secret in ["jbswy3dpehpk3pxp", "jbsw y3dp ehpk 3pxp", "JBSW Y3DP EHPK 3PXP", "jbsw-y3dp-ehpk-3pxp",
                   "JBSWY3DPEHPK3PXP", "xoxb-123456789012-abcdefABCDEF",
                   "cfut_AbCdEf0123456789AbCdEf0123456789abcd"]:
        out = scrub_secrets(f"시드 {secret} 확인")
        assert secret not in out and "[redacted]" in out, secret
    for prose in ["internationalization characterization", "recall_max_query_tokens enableAutoConsolidation",
                  "have been used with some otp", "THIS THAT WITH FROM", "commit 2671500 bcc40a6"]:
        assert scrub_secrets(prose) == prose, prose


def test_lowercase_seed_shared_by_both_texts_never_reaches_keywords_preview_or_discord():
    seed = "jbswy3dpehpk3pxp"
    new = f"[gotcha] democloud otp seed {seed} 는 호출자가 읽는다 / grouped jbsw y3dp ehpk 3pxp"
    old = {"id": "old", "text": f"[gotcha] democloud otp 시드 {seed} 경로", "tags": ["kind:gotcha"],
           "entities": [seed], "scores": {"reranker": 0.95}}
    ev = recurrence.RecallEvent(ts=0.0, identity="demo", bank="b", transport="mcp",
                                query="democloud otp 분담", tags=(), result_ids=("old",))
    d = recurrence.build_draft(bank="b", signal="gotcha-recurrence", new_content=new, similar=old, rescue=ev)
    blob = json.dumps(d, ensure_ascii=False) + recurrence.discord_message(d)
    assert seed not in blob and "y3dp" not in blob
    assert seed not in d["trigger"]["all_of"][0]


def test_polarity_covers_tool_diagnosis_words_and_negated_negatives():
    """Review P3 (2026-09-23): CLAUDE.md calls tool self-diagnoses symptoms, not causes -
    those are exactly the labels a premature 'blocked' retain carries."""
    from gateway import contradiction as c
    for neg in ["권한 없음", "접근 거부", "만료", "타임아웃", "안됨", "못함", "포기", "보류", "에러",
                "denied", "timed out", "불완료", "우회 실패", "해제 불가"]:
        assert c.is_negative(f"[{neg}] x"), neg
    for pos in ["막힘 해소", "차단 아님", "실패 아님", "에러 해결", "차단 해제", "실패 복구"]:
        assert not c.is_negative(f"[{pos}] x") and c.is_positive_precedent({"text": f"[{pos}] x"}), pos
    assert not c.is_positive_precedent({"text": "[성공률 낮음] x"}) and not c.is_negative("[성공률 낮음] x")


def test_scrub_catches_secrets_followed_by_korean_particles_and_spares_camelcase():
    """Review 2nd pass: Python's \\b counts Hangul as a word char, so particles hid secrets."""
    from gateway.context_triggers import scrub_secrets
    for text, secret in [("시드는 JBSWY3DPEHPK3PXP로 등록", "JBSWY3DPEHPK3PXP"), ("jbswy3dpehpk3pxp를", "jbswy3dpehpk3pxp"),
                         ("ghp_0123456789abcdefghijklmn0123를 폐기", "ghp_0123456789abcdefghijklmn0123"),
                         ("sk-0123456789abcdefghij는 만료", "sk-0123456789abcdefghij"),
                         ("cfut_AbCdEf0123456789AbCdEf0123456789abcd로", "cfut_AbCdEf0123456789AbCdEf0123456789abcd")]:
        assert secret not in scrub_secrets(text), text
    for prose in ["passwordResetToken2 필드", "Qwen3VLEmbedding 모델"]:
        assert scrub_secrets(prose) == prose


@pytest.mark.parametrize("text, expected", [
    # issue #270: grouped uppercase seeds with no digit 2-7 must be erased
    ("HJKM-NPQR-STUV-WXYZ", "[redacted]"),
    ("HJKM NPQR STUV WXYZ", "[redacted]"),
    ("JBSW Y3DP EHPK 3PXP", "[redacted]"),
    ("HJKMNPQRSTUVWXYZ", "[redacted]"),
    ("hjkm npqr stuv wxyz", "hjkm npqr stuv wxyz"),    # lowercase groups keep the digit requirement
    # vowel rule boundary: a vowelless 4-char group marks a seed, all-voweled groups read as prose
    ("THIS THAT WITH FROM", "THIS THAT WITH FROM"),
    ("MYTH SYNC LYNX HYMN", "MYTH SYNC LYNX HYMN"),    # Y counts as a vowel
    ("ABCD EFGH IJKL MNPQ", "[redacted]"),             # last group has no vowel
])
def test_scrub_grouped_uppercase_seed_without_digit_needs_a_vowelless_group(text, expected):
    from gateway.context_triggers import scrub_secrets
    assert scrub_secrets(text) == expected


def test_polarity_second_pass_rates_negations_and_embedded_words():
    from gateway import contradiction as c
    for label in ["오류율 개선", "실패율 감소", "만료일 확인", "보안됨"]:
        assert not c.is_negative(f"[{label}] x"), label
    for label in ["에러 없음", "만료 아님", "거부 아님", "timeout 아님", "미완료 아님"]:
        assert not c.is_negative(f"[{label}] x") and c.is_positive_precedent({"text": f"[{label}] x"}), label
    assert c.is_negative("[권한 없음] x") and c.is_negative("[잘 안됨] x")
