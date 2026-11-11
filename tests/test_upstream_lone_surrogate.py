"""Issue #138: upstream 응답에 lone surrogate(U+D800-U+DFFF)가 실려도 게이트웨이가 500도, 응답 거부도 내지 않는다.

upstream v0.8.3 은 정상 recall 결과에 surrogate 를 실을 수 없다(#137 분석). 이 파일은 그 전제가 깨질 때를 대비한
방어를 모의 upstream 으로만 핀한다. 경계 규칙(#135 R)은 그대로다: 게이트웨이 응답은 surrogate 만 `\\udXXX` 로
내보내고, 치환(U+FFFD)은 파일 경계에서만 한다.

  ① context-trigger(JSON·text/plain): 500 없이 200, 감사 줄 1개, JSON `injection`에 원래 surrogate,
     text/plain 본문은 strict utf-8 디코드 가능(surrogate 는 `\\udXXX` 글자열), `x-hc-memory-ids` 헤더는 latin-1 안전.
  ② MCP recall 프루닝: 내부 JSON 의 `\\udXXX` 를 upstream 수준 그대로 남긴다(바깥으로 올리면 mcp SDK 가
     `json_invalid` 로 메시지 전체를 거부한다). 프루닝은 유지되고 `rewrite_failures` 는 0.
  ③ REST recall 프루닝: 조용한 원문 폴백 없이 프루닝이 적용되고 값은 upstream 과 같다.
  ④ upstream 예외 본문(`mcp_tools.py:897` f-string 형태): 응답 바이트가 upstream 과 같다.
  그리고 유효한 문자열의 응답 바이트는 그대로다.
"""
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, UPSTREAM_KEY
from gateway import context_triggers as ct
from gateway import recall_prune
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit
from test_response_honesty import JSON_CT, Upstream, make_client, post, sse

BANK = "personal-alice"
AUTH = {"content-type": "application/json", "authorization": "Bearer tok-alice"}
INPUTS = ["\ud800", "a\udfff"]
INPUT_IDS = ["S1", "S2"]
FIELDS = ["text", "id", "occurred_start", "tags"]
SURROGATE = re.compile("[\ud800-\udfff]")
TS = "2026-09-21T08:55:48+00:00"
PRUNED_KEYS = {"chunk_id", "mentioned_at", "occurred_end", "context"}


def assert_sdk_accepts(data: bytes) -> None:
    """mcp SDK 는 requirements-dev.txt(python>=3.10) 전용이라 3.9 로컬 .venv 에서는 이 헬퍼를 쓰는 테스트가 SKIP 된다."""
    types = pytest.importorskip("mcp.types", reason="mcp 공식 SDK 는 python>=3.10 전용")
    types.JSONRPCMessage.model_validate_json(data)


def escaped(value: str) -> str:
    """응답 경계 표기: surrogate 만 `\\udXXX` 글자열로."""
    return value.encode("utf-8", "backslashreplace").decode("utf-8")


# --------------------------------------------------------------------------
# ① context-trigger
# --------------------------------------------------------------------------

TASK_TEXT = "dash.democloud.example 에 로그인해서 2단계 democloud 설정을 확인해줘"


def trigger_table():
    return ct.TriggerTable.from_dict({"version": 1, "triggers": [{
        "id": "demo-dash-login", "bank": BANK, "tools": ["aside"],
        "all_of": [["democloud", "dash.democloud", "2단계"]],
        "recall": {"query": "대시보드 로그인 절차 - 2단계 코드는 누가 만드나"}}]})


def trigger_memory(field, value):
    mem = {"id": "8ac1598d-b79c-4421-9453-a88c2857b6f2", "text": "aside 샌드박스는 페이지 조작을 한다.",
           "tags": ["kind:gotcha"], "occurred_start": TS, "scores": {"reranker": 0.98, "final": 0.98}}
    if field == "tags":
        mem["tags"] = [value]
    elif field == "occurred_start":
        mem["occurred_start"] = "2026-09-" + value + "T00:00:00"      # _observed_date 가 앞 10자를 쓴다
    else:
        mem[field] = value
    return mem


