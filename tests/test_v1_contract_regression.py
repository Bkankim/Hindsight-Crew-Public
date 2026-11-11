"""upstream v1.0.0 계약 회귀 (issue #247): 응답 가공을 v1.0.0 원본 바이트로 재생한다.

fixtures/mcp/*.raw·fixtures/rest/recall_gate.json 은 scripts/mcp-probe.sh 가 사본(hc-next, --llm opus)에서
upstream 직결로 채집한 원본이다(fixtures/mcp/NOTES.md). 여기서는 그 바이트와 Content-Type 을 mock upstream 이
그대로 돌려주고, 게이트웨이 요청 경계(TestClient)에서 단언한다. 변환이 실제로 적용됐는지를 먼저 보고(원본과
달라야 한다), fail-open 으로 원본이 그대로 나가면 실패한다.

  1. 원본 전제 고정   - 이 시험들이 공허하지 않으려면 원본이 가져야 하는 모양
  2. recall_prune      - MCP·REST, attachments 규칙
  3. unescape          - prune 이 닿지 않는 경로(get_memory, HC_RECALL_PRUNE=0)에서도
  4. honesty           - 성공형 실패 프레임 -> isError true, 나머지는 무변조
  5. reflect_based_on  - 인자 생략 시 주입, 응답에서 제거
  6. retain_gates      - 시드와 비슷한 kind:gotcha retain 이 REST recall 원본으로 초안을 만든다
  7. 지식베이스 도구   - v1.0.0 에서 새로 생긴 7개는 허용 목록 거부, upstream 0회
  8. #24 은퇴 뒤 통과  - 규칙 뱅크 tools/list 의 list_mental_models 와 REST 목록은 upstream 그대로
"""
import base64
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, FIXTURES_DIR, UPSTREAM_KEY, call
from gateway import honesty, policy, recall_prune, recurrence
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit
from test_mcp_handshake import _tools_from, _tools_list_app, _tools_list_req
from test_response_honesty import sse_payload

BANK = "personal-alice"      # ACL_DATA 의 데모 principal alice 가 허용된 뱅크
TOKEN = "tok-alice"
REFLECT_AUTOFILL_REASON = "based-on-default:true"

# 브랜치 시작 시점(origin/main) fixtures/mcp/tools_list.raw 는 v0.8.3 의 32도구였다. 재채집으로 새로 생긴 도구가 아래 7개다.
PREVIOUS_TOOL_NAMES = frozenset({
    "retain", "sync_retain", "recall", "reflect", "list_banks", "create_bank", "get_bank", "get_bank_stats",
    "update_bank", "delete_bank", "clear_memories", "list_memories", "get_memory", "update_memory",
    "invalidate_memory", "list_documents", "get_document", "delete_document", "list_tags",
    "list_mental_models", "get_mental_model", "create_mental_model", "update_mental_model",
    "refresh_mental_model", "clear_mental_model", "delete_mental_model", "list_directives",
    "create_directive", "delete_directive", "list_operations", "get_operation", "cancel_operation",
})
NEW_KNOWLEDGE_TOOLS = (
    "create_knowledge_folder", "create_knowledge_page", "delete_knowledge_node", "get_knowledge_base_tree",
    "get_knowledge_page", "search_knowledge_base", "update_knowledge_node",
)


# --------------------------------------------------------------------------
# fixture 로더 / mock upstream
# --------------------------------------------------------------------------

def raw(name):
    with open(os.path.join(FIXTURES_DIR, "mcp", name + ".raw"), "rb") as fh:
        return fh.read()


def content_type(name):
    with open(os.path.join(FIXTURES_DIR, "mcp", name + ".headers"), encoding="utf-8") as fh:
        for line in fh:
            if line.lower().startswith("content-type:"):
                return line.split(":", 1)[1].strip()
    raise AssertionError(f"{name}.headers has no content-type")


def element(name):
    """원본 바이트의 JSON-RPC element."""
    return sse_payload(raw(name))


def result_of(name):
    return element(name)["result"]


