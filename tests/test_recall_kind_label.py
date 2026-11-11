"""recall 종류 표시 (issue #302, HC_RECALL_KIND_LABEL): 원문 사실은 "스냅샷", 관찰은 "통합됨".

upstream v1.1.0 원본 바이트(fixtures/mcp/recall_ok.raw, fixtures/rest/recall_gate.json)를 mock upstream 이 그대로
재생하고 게이트웨이 요청 경계(TestClient)에서 단언한다. 표면(MCP·REST) x 프루닝(HC_RECALL_PRUNE) x 플래그 행렬이다.

  (a) 플래그가 꺼져 있으면(미설정·"0"·알 수 없는 값) 응답 바이트가 이 변경 이전과 같다(sha256 고정)
  (b) 켜면 행마다 표시가 붙고 결과 id·순서·기존 필드는 그대로다
  (d) 감사 로그 줄(allow)은 플래그와 무관하게 남는다

표시 위치: 필드 추가(`gateway_kind_label`). 근거는 gateway/recall_label.py 머리말.
"""
import hashlib
import json

import pytest

from test_response_honesty import sse_payload
from test_v1_contract_regression import (BANK, TOKEN, Replay, audit_lines, fixture_id, inner_of, make, mcp,
                                         raw, result_of)

LABEL_KEY = "gateway_kind_label"
SNAPSHOT = "스냅샷(그 시점에 말한 것)"
CONSOLIDATED = "통합됨(현재 상태 후보)"
EXPECTED_LABEL = {"world": SNAPSHOT, "experience": SNAPSHOT, "observation": CONSOLIDATED}
KIND_KEY = {"mcp": "fact_type", "rest": "type"}          # fixtures/mcp/NOTES.md: 행 키가 표면마다 다르다

# 변경 이전(origin/main f2a1c47) 코드가 돌려준 응답 바이트의 sha256. (표면, HC_RECALL_PRUNE) -> 해시.
BASELINE_SHA256 = {
    ("mcp", "1"): "4028d4b285c7dcce23699397edf2e82a983364939141fbb0b147a5dc8bf5c03e",
    ("rest", "1"): "2a2c2952cf47219b1ddfdada20e24c776ee7b515d367ba28264c85b7c6cb929d",
    ("mcp", "0"): "f8b882bdb617d876370280633a3eb100ff0a109d4cb9922e2e4c41c917ee43d9",
    ("rest", "0"): "da0ee46800dfc804ed86efabb4e43d2ec7cf10383e4251708555f317169c1290",
}
SURFACES = ("mcp", "rest")
PRUNE = ("1", "0")