class RestUpstream:
    """REST recall 에 raw 바이트(JSON 이스케이프 `\\udXXX`)를 돌려주는 모의 upstream."""

    def __init__(self, body: bytes):
        self.body, self.calls = body, 0

    def handler(self, request):
        self.calls += 1
        return httpx.Response(200, content=self.body, headers={"content-type": JSON_CT})


def trigger_client(tmp_path, memory):
    up = RestUpstream(json.dumps({"results": [memory]}).encode("utf-8"))
    http = httpx.AsyncClient(transport=httpx.MockTransport(up.handler), base_url="http://upstream", timeout=5.0)
    log = tmp_path / "audit.log"
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(log)), upstream_key=UPSTREAM_KEY,
                     http_client=http, trigger_table=trigger_table(), notifier=None)
    return TestClient(app, raise_server_exceptions=False), log


def trigger_audit_lines(log):
    lines = [json.loads(ln) for ln in log.read_bytes().decode("utf-8").splitlines()]
    return [ln for ln in lines if ln["decision"] == ct.DECISION]


def call_trigger(client, fmt):
    url = f"/context-triggers/{BANK}" + ("?format=json" if fmt == "json" else "")
    return client.post(url, content=json.dumps({"text": TASK_TEXT, "tool": "aside"}), headers=AUTH)


@pytest.mark.parametrize("fmt", ["json", "text"])
@pytest.mark.parametrize("value", INPUTS, ids=INPUT_IDS)
@pytest.mark.parametrize("field", FIELDS)
def test_context_trigger_survives_surrogate_in_recall_result(tmp_path, field, value, fmt):
    client, log = trigger_client(tmp_path, trigger_memory(field, value))
    resp = call_trigger(client, fmt)
    assert resp.status_code == 200
    audit = trigger_audit_lines(log)
    assert len(audit) == 1 and audit[0]["reason"] == "match:demo-dash-login"
    # injection 은 tags 를 읽지 않는다 - tags 칸은 surrogate 가 응답에 없는 게 정상이다
    in_injection = field != "tags"
    if fmt == "json":
        injection = json.loads(resp.content)["injection"]
        assert (value in injection) is in_injection
        if field == "id":
            assert value[:8] in injection
    else:
        body = resp.content.decode("utf-8")                       # strict
        assert (escaped(value) in body) is in_injection
        assert not SURROGATE.search(body)
        assert body
        want = trigger_memory(field, value)["id"]
        header = resp.headers["x-hc-memory-ids"]
        assert header == want.encode("latin-1", "backslashreplace").decode("latin-1")
    if fmt == "json":
        assert json.loads(resp.content)["memory_ids"] == [trigger_memory(field, value)["id"]]


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_context_trigger_valid_text_bytes_unchanged(tmp_path, fmt):
    mem = trigger_memory("text", "유효한 한글 본문 😀 é")
    client, _ = trigger_client(tmp_path, mem)
    resp = call_trigger(client, fmt)
    assert resp.status_code == 200
    injection, used, _ = ct.build_injection([mem], max_memories=3, max_tokens=500)
    expected = (json.dumps({"matched": True, "trigger_id": "demo-dash-login", "injection": injection,
                            "memory_ids": used, "tokens_est": ct.estimate_tokens(injection),
                            "recall_ok": True}, ensure_ascii=False, separators=(",", ":"))
                if fmt == "json" else injection).encode("utf-8")
    assert resp.content == expected


# --------------------------------------------------------------------------
# ②③ recall 프루닝 (MCP SSE · REST)
# --------------------------------------------------------------------------

def recall_row(field, value):
    row = {"id": "m1", "text": "본문", "tags": ["kind:note"], "occurred_start": TS, "occurred_end": TS,
           "mentioned_at": TS, "fact_type": "world", "document_id": "doc-1",
           "chunk_id": f"{BANK}_doc-1_0", "context": None, "scores": {"final": 1.0}}
    if field == "tags":
        row["tags"] = [value]
    elif field == "occurred_start":
        row.update(occurred_start=value, occurred_end=value, mentioned_at=value)
    else:
        row[field] = value
    return row