def inner_of(result):
    """content[0].text 를 JSON 으로 (이중 인코딩된 본문)."""
    return json.loads(result["content"][0]["text"])


def fixture_id(name):
    return element(name)["id"]


def seed_cases():
    with open(os.path.join(FIXTURES_DIR, "mcp", "seed_cases.json"), encoding="utf-8") as fh:
        return json.load(fh)


def rest_recall():
    with open(os.path.join(FIXTURES_DIR, "rest", "recall_gate.json"), encoding="utf-8") as fh:
        fx = json.load(fh)
    fx["body"] = base64.b64decode(fx["bodyBase64"])
    return fx


def has_escape(text):
    return "\\u" in text


class Replay:
    """tool 이름 -> fixture 이름으로 /mcp 응답을 재생하고, REST recall 은 recall_gate 원본을 재생한다."""

    def __init__(self, mcp=None):
        self.mcp = dict(mcp or {})
        self.calls = []

    def handler(self, request):
        body = json.loads(request.content or b"{}")
        self.calls.append({"path": request.url.path, "body": body})
        if request.url.path.endswith("/memories/recall"):
            fx = rest_recall()
            return httpx.Response(fx["status"], content=fx["body"], headers={"content-type": fx["contentType"]})
        if request.url.path == "/mcp":
            name = (body.get("params") or {}).get("name")
            fixture = self.mcp[name]
            return httpx.Response(200, content=raw(fixture),
                                  headers={"content-type": content_type(fixture), "mcp-session-id": "s1"})
        return httpx.Response(404)

    def paths(self):
        return [c["path"] for c in self.calls]

    def rest_recalls(self):
        return [c for c in self.calls if c["path"].endswith("/memories/recall")]

    def mcp_args(self, tool):
        return [c["body"]["params"]["arguments"] for c in self.calls
                if c["path"] == "/mcp" and (c["body"].get("params") or {}).get("name") == tool]


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
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(log)), upstream_key=UPSTREAM_KEY,
                     http_client=http, notifier=notifier)
    return TestClient(app), log


def mcp(client, tool, arguments, el_id, bank=BANK, token=TOKEN):
    body = {"jsonrpc": "2.0", "id": el_id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}
    return client.post(f"/mcp/{bank}/", content=json.dumps(body),
                       headers={"content-type": "application/json", "authorization": f"Bearer {token}"})


def audit_lines(log):
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def rewrite_failures(client):
    return client.get("/healthz").json()["rewrite_failures"]


# --------------------------------------------------------------------------
# 1. 원본 전제 (공허한 통과 방지)
# --------------------------------------------------------------------------

def test_initialize_reports_v1_server():
    assert element("initialize")["result"]["serverInfo"]["version"] == "1.1.0"   # #285 재채집(v1.1.0)


def test_knowledge_tools_are_exactly_the_new_names_in_tools_list():
    names = {t["name"] for t in result_of("tools_list")["tools"]}
    assert names - PREVIOUS_TOOL_NAMES == set(NEW_KNOWLEDGE_TOOLS)
    assert PREVIOUS_TOOL_NAMES <= names


def test_get_memory_original_is_escaped_korean():
    text = result_of("get_memory_ok")["content"][0]["text"]
    assert has_escape(text)
    assert not any("가" <= ch <= "힣" for ch in text)              # 한글이 전부 \uXXXX 로만 있다
    assert "프로브 시드" in inner_of(result_of("get_memory_ok"))["text"]


def test_v1_recall_original_is_not_escaped():
    """v1.0.0 의 recall 은 한글을 그대로 쓴다. 이슈가 recall 로 하려던 unescape 시험이 공허한 이유(NOTES.md)."""
    text = result_of("recall_ok")["content"][0]["text"]
    assert not has_escape(text)
    assert "프로브 시드" in text