def _recall(monkeypatch, tmp_path, surface, *, prune, flag, unescape="1"):
    """(응답, 감사 로그 경로, mock upstream). flag None = 환경변수 미설정."""
    monkeypatch.setenv("HC_RECALL_PRUNE", prune)
    monkeypatch.setenv("HC_UNESCAPE", unescape)
    if flag is None:
        monkeypatch.delenv("HC_RECALL_KIND_LABEL", raising=False)
    else:
        monkeypatch.setenv("HC_RECALL_KIND_LABEL", flag)
    tmp_path.mkdir(parents=True, exist_ok=True)
    up = Replay({"recall": "recall_ok"})
    client, log = make(up, tmp_path)
    if surface == "mcp":
        resp = mcp(client, "recall", {"query": "q", "max_tokens": 4096}, fixture_id("recall_ok"))
    else:
        resp = client.post(f"/v1/default/banks/{BANK}/memories/recall", content=json.dumps({"query": "q"}),
                           headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    return resp, log, up


def _rows(surface, resp):
    if surface == "rest":
        return resp.json()["results"]
    result = sse_payload(resp.content)["result"]
    assert result["structuredContent"]["result"] == result["content"][0]["text"]     # 두 벌이 어긋나지 않는다
    return inner_of(result)["results"]


def _payload(surface, resp):
    if surface == "rest":
        return resp.json()
    return inner_of(sse_payload(resp.content)["result"])


# --------------------------------------------------------------------------
# 원본 전제 (공허한 통과 방지)
# --------------------------------------------------------------------------

def test_originals_carry_all_three_kinds_under_the_surface_key():
    mcp_kinds = {r["fact_type"] for r in inner_of(result_of("recall_ok"))["results"]}
    from test_v1_contract_regression import rest_recall
    rest_kinds = {r["type"] for r in rest_recall()["response"]["results"]}
    assert mcp_kinds == rest_kinds == {"world", "experience", "observation"}
    assert all(LABEL_KEY not in r for r in inner_of(result_of("recall_ok"))["results"])


# --------------------------------------------------------------------------
# (a) 플래그 꺼짐: 응답 바이트가 지금과 같다
# --------------------------------------------------------------------------

@pytest.mark.parametrize("flag", [None, "0", "", "off", "no", "garbage"])
@pytest.mark.parametrize("prune", PRUNE)
@pytest.mark.parametrize("surface", SURFACES)
def test_flag_off_keeps_the_response_bytes(monkeypatch, tmp_path, surface, prune, flag):
    resp, _, _ = _recall(monkeypatch, tmp_path, surface, prune=prune, flag=flag)
    assert hashlib.sha256(resp.content).hexdigest() == BASELINE_SHA256[(surface, prune)]
    assert LABEL_KEY.encode() not in resp.content


# --------------------------------------------------------------------------
# (b) 플래그 켜짐: 행마다 표시, 집합·순서·기존 필드 불변
# --------------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
@pytest.mark.parametrize("unescape", ["1", "0"])
@pytest.mark.parametrize("prune", PRUNE)
@pytest.mark.parametrize("surface", SURFACES)
def test_flag_on_labels_every_row_and_keeps_ids_and_order(monkeypatch, tmp_path, surface, prune, unescape, flag):
    off, _, _ = _recall(monkeypatch, tmp_path / "off", surface, prune=prune, flag=None, unescape=unescape)
    on, _, _ = _recall(monkeypatch, tmp_path / "on", surface, prune=prune, flag=flag, unescape=unescape)
    off_rows, on_rows = _rows(surface, off), _rows(surface, on)
    assert [r["id"] for r in on_rows] == [r["id"] for r in off_rows]                  # 결과 집합·순서 불변
    assert len(on_rows) == len(off_rows) > 0
    for row_on, row_off in zip(on_rows, off_rows):
        assert row_on[LABEL_KEY] == EXPECTED_LABEL[row_off[KIND_KEY[surface]]]
        assert {k: v for k, v in row_on.items() if k != LABEL_KEY} == row_off          # 기존 필드 불변
    assert {r[LABEL_KEY] for r in on_rows} == {SNAPSHOT, CONSOLIDATED}                 # 두 표시가 모두 나온다
    # 행 밖 최상위 키도 그대로다
    assert {k: v for k, v in _payload(surface, on).items() if k != "results"} == \
           {k: v for k, v in _payload(surface, off).items() if k != "results"}
    assert on.status_code == off.status_code == 200


@pytest.mark.parametrize("surface", SURFACES)
def test_flag_on_keeps_the_mcp_framing_and_kind_field(monkeypatch, tmp_path, surface):
    on, _, _ = _recall(monkeypatch, tmp_path, surface, prune="1", flag="1")
    if surface == "mcp":
        assert on.content.startswith(b"event: message\r\ndata: ")                      # SSE 프레임 유지
        assert on.headers["content-type"].startswith("text/event-stream")
    else:
        assert on.headers["content-type"].startswith("application/json")
    assert all(KIND_KEY[surface] in r for r in _rows(surface, on))                     # 종류 필드는 지우지 않는다


# --------------------------------------------------------------------------
# (d) 감사 로그
# --------------------------------------------------------------------------

@pytest.mark.parametrize("flag", [None, "1"])
@pytest.mark.parametrize("surface", SURFACES)
def test_audit_line_is_written_with_or_without_the_flag(monkeypatch, tmp_path, surface, flag):
    _, log, _ = _recall(monkeypatch, tmp_path, surface, prune="1", flag=flag)
    allows = [e for e in audit_lines(log) if e["decision"] == "allow"]
    assert len(allows) == 1
    assert allows[0]["resolved_bank"] == BANK
    if surface == "mcp":
        assert (allows[0]["tool"], allows[0]["reason"]) == ("recall", "allow:recall")
    else:
        assert allows[0]["reason"] == "rest-allow" and allows[0]["tool"].endswith("/memories/recall")
    assert [e for e in audit_lines(log) if e["decision"] in ("rewrite-failure", "deny")] == []


# --------------------------------------------------------------------------
# recall 이 아닌 응답과 recall 의 실패 응답은 표시하지 않는다
# --------------------------------------------------------------------------

def test_flag_on_leaves_other_tools_untouched(monkeypatch, tmp_path):
    monkeypatch.setenv("HC_RECALL_KIND_LABEL", "1")
    up = Replay({"get_memory": "get_memory_ok"})
    client, _ = make(up, tmp_path)
    resp = mcp(client, "get_memory", {"memory_id": "x"}, fixture_id("get_memory_ok"))
    assert LABEL_KEY.encode() not in resp.content


# --------------------------------------------------------------------------
# 순수 함수 (gateway/recall_label.py)
# --------------------------------------------------------------------------

def test_label_unit_unknown_and_malformed_rows_pass_through():
    from gateway import recall_label
    rows = [{"id": "1", "fact_type": "world"}, {"id": "2", "fact_type": "mystery"}, {"id": "3"}, "x",
            {"id": "4", "fact_type": ["observation"]}]
    out = recall_label.label_payload({"results": rows, "trace": None}, "fact_type")
    assert out["results"][0][LABEL_KEY] == SNAPSHOT
    assert out["results"][1:] == rows[1:]                       # 모르는 종류·종류 없음·비 dict·비 문자열은 그대로
    assert out["trace"] is None and list(out) == ["results", "trace"]
    assert recall_label.label_payload({"results": "nope"}, "fact_type") == {"results": "nope"}
    assert recall_label.label_payload(["x"], "fact_type") == ["x"]


def test_label_unit_does_not_overwrite_an_existing_label():
    from gateway import recall_label
    row = {"id": "1", "fact_type": "world", LABEL_KEY: "upstream"}
    assert recall_label.label_payload({"results": [row]}, "fact_type")["results"] == [row]


def test_label_unit_rest_body_failures_return_the_original_bytes():
    from gateway import recall_label
    for body in (b"", b"not json", b'["x"]', b'{"results": 3}', b"\xff\xfe"):
        assert recall_label.label_rest_body(body) == body


def test_label_unit_error_tool_result_is_untouched():
    from gateway import recall_label
    err = {"isError": True, "content": [{"type": "text", "text": '{"results":[{"fact_type":"world"}]}'}]}
    assert recall_label.label_tool_result(err) == (err, False)
