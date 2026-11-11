"""한글 이스케이프 해제의 게이트웨이 경계 (issue #23).

규칙 자체는 tests/test_unescape.py 가 고정한다. 여기는 app.py 가 허용 목록 도구에만 적용하는지,
reflect 도 풀리는지(issue #26), 롤백 플래그가 듣는지만 본다.
"""
import json

import pytest

from gateway import unescape
from test_response_honesty import Upstream, make_client, post, sse, sse_payload


def _escaped_result(value):
    blob = json.dumps(value, indent=2)          # upstream 기본값 = ensure_ascii=True
    return {"content": [{"type": "text", "text": blob}], "structuredContent": {"result": blob},
            "isError": False}


def _call(tool, el_id=1, **args):
    return {"jsonrpc": "2.0", "id": el_id, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}


VALUE = {"id": "demo-rules-x", "name": "규칙", "content": "# 금지 표현\n- \"6주 만에\""}


@pytest.mark.parametrize("tool", sorted(unescape.TOOLS))
def test_allowlisted_tool_response_is_unescaped_losslessly(tmp_path, monkeypatch, tool):
    monkeypatch.setenv("HC_KB_READ_TOOLS", "1")      # KB 읽기 도구는 이 플래그가 켜져야 게이트웨이를 통과한다(issue #302)
    upstream = Upstream(sse({"jsonrpc": "2.0", "id": 1, "result": _escaped_result(VALUE)}))
    client = make_client(upstream, tmp_path)
    resp = post(client, _call(tool, mental_model_id="demo-rules-x"))
    result = sse_payload(resp.content)["result"]
    text = result["content"][0]["text"]
    assert "\\u" not in text and "규칙" in text
    assert json.loads(text) == VALUE
    assert result["structuredContent"]["result"] == text
    assert client.get("/healthz").json()["rewrite_failures"] == 0


def test_recall_without_prunable_fields_is_still_unescaped(tmp_path):
    """recall_prune 이 지울 게 없어 원본을 돌려준 경우에도 한글은 풀린다."""
    payload = {"results": [{"id": "m1", "text": "본문", "fact_type": "world"}]}
    upstream = Upstream(sse({"jsonrpc": "2.0", "id": 1, "result": _escaped_result(payload)}))
    resp = post(make_client(upstream, tmp_path), _call("recall", query="x"))
    text = sse_payload(resp.content)["result"]["content"][0]["text"]
    assert "본문" in text and json.loads(text) == payload


def test_reflect_is_unescaped_per_event(tmp_path):
    """#23 은 reflect 를 뺐지만 issue #26 부터 스트리밍을 유지한 채 이벤트 단위로 푼다.
    스트리밍 불변식(첫 ping 선도착)은 tests/test_mcp_streaming.py 가 실 uvicorn 으로 고정한다."""
    value = {"text": "답변"}
    body = sse({"jsonrpc": "2.0", "id": 1, "result": _escaped_result(value)})
    client = make_client(Upstream(body), tmp_path)
    resp = post(client, _call("reflect", query="q", tags=["project:x"]))
    result = sse_payload(resp.content)["result"]
    text = result["content"][0]["text"]
    assert "\\u" not in text and "답변" in text and json.loads(text) == value
    assert result["structuredContent"]["result"] == text
    assert client.get("/healthz").json()["rewrite_failures"] == 0


def test_rollback_flag_passes_bytes_through(tmp_path, monkeypatch):
    monkeypatch.setenv(unescape.ENV_FLAG, "0")
    body = sse({"jsonrpc": "2.0", "id": 1, "result": _escaped_result(VALUE)})
    resp = post(make_client(Upstream(body), tmp_path), _call("get_mental_model", mental_model_id="x"))
    assert resp.content == body


def test_batch_unescapes_only_allowlisted_element(tmp_path):
    body = (sse({"jsonrpc": "2.0", "id": 1, "result": _escaped_result(VALUE)})
            + sse({"jsonrpc": "2.0", "id": 2, "result": _escaped_result({"a": "한"})}))
    upstream = Upstream(body)
    resp = post(make_client(upstream, tmp_path),
                [_call("get_mental_model", 1, mental_model_id="x"), _call("get_bank", 2)])
    frames = [json.loads(ln[len("data:"):]) for ln in resp.content.decode().splitlines()
              if ln.startswith("data:")]
    by_id = {f["id"]: f["result"]["content"][0]["text"] for f in frames}
    assert "규칙" in by_id[1]
    assert "\\ud55c" in by_id[2]                   # get_bank 은 허용 목록 밖