def test_reflect_originals_pair():
    with_based_on = result_of("reflect_based_on")
    default = result_of("reflect_default")
    for carrier in (with_based_on["content"][0]["text"], with_based_on["structuredContent"]["result"]):
        assert "based_on" in json.loads(carrier)
    for carrier in (default["content"][0]["text"], default["structuredContent"]["result"]):
        assert "based_on" not in json.loads(carrier)
    assert has_escape(with_based_on["content"][0]["text"])           # reflect 도 \uXXXX 다


def test_success_frame_failure_original():
    result = result_of("sync_retain_error_frame")
    assert result["isError"] is False
    assert result["structuredContent"]["status"] == "error"
    assert inner_of(result)["status"] == "error"


def test_validation_error_original_is_already_honest():
    result = result_of("sync_retain_fail")
    assert result["isError"] is True and "structuredContent" not in result


def test_rest_recall_original_is_replayable_and_gate_ready():
    fx, seed = rest_recall(), seed_cases()
    assert fx["status"] == 200 and fx["contentType"].startswith("application/json")
    assert json.loads(fx["body"]) == fx["response"]
    assert fx["request"] == {"query": seed["retainBody"], "max_tokens": 4096}
    top = fx["response"]["results"][0]
    assert sorted(top["tags"]) == sorted(seed["seedTags"]) and top["text"] == seed["seedBody"]
    assert top["scores"]["reranker"] >= recurrence.DEFAULT_MIN_RERANK
    assert "attachments" in top and top["attachments"] is None


# --------------------------------------------------------------------------
# 2. recall_prune
# --------------------------------------------------------------------------

def _expected_rows(rows):
    """원본 행에서 recall_prune 의 무손실 제거만 뺀 것: null/빈 컨테이너, occurred_start 와 같은 occurred_end·mentioned_at.
    chunk_id 는 probe-n0 뱅크 형식이라 요청 뱅크(personal-alice)로는 복원되지 않아 남는다."""
    out = []
    for row in rows:
        kept = {k: v for k, v in row.items() if v is not None and v != {} and v != []}
        for twin in ("occurred_end", "mentioned_at"):
            if twin in kept and kept.get(twin) == row.get("occurred_start"):
                del kept[twin]
        out.append(kept)
    return out


def test_mcp_recall_is_pruned_and_the_rest_is_kept(tmp_path):
    up = Replay({"recall": "recall_ok"})
    client, _ = make(up, tmp_path)
    resp = mcp(client, "recall", {"query": seed_cases()["recallQuery"], "max_tokens": 4096}, fixture_id("recall_ok"))
    assert resp.status_code == 200
    assert resp.content != raw("recall_ok")                          # 변환이 실제로 적용됐다(fail-open 아님)
    original = inner_of(result_of("recall_ok"))
    result = sse_payload(resp.content)["result"]
    final = inner_of(result)
    assert result["structuredContent"]["result"] == result["content"][0]["text"]
    assert final["results"] == _expected_rows(original["results"])
    assert {"trace", "chunks", "source_facts", "source_facts_truncated"} <= set(original)
    assert set(final) == {k for k, v in original.items() if v is not None and v != {} and v != []}
    row = final["results"][0]
    assert "entities" not in row and "occurred_start" not in row and "source_fact_ids" not in row
    assert row["text"] == seed_cases()["seedBody"] and row["scores"] == original["results"][0]["scores"]
    assert rewrite_failures(client) == 0


