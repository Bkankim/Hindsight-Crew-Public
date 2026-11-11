"""recall 프루너의 게이트웨이 경계 (issue #13) - chunk_id 형식 파생에 쓰는 bank 는 URL path 로 고정된 bank 다.

프루너 규칙 자체는 tests/test_recall_prune.py 가 고정한다. 여기는 app.py 가 요청의 bank 를 프루너에
넘기는지만 본다(MCP·REST 두 표면).
"""
import json

from test_response_honesty import JSON_CT, Upstream, make_client, post, sse, sse_payload


def _recall_result(chunk_bank):
    row = {"id": "m1", "text": "본문", "fact_type": "world", "document_id": "doc-1",
           "chunk_id": f"{chunk_bank}_doc-1_0", "scores": {"final": 1.0}}
    blob = json.dumps({"results": [row]}, ensure_ascii=False)
    return {"content": [{"type": "text", "text": blob}], "structuredContent": {"result": blob}, "isError": False}


def _mcp_rows(resp):
    return json.loads(sse_payload(resp.content)["result"]["content"][0]["text"])["results"]


def _recall_call():
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "recall", "arguments": {"query": "x"}}}


def test_mcp_recall_drops_chunk_id_rebuildable_from_path_bank(tmp_path):
    upstream = Upstream(sse({"jsonrpc": "2.0", "id": 1, "result": _recall_result("personal-alice")}))
    rows = _mcp_rows(post(make_client(upstream, tmp_path), _recall_call(), bank="personal-alice"))
    assert rows[0]["document_id"] == "doc-1" and "chunk_id" not in rows[0]


def test_rest_recall_drops_chunk_id_rebuildable_from_path_bank(tmp_path):
    body = json.dumps({"results": [json.loads(_recall_result("personal-alice")["content"][0]["text"])["results"][0]]})
    client = make_client(Upstream(body.encode(), content_type=JSON_CT), tmp_path)
    resp = client.post("/v1/default/banks/personal-alice/memories/recall", content=json.dumps({"query": "x"}),
                       headers={"content-type": JSON_CT, "authorization": "Bearer tok-alice"})
    row = resp.json()["results"][0]
    assert row["document_id"] == "doc-1" and "chunk_id" not in row


def test_chunk_id_of_another_bank_is_kept(tmp_path):
    """복원 규칙은 요청 path 의 bank 로만 성립한다 - 다른 bank 형식의 chunk_id 는 그대로 나간다."""
    upstream = Upstream(sse({"jsonrpc": "2.0", "id": 1, "result": _recall_result("personal-bob")}))
    rows = _mcp_rows(post(make_client(upstream, tmp_path), _recall_call(), bank="personal-alice"))
    assert rows[0]["chunk_id"] == "personal-bob_doc-1_0"


def test_rest_chunk_id_of_another_bank_is_kept(tmp_path):
    body = json.dumps({"results": [json.loads(_recall_result("personal-bob")["content"][0]["text"])["results"][0]]})
    client = make_client(Upstream(body.encode(), content_type=JSON_CT), tmp_path)
    resp = client.post("/v1/default/banks/personal-alice/memories/recall", content=json.dumps({"query": "x"}),
                       headers={"content-type": JSON_CT, "authorization": "Bearer tok-alice"})
    assert resp.json()["results"][0]["chunk_id"] == "personal-bob_doc-1_0"