def expected_row(field, value):
    return {k: v for k, v in recall_row(field, value).items() if k not in PRUNED_KEYS}


def mcp_recall_call():
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "recall", "arguments": {"query": "x"}}}


def mcp_upstream_frame(inner_text):
    result = {"content": [{"type": "text", "text": inner_text}],
              "structuredContent": {"result": inner_text}, "isError": False}
    return sse({"jsonrpc": "2.0", "id": 1, "result": result})


def data_line(content: bytes) -> bytes:
    lines = [ln[len(b"data:"):].strip() for ln in content.splitlines() if ln.startswith(b"data:")]
    return lines[-1]


@pytest.mark.parametrize("value", INPUTS, ids=INPUT_IDS)
@pytest.mark.parametrize("field", FIELDS)
def test_mcp_recall_prune_keeps_surrogate_at_upstream_json_level(tmp_path, field, value):
    inner = json.dumps({"results": [recall_row(field, value)]})            # 기본값: `\udXXX` 이스케이프
    assert SURROGATE.search(inner) is None
    client = make_client(Upstream(mcp_upstream_frame(inner)), tmp_path)
    resp = post(client, mcp_recall_call(), bank=BANK)
    assert resp.status_code == 200
    data = data_line(resp.content)
    assert_sdk_accepts(data)                                # strict 파서(mcp SDK)가 받는다
    result = json.loads(data)["result"]
    text = result["content"][0]["text"]
    assert result["structuredContent"]["result"] == text
    assert json.loads(text)["results"] == [expected_row(field, value)]     # 프루닝됨 + 값은 upstream 과 같다
    assert client.app.state.rewrite_failures == 0


@pytest.mark.parametrize("value", INPUTS, ids=INPUT_IDS)
@pytest.mark.parametrize("field", FIELDS)
def test_rest_recall_prune_applies_with_surrogate(tmp_path, field, value):
    body = json.dumps({"results": [recall_row(field, value)]}).encode("utf-8")
    client = make_client(Upstream(body, content_type=JSON_CT), tmp_path)
    resp = client.post(f"/v1/default/banks/{BANK}/memories/recall", content=json.dumps({"query": "x"}), headers=AUTH)
    assert resp.status_code == 200
    assert resp.content != body                                            # 원문 폴백이 아니다
    rows = json.loads(resp.content.decode("utf-8"))["results"]
    assert "chunk_id" not in rows[0]
    assert rows == [expected_row(field, value)]


# --------------------------------------------------------------------------
# ④ upstream 예외 본문 형태 (mcp_tools.py:897)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", INPUTS, ids=INPUT_IDS)
def test_upstream_exception_body_passes_through_byte_identical(tmp_path, value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as e:
        inner = f'{{"error": "{e}", "results": []}}'
    frame = mcp_upstream_frame(inner)
    client = make_client(Upstream(frame), tmp_path)
    resp = post(client, mcp_recall_call(), bank=BANK)
    assert resp.status_code == 200
    assert resp.content == frame
    assert_sdk_accepts(data_line(resp.content))
    assert client.app.state.rewrite_failures == 0


# --------------------------------------------------------------------------
# 유효한 문자열은 바이트 동일
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["본문", "plain ascii", "emoji 😀 é", 'quote " back\\slash\n'])
def test_prune_dumps_valid_strings_byte_identical(value):
    payload = {"results": [{"id": "m1", "text": value, "tags": [value]}]}
    old = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert recall_prune._dumps(payload) == old
    assert recall_prune._dumps(payload).encode("utf-8") == old.encode("utf-8")


@pytest.mark.parametrize("value", INPUTS, ids=INPUT_IDS)
def test_estimate_tokens_counts_surrogate_as_three_bytes(value):
    assert ct.estimate_tokens(value) == ct.estimate_tokens(value.replace("\ud800", "�").replace("\udfff", "�"))