def test_rest_recall_is_pruned_and_null_attachments_follow_the_existing_rule(tmp_path):
    up = Replay()
    client, _ = make(up, tmp_path)
    resp = client.post(f"/v1/default/banks/{BANK}/memories/recall", content=json.dumps(rest_recall()["request"]),
                       headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/json")
    assert resp.content != rest_recall()["body"]                     # 변환이 실제로 적용됐다
    original, final = rest_recall()["response"], resp.json()
    assert all("attachments" in row and row["attachments"] is None for row in original["results"])
    assert all("attachments" not in row for row in final["results"])  # null 은 기존 규칙(_empty)대로 제거
    assert final["results"] == _expected_rows(original["results"])
    assert final["entities"] == original["entities"]                 # 값이 있는 최상위 키는 남는다
    assert not {"trace", "chunks", "source_facts", "source_facts_truncated"} & set(final)
    assert rewrite_failures(client) == 0


@pytest.mark.parametrize("attachments,kept", [
    (None, False), ([], False), ({}, False),                          # 빈 값: 기존 규칙대로 제거
    ([{"id": "att-1", "name": "a.png"}], True),                       # 값이 있으면 제거 목록에 없으니 그대로 보존
])
def test_attachments_rule_is_the_existing_empty_container_rule(attachments, kept):
    row = {**rest_recall()["response"]["results"][0], "attachments": attachments}
    pruned = recall_prune.prune_result(row, BANK)
    assert ("attachments" in pruned) is kept
    if kept:
        assert pruned["attachments"] == attachments


# --------------------------------------------------------------------------
# 3. unescape (prune 이 닿지 않는 경로)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("prune", ["1", "0"])
@pytest.mark.parametrize("tool,fixture,arguments", [
    ("get_memory", "get_memory_ok", {"memory_id": "351b44a7-e7dd-474d-89b9-5e35620f0260"}),
    ("reflect", "reflect_default", {"query": "q", "include_based_on": False}),
])
def test_escaped_korean_is_restored_losslessly(tmp_path, monkeypatch, prune, tool, fixture, arguments):
    monkeypatch.setenv(recall_prune.ENV_FLAG, prune)                 # get_memory·reflect 는 prune 대상이 아니다
    up = Replay({tool: fixture})
    client, _ = make(up, tmp_path)
    resp = mcp(client, tool, arguments, fixture_id(fixture))
    assert resp.status_code == 200
    original = result_of(fixture)
    assert has_escape(original["content"][0]["text"])                # 원본에 \uXXXX 가 있다(공허 아님)
    result = sse_payload(resp.content)["result"]
    text = result["content"][0]["text"]
    assert not has_escape(text) and "프로브" in text                  # 풀렸다
    assert json.loads(text) == json.loads(original["content"][0]["text"])        # 무손실
    assert result["structuredContent"]["result"] == text
    assert resp.content != raw(fixture)
    assert rewrite_failures(client) == 0


def test_recall_with_prune_off_is_forwarded_byte_for_byte(tmp_path, monkeypatch):
    """v1.0.0 recall 원본은 이미 풀려 있다. prune 을 끄면 게이트웨이는 아무것도 바꾸지 않는다."""
    monkeypatch.setenv(recall_prune.ENV_FLAG, "0")
    up = Replay({"recall": "recall_ok"})
    client, _ = make(up, tmp_path)
    resp = mcp(client, "recall", {"query": seed_cases()["recallQuery"], "max_tokens": 4096}, fixture_id("recall_ok"))
    assert resp.content == raw("recall_ok")
    assert rewrite_failures(client) == 0


# --------------------------------------------------------------------------
# 4. honesty
# --------------------------------------------------------------------------

RETAIN_ARGS = {"content": "[note] v1.0.0 회귀", "tags": ["kind:probe"], "timestamp": "2026-07-26T00:00:00Z"}


@pytest.mark.parametrize("tool,fixture,was_error,now_error,note", [
    # 성공형 프레임 안의 저장 실패: isError false -> true. 본문(text·structuredContent)은 그대로.
    ("sync_retain", "sync_retain_error_frame", False, True, None),
    # 진짜 실패(검증 오류): 이미 isError true 다. 바이트 무변조.
    ("sync_retain", "sync_retain_fail", True, True, None),
    # 성공: 건드리지 않는다(status completed / accepted 는 실패 신호가 아니다).
    ("sync_retain", "sync_retain_ok", False, False, None),
    ("sync_retain", "retain_seed_ok", False, False, None),
    # 비동기 수락은 get_operation 안내만 덧붙는다(isError 는 그대로).
    ("retain", "retain_async_ok", False, False, "async"),
])
def test_retain_responses(tmp_path, tool, fixture, was_error, now_error, note):
    up = Replay({tool: fixture})
    client, _ = make(up, tmp_path)
    resp = mcp(client, tool, RETAIN_ARGS, fixture_id(fixture))
    assert resp.status_code == 200
    original = result_of(fixture)
    assert original["isError"] is was_error                          # 원본 isError
    result = sse_payload(resp.content)["result"]
    assert result["isError"] is now_error                            # 변환 뒤 isError
    if note is None:
        assert result["content"] == original["content"]              # 텍스트 무변조
        assert result.get("structuredContent") == original.get("structuredContent")
    else:
        assert result["content"][0] == original["content"][0]
        assert len(result["content"]) == 2 and result["content"][1]["text"] == honesty.ASYNC_NOTE % \
            original["structuredContent"]["operation_id"]
    if fixture in ("sync_retain_fail", "sync_retain_ok", "retain_seed_ok"):
        assert resp.content == raw(fixture)                          # 바이트까지 무변조
    assert rewrite_failures(client) == 0


def test_success_frame_failure_is_flipped_only_by_status_error(tmp_path):
    """status 가 completed/accepted 인 성공은 같은 구조여도 뒤집히지 않는다 - 신호는 status == error 하나뿐."""
    for status in ("completed", "accepted", "failed", "errors"):
        el = {"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": json.dumps({"status": status})}],
            "structuredContent": {"status": status}, "isError": False}}
        out = honesty.annotate_element(el, retain_ids=[1])
        assert out["result"]["isError"] is False, status
    el = {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [], "structuredContent": {"status": " Error "}, "isError": False}}
    assert honesty.annotate_element(el, retain_ids=[1])["result"]["isError"] is True


# --------------------------------------------------------------------------
# 5. reflect include_based_on 주입·제거
# --------------------------------------------------------------------------

def _carriers(result):
    return {"text": json.loads(result["content"][0]["text"]),
            "structured": json.loads(result["structuredContent"]["result"])}


@pytest.mark.parametrize("arguments,sent,fixture,stripped", [
    ({"query": "q"}, True, "reflect_based_on", True),                          # 생략: true 를 채우고 응답에서 제거
    ({"query": "q", "include_based_on": None}, True, "reflect_based_on", True),  # null 도 "없음"
    ({"query": "q", "include_based_on": True}, True, "reflect_based_on", False),  # 명시한 true: 호출자가 원했으니 보존
    ({"query": "q", "include_based_on": False}, False, "reflect_default", False),  # 명시한 false: 채우지 않고 원본 그대로
])
def test_reflect_based_on(tmp_path, arguments, sent, fixture, stripped):
    up = Replay({"reflect": fixture})
    client, log = make(up, tmp_path)
    resp = mcp(client, "reflect", arguments, fixture_id(fixture))
    assert resp.status_code == 200
    assert up.mcp_args("reflect")[0]["include_based_on"] is sent      # upstream 으로 간 인자
    original = _carriers(result_of(fixture))
    final = _carriers(sse_payload(resp.content)["result"])
    for carrier in ("text", "structured"):
        if stripped:
            assert "based_on" in original[carrier]                    # 원본에는 있다(제거 시험이 공허하지 않다)
            assert "based_on" not in final[carrier]
            assert final[carrier] == {k: v for k, v in original[carrier].items() if k != "based_on"}
        else:
            assert final[carrier] == original[carrier]
    autofills = [e for e in audit_lines(log) if e["decision"] == "enforce-autofill"]
    assert [e["reason"] for e in autofills] == ([REFLECT_AUTOFILL_REASON] if stripped else [])
    assert rewrite_failures(client) == 0


# --------------------------------------------------------------------------
# 6. retain_gates (REST recall 원본 재생)
# --------------------------------------------------------------------------

def _gotcha_retain():
    seed = seed_cases()
    return {"content": seed["retainBody"], "tags": seed["seedTags"], "timestamp": "2026-10-14T00:00:00Z"}


def test_similar_gotcha_retain_drafts_a_trigger_from_the_recorded_recall(tmp_path):
    """sync_retain 으로 본다: 비동기 retain 은 operation 조회로 저장을 확인한 뒤에야 초안을 확정하는데(gate_effects) 그 응답은 채집 대상이 아니다."""
    tool, fixture = "sync_retain", "sync_retain_ok"
    seed, fx = seed_cases(), rest_recall()
    up = Replay({"recall": "recall_ok", tool: fixture})
    notifier = Notifier()
    client, log = make(up, tmp_path, notifier)
    # 1) 세션이 시드를 recall 한다 - 이 쿼리가 recall ledger 에 남는다
    recalled = mcp(client, "recall", {"query": seed["recallQuery"], "max_tokens": 4096}, fixture_id("recall_ok"))
    assert recalled.status_code == 200
    seed_id = fx["response"]["results"][0]["id"]
    assert seed_id in inner_of(result_of("recall_ok"))["results"][0]["id"]
    # 2) 시드와 비슷한 kind:gotcha retain
    resp = mcp(client, tool, _gotcha_retain(), fixture_id(fixture))
    assert resp.status_code == 200 and sse_payload(resp.content)["result"]["isError"] is False
    # 대상 retain 당 gate recall 은 정확히 1회, 요청은 upstream_recall 이 보내는 그 모양이다
    gate = up.rest_recalls()
    assert len(gate) == 1
    assert gate[0]["path"] == f"/v1/default/banks/{BANK}/memories/recall"
    assert gate[0]["body"] == fx["request"]
    # 초안: 기존 test_retain_gates 가 단언하는 것과 같은 종류(gotcha-recurrence, 구출 쿼리 재사용, 알림 1회)
    drafts = [json.loads(ln) for ln in (tmp_path / "pending-triggers.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["signal"] == "gotcha-recurrence" and draft["bank"] == BANK
    assert draft["evidence"]["similar_memory_id"] == seed_id
    assert draft["evidence"]["rescue_query_source"] == "recall-log" and draft["evidence"]["rescue_result_hit"] is True
    assert draft["trigger"]["recall"]["query"] == seed["recallQuery"]
    assert len(notifier.drafts) == 1 and notifier.drafts[0]["draft_id"] == draft["draft_id"]
    event = [e for e in audit_lines(log) if e["decision"] == "recurrence"]
    assert event and event[0]["reason"] == "draft:gotcha-recurrence"
    assert event[0]["recurrence"]["similar_memory_id"] == seed_id
    assert event[0]["recurrence"]["similar_score"] >= recurrence.DEFAULT_MIN_RERANK


def test_same_body_as_the_seed_is_not_a_recurrence(tmp_path):
    """본문이 시드와 똑같은 retain 은 "같은 기억"으로 건너뛴다(복원 뱅크가 verbatim 이라 저장 본문 = 입력)."""
    seed = seed_cases()
    up = Replay({"recall": "recall_ok", "sync_retain": "sync_retain_ok"})
    notifier = Notifier()
    client, log = make(up, tmp_path, notifier)
    mcp(client, "recall", {"query": seed["recallQuery"], "max_tokens": 4096}, fixture_id("recall_ok"))
    args = {**_gotcha_retain(), "content": seed["seedBody"]}
    assert mcp(client, "sync_retain", args, fixture_id("sync_retain_ok")).status_code == 200
    assert len(up.rest_recalls()) == 1                                # gate recall 은 돌았고
    assert not (tmp_path / "pending-triggers.jsonl").exists() and notifier.drafts == []     # 초안은 없다


def test_retain_without_a_gate_signal_costs_no_recall(tmp_path):
    up = Replay({"sync_retain": "sync_retain_ok"})
    client, log = make(up, tmp_path, Notifier())
    args = {"content": "[완료] 배포 성공", "tags": ["kind:completion"], "timestamp": "2026-10-14T00:00:00Z"}
    assert mcp(client, "sync_retain", args, fixture_id("sync_retain_ok")).status_code == 200
    assert up.rest_recalls() == []
    assert [e for e in audit_lines(log) if e["decision"] in ("recurrence", "contradiction-warn")] == []


# --------------------------------------------------------------------------
# 7. 지식베이스 도구 거부 (deny-by-default 유지)
# --------------------------------------------------------------------------

def test_knowledge_tools_are_not_on_any_allow_list():
    allowed = (policy.DESTRUCTIVE_TOOLS | policy.CORRECTIVE_TOOLS | policy.RETAIN_TOOLS | policy.READ_TOOLS)
    assert not set(NEW_KNOWLEDGE_TOOLS) & allowed


@pytest.mark.parametrize("tool", NEW_KNOWLEDGE_TOOLS)
def test_knowledge_tool_call_is_denied_with_zero_upstream_calls(tmp_path, tool):
    up = Replay({"recall": "recall_ok"})
    client, log = make(up, tmp_path)
    # 대조: 같은 principal·뱅크로 허용 도구는 통과한다 - 아래 거부가 잘못된 토큰·뱅크 때문이 아니다
    assert mcp(client, "recall", {"query": "q", "max_tokens": 4096}, fixture_id("recall_ok")).status_code == 200
    before = len(up.calls)
    resp = mcp(client, tool, {}, 7)
    assert resp.status_code == 403
    assert len(up.calls) == before                                    # upstream 호출 0회
    denies = [e for e in audit_lines(log) if e.get("tool") == tool]
    assert [(e["decision"], e["reason"]) for e in denies] == [("deny", f"tool-not-whitelisted:{tool}")]
    assert denies[0]["attempted_bank"] == BANK


# --------------------------------------------------------------------------
# 8. #24 은퇴 뒤에도 list_mental_models 는 upstream 그대로다
# --------------------------------------------------------------------------

def _upstream_tools():
    return {t["name"]: t for t in result_of("tools_list")["tools"]}


def test_tools_list_on_a_rule_bank_leaves_list_mental_models_as_upstream(tmp_path):
    """규칙 뱅크(personal-alice)의 tools/list 는 retain 계열만 고쳐 쓴다. list_mental_models 에 detail 을 권하는 설명이나
    schema 가 다시 붙으면 v1.0.0 에서 거부(또는 무시되는 인자 안내)를 되부른다(#24 은퇴)."""
    listed = _tools_from(call(_tools_list_app(tmp_path, raw("tools_list")), BANK, TOKEN, raw=_tools_list_req()))
    upstream = _upstream_tools()
    assert listed["list_mental_models"] == upstream["list_mental_models"]
    assert "detail" not in listed["list_mental_models"]["inputSchema"]["properties"]
    assert "detail" not in listed["list_mental_models"]["description"]
    # 대조: 같은 응답에서 retain 은 실제로 고쳐 쓰였다 - 위 동일성이 "아무것도 안 고쳐서" 성립한 것이 아님
    assert listed["retain"]["description"] != upstream["retain"]["description"]
    changed = {n for n in upstream if listed[n] != upstream[n]}
    assert changed <= policy.RETAIN_TOOLS


def test_rest_mental_model_listing_with_detail_content_passes_through_untouched(tmp_path):
    """scripts/model-refresh.sh(#243)는 trigger 가 있는 GET .../mental-models?detail=content&limit=1000 을 읽는다."""
    seen = []
    body = json.dumps({"items": [{"id": "x", "trigger": {"refresh_after_consolidation": True},
                                  "tags": ["project:x"], "content": "본문"}], "total": 1}).encode()

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://upstream")
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=http)
    resp = TestClient(app).get(f"/v1/default/banks/{BANK}/mental-models?detail=content&limit=1000",
                               headers={"authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200 and resp.content == body              # 본문 바이트 무변조(trigger 포함)
    assert len(seen) == 1 and seen[0].url.path == f"/v1/default/banks/{BANK}/mental-models"
    assert dict(seen[0].url.params) == {"detail": "content", "limit": "1000"}   # 쿼리를 더하거나 빼지 않는다
    assert seen[0].content == b""
