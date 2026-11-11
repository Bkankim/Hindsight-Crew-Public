"""MCP 비 dict params·metadata·원소 거부 (issue #70).

#61 이후에도 같은 계열(비 dict 형식 -> HTTP 500)이 두 곳 남아 있었다.

1. DECIDE 루프의 `(el or {}).get("params") or {}` 다음 `params.get("name")`: `params`가
   비 dict truthy거나 원소·본문 자체가 dict가 아니면 AttributeError(500).
2. `policy.sanitize_arguments`의 팀 뱅크 attribution `dict(args.get("metadata") or {})`:
   `metadata: ["a"]`는 ValueError(500), `["ab"]`는 조용히 `{"a": "b"}`로 바뀐다.

계약:
- `tools/call`의 `params`가 비 dict(null/누락 제외) -> HTTP 200 + isError + `RULE:params-not-object`.
- retain 계열의 `metadata`가 비 dict(null/누락 제외) -> HTTP 200 + isError + `RULE:metadata-not-object`.
  팀 뱅크에서만 500이 났지만 형식 판정이라 뱅크 종류와 무관하게 막는다.
- 원소·본문이 JSON 객체가 아니면 isError를 실을 id가 없으므로 JSON-RPC `-32600 Invalid Request`
  (id null)로 답한다. 감사 사유는 `rule-violation:RULE:request-not-object`.
- 어느 경우든 upstream은 호출하지 않는다. 권한 거부(토큰·ACL)는 형식보다 먼저라 기존 403 그대로다.
"""
import pytest
from fastapi.testclient import TestClient

from gateway import rules
from gateway.bank_policies import BankPolicies, BankPolicy

from test_rules import REPO_POLICY_FILE, audit_entries, post_raw

TOKEN = "tok-alice"
NOW = "2026-10-02T00:00:00Z"
RULE_PARAMS = "RULE:params-not-object"
RULE_METADATA = "RULE:metadata-not-object"
RULE_REQUEST = "RULE:request-not-object"
GOOD_RETAIN = {"content": "[decision] 본문", "tags": ["project:hindsight-crew", "kind:decision"]}


def strict_client(make_app):
    return TestClient(make_app(bank_policies=BankPolicies.from_file(REPO_POLICY_FILE)))


def last_audit(client):
    return audit_entries(client)[-1]


def is_error_text(data):
    assert data["result"]["isError"] is True
    return data["result"]["content"][0]["text"]


# ============================ metadata (2번 결함) ============================

NON_DICT_METADATA = [["a"], ["ab"], "x", 5, True, [], 0, False, ""]


@pytest.mark.parametrize("tool", ["retain", "sync_retain"])
@pytest.mark.parametrize("metadata", NON_DICT_METADATA, ids=repr)
def test_non_dict_metadata_on_team_bank_is_rejected_not_500(make_app, recorder, tool, metadata):
    c = TestClient(make_app(attribution_enabled=True))
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": tool, "arguments": {"content": "team note", "metadata": metadata}}}
    r = post_raw(c, "team-eng", TOKEN, body)

    assert r.status_code == 200
    data = r.json()
    assert data["id"] == 7
    text = is_error_text(data)
    assert text.startswith(RULE_METADATA)
    assert '"metadata": {' in text                       # 올바른 예 1개
    assert recorder.calls == []                          # ["ab"]가 {"a": "b"}로 새어 나가지 않는다
    entry = last_audit(c)
    assert entry["decision"] == "enforce-reject"
    assert entry["reason"] == "rule-violation:" + RULE_METADATA


@pytest.mark.parametrize("metadata", [["a"], "x"], ids=repr)
def test_non_dict_metadata_is_rejected_on_personal_bank_too(make_app, recorder, metadata):
    c = strict_client(make_app)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "retain", "arguments": {**GOOD_RETAIN, "metadata": metadata}}}
    r = post_raw(c, "personal-alice", TOKEN, body)

    assert r.status_code == 200
    assert RULE_METADATA in is_error_text(r.json())
    assert recorder.calls == []


@pytest.mark.parametrize("metadata", [None, {}, {"k": "v"}], ids=repr)
def test_null_or_dict_metadata_keeps_attribution(make_app, recorder, metadata):
    c = TestClient(make_app(attribution_enabled=True))
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "sync_retain", "arguments": {"content": "n", "metadata": metadata}}}
    r = post_raw(c, "team-eng", TOKEN, body)

    assert r.status_code == 200
    md = recorder.last["body"]["params"]["arguments"]["metadata"]
    assert md["hc_member"] == "alice"
    assert {k: v for k, v in md.items() if k != "hc_member"} == (metadata or {})


def test_metadata_is_not_checked_on_non_retain_tools():
    """metadata는 retain 계열의 인자다. 다른 도구는 지금처럼 그대로 둔다."""
    v = rules.evaluate_tool_call("recall", {"query": "q", "metadata": ["a"]}, BankPolicy(), NOW)
    assert RULE_METADATA not in v.rule_ids


# ============================ params (1번 결함) ============================

NON_DICT_PARAMS = [["recall"], "recall", 5, True, [], 0, False, ""]


@pytest.mark.parametrize("params", NON_DICT_PARAMS, ids=repr)
def test_non_dict_params_on_tools_call_is_isError_not_500(make_app, recorder, params):
    c = strict_client(make_app)
    body = {"jsonrpc": "2.0", "id": "req-70", "method": "tools/call", "params": params}
    r = post_raw(c, "personal-alice", TOKEN, body)

    assert r.status_code == 200
    data = r.json()
    assert data["jsonrpc"] == "2.0" and data["id"] == "req-70"
    text = is_error_text(data)
    assert text.startswith(RULE_PARAMS)
    assert '"params": {"name": ' in text                 # 올바른 예 1개
    assert recorder.calls == []
    entry = last_audit(c)
    assert entry["decision"] == "enforce-reject"
    assert entry["reason"] == "rule-violation:" + RULE_PARAMS


def test_non_dict_params_in_batch_aborts_whole_batch(make_app, recorder):
    c = strict_client(make_app)
    body = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "recall", "arguments": {"query": "q"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": ["recall"]}]
    r = post_raw(c, "personal-alice", TOKEN, body)

    assert r.status_code == 200
    first, second = r.json()
    assert first["id"] == 1 and is_error_text(first).startswith("RULE:batch-aborted")
    assert "id=2" in is_error_text(first)
    assert second["id"] == 2 and is_error_text(second).startswith(RULE_PARAMS)
    assert recorder.calls == []


def test_non_dict_params_without_token_is_still_403(make_app, recorder):
    """권한 판정이 형식 판정보다 먼저다(decide FIRST)."""
    c = strict_client(make_app)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["recall"]}
    r = post_raw(c, "personal-alice", "tok-nobody", body)

    assert r.status_code == 403
    assert recorder.calls == []


def test_null_params_on_tools_call_keeps_missing_tool_name_deny(make_app, recorder):
    """null/누락 params는 형식 위반이 아니다. 도구 이름이 없으니 기존대로 missing-tool-name."""
    c = strict_client(make_app)
    for body in ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": None},
                 {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}):
        r = post_raw(c, "personal-alice", TOKEN, body)
        assert r.status_code == 403
        assert last_audit(c)["reason"] == "missing-tool-name"
    assert recorder.calls == []


# ======================= 원소·본문 자체가 비 객체 =======================

def invalid_request(entry):
    assert entry["jsonrpc"] == "2.0" and entry["id"] is None
    assert entry["error"]["code"] == -32600
    assert RULE_REQUEST in entry["error"]["message"]


@pytest.mark.parametrize("body", ["x", 1, True, None, ["recall"]], ids=repr)
def test_non_object_body_is_invalid_request_not_500(make_app, recorder, body):
    c = strict_client(make_app)
    r = post_raw(c, "personal-alice", TOKEN, body)

    assert r.status_code == 200
    data = r.json()
    entries = data if isinstance(body, list) else [data]
    assert len(entries) == 1
    invalid_request(entries[0])
    assert recorder.calls == []
    assert last_audit(c)["reason"] == "rule-violation:" + RULE_REQUEST


def test_non_object_batch_element_aborts_addressable_siblings(make_app, recorder):
    c = strict_client(make_app)
    body = [1, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": "recall", "arguments": {"query": "q"}}}]
    r = post_raw(c, "personal-alice", TOKEN, body)

    assert r.status_code == 200
    first, second = r.json()
    invalid_request(first)
    assert second["id"] == 9
    assert is_error_text(second) == "RULE:batch-aborted (caused by element index=0)"
    assert recorder.calls == []


def test_non_object_body_without_token_is_still_403(make_app, recorder):
    c = strict_client(make_app)
    r = post_raw(c, "personal-alice", "tok-nobody", [1])
    assert r.status_code == 403
    assert recorder.calls == []


# ================= method·params.name 이 문자열이 아님 (리뷰 #2·#3) =================
# 비 hashable 값이면 decide()의 `in frozenset`이 TypeError(500)를 냈다.

RULE_METHOD = "RULE:method-not-string"
RULE_TOOL_NAME = "RULE:tool-name-not-string"


@pytest.mark.parametrize("method", [["tools/call"], {"m": 1}, 5, True], ids=repr)
def test_non_string_method_is_isError_not_500(make_app, recorder, method):
    c = strict_client(make_app)
    r = post_raw(c, "personal-alice", TOKEN, {"jsonrpc": "2.0", "id": 3, "method": method})

    assert r.status_code == 200
    data = r.json()
    assert data["id"] == 3
    assert is_error_text(data).startswith(RULE_METHOD)
    assert recorder.calls == []
    assert last_audit(c)["reason"] == "rule-violation:" + RULE_METHOD


@pytest.mark.parametrize("name", [["recall"], {"n": 1}, 5, True], ids=repr)
def test_non_string_tool_name_is_isError_not_500(make_app, recorder, name):
    c = strict_client(make_app)
    body = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": name, "arguments": {"query": "q"}}}
    r = post_raw(c, "personal-alice", TOKEN, body)

    assert r.status_code == 200
    data = r.json()
    assert data["id"] == 4
    assert is_error_text(data).startswith(RULE_TOOL_NAME)
    assert recorder.calls == []
    assert last_audit(c)["reason"] == "rule-violation:" + RULE_TOOL_NAME


def test_non_string_method_without_token_is_still_403(make_app, recorder):
    c = strict_client(make_app)
    r = post_raw(c, "personal-alice", "tok-nobody", {"jsonrpc": "2.0", "id": 1, "method": ["x"]})
    assert r.status_code == 403
    assert recorder.calls == []


# ===== 이슈 #107-3: id 없는 형식 위반 객체도 조용히 사라지지 않는다 (-32600, id null) =====
# 예전엔 id 없는 객체를 알림으로 보고 건너뛰어 HTTP 200 `[]`가 나갔다. 형식을 어긴 그 객체는
# 알림이 아니라 "응답을 실을 곳이 없는 잘못된 요청"이므로 JSON-RPC 자체 `-32600`을 돌려준다.
# id 있는 요청(isError 결과)과 정상 알림(응답 없음)의 기존 형식은 그대로다.

ID_LESS_OFFENDERS = [
    pytest.param({"jsonrpc": "2.0", "method": ["tools/call"]}, RULE_METHOD, id="method-not-string"),
    pytest.param({"jsonrpc": "2.0", "method": "tools/call",
                  "params": {"name": ["recall"], "arguments": {"query": "q"}}},
                 RULE_TOOL_NAME, id="tool-name-not-string"),
    pytest.param({"jsonrpc": "2.0", "method": "tools/call", "params": ["recall"]},
                 RULE_PARAMS, id="params-not-object"),
    pytest.param({"jsonrpc": "2.0", "id": None, "method": ["tools/call"]},
                 RULE_METHOD, id="explicit-null-id"),
]
NOTIFICATION = {"jsonrpc": "2.0", "method": "notifications/initialized"}
ADDRESSABLE = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
               "params": {"name": "recall", "arguments": {"query": "q"}}}
# 레이아웃: 본문 형태 -> (본문 만들기, 기대 응답 길이). 단일 본문은 배열이 아니라 객체 하나로 답한다.
LAYOUTS = ["single", "batch-alone", "batch-with-notification", "batch-with-addressable"]


def build_body(layout, offender):
    return {"single": offender,
            "batch-alone": [offender],
            "batch-with-notification": [offender, NOTIFICATION],
            "batch-with-addressable": [offender, ADDRESSABLE]}[layout]


def assert_invalid_request(entry, rule):
    assert entry["jsonrpc"] == "2.0" and entry["id"] is None
    assert entry["error"]["code"] == -32600
    assert rule in entry["error"]["message"]


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("offender, rule", ID_LESS_OFFENDERS)
def test_id_less_shape_violation_answers_invalid_request_not_empty(
        make_app, recorder, layout, offender, rule):
    c = strict_client(make_app)
    r = post_raw(c, "personal-alice", TOKEN, build_body(layout, offender))

    assert r.status_code == 200
    data = r.json()
    assert data != []                                          # 조용한 빈 응답이 아니다
    assert recorder.calls == []                                # 형식 위반은 어떤 레이아웃이든 미전달
    assert "rule-violation:" + rule in [e["reason"] for e in audit_entries(c)]   # 형제 줄이 뒤따를 수 있다
    if layout == "single":
        assert isinstance(data, dict)
        assert_invalid_request(data, rule)
        return
    assert isinstance(data, list)
    assert_invalid_request(data[0], rule)
    if layout == "batch-with-addressable":
        # 지목 가능한 형제는 기존 계약대로 batch-aborted 를 받는다 (id 있는 요청 형식 불변)
        assert len(data) == 2
        assert data[1]["id"] == 9
        assert is_error_text(data[1]) == "RULE:batch-aborted (caused by element index=0)"
    else:
        assert len(data) == 1                                  # 정상 알림은 여전히 응답 원소가 없다


@pytest.mark.parametrize("layout", ["single", "batch-alone"])
def test_id_bearing_shape_violation_keeps_its_isError_result(make_app, recorder, layout):
    c = strict_client(make_app)
    offender = {"jsonrpc": "2.0", "id": 3, "method": ["tools/call"]}
    r = post_raw(c, "personal-alice", TOKEN, build_body(layout, offender))

    assert r.status_code == 200
    data = r.json()
    entry = data[0] if layout == "batch-alone" else data
    assert entry["id"] == 3
    assert is_error_text(entry).startswith(RULE_METHOD)
    assert "error" not in entry
    assert recorder.calls == []


@pytest.mark.parametrize("layout", ["single", "batch-alone"])
def test_valid_notification_still_gets_no_response_body(make_app, recorder, layout):
    c = strict_client(make_app)
    r = post_raw(c, "personal-alice", TOKEN, build_body(layout, NOTIFICATION))

    assert r.status_code == 200
    assert len(recorder.calls) == 1                            # 정상 알림은 그대로 upstream 으로 간다
    assert "-32600" not in r.text                              # 형식 위반 응답이 새어 나오지 않는다


# ===== 이슈 #127-1: 잘못된 id 타입은 반사하지 않는다 (유효 id = 문자열, bool 아닌 숫자, null) =====
# 예전엔 `id is None`만 봐서 `{"x":1}`·`[]`·`true`·`false`가 그대로 응답에 실렸다. 유효한 id의 응답
# 형식(isError 결과)은 그대로이고, 그 밖의 id는 id 없음과 같이 `-32600` + `id: null`이다.
# 단일 본문과 배치 원소는 같은 `_shape_rejection`을 타므로 세 레이아웃 모두 핀한다.

BAD_IDS = [
    pytest.param({"x": 1}, id="object"),
    pytest.param([], id="empty-array"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(float("nan"), id="nan"),            # json.loads는 NaN/Infinity를 float으로 받지만 응답에 실을 수 없다
    pytest.param(float("inf"), id="infinity"),
]
GOOD_IDS = [
    pytest.param("req-1", id="string"),
    pytest.param(5, id="int"),
    pytest.param(1.5, id="float"),
]
ID_LAYOUTS = ["single", "batch-alone", "batch-with-addressable"]


def method_offender(req_id):
    return {"jsonrpc": "2.0", "id": req_id, "method": ["tools/call"]}


def split_response(layout, data):
    """(offender 응답, 지목 가능한 형제 응답 또는 None)"""
    if layout == "single":
        assert isinstance(data, dict)
        return data, None
    assert isinstance(data, list)
    if layout == "batch-alone":
        assert len(data) == 1
        return data[0], None
    assert len(data) == 2
    return data[0], data[1]


@pytest.mark.parametrize("layout", ID_LAYOUTS)
@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_wrong_typed_id_is_invalid_request_with_null_id_not_reflected(
        make_app, recorder, layout, bad_id):
    c = strict_client(make_app)
    r = post_raw(c, "personal-alice", TOKEN, build_body(layout, method_offender(bad_id)))

    assert r.status_code == 200
    offender, sibling = split_response(layout, r.json())
    assert_invalid_request(offender, RULE_METHOD)
    assert "result" not in offender
    assert recorder.calls == []
    if sibling is not None:
        # 형제는 기존 계약대로 batch-aborted. 잘못된 id는 이름으로 지목하지 않고 index로 지목한다.
        assert sibling["id"] == 9
        assert is_error_text(sibling) == "RULE:batch-aborted (caused by element index=0)"
    assert "rule-violation:" + RULE_METHOD in [e["reason"] for e in audit_entries(c)]


@pytest.mark.parametrize("layout", ID_LAYOUTS)
@pytest.mark.parametrize("good_id", GOOD_IDS)
def test_valid_id_keeps_the_existing_isError_result_format(make_app, recorder, layout, good_id):
    c = strict_client(make_app)
    r = post_raw(c, "personal-alice", TOKEN, build_body(layout, method_offender(good_id)))

    assert r.status_code == 200
    offender, sibling = split_response(layout, r.json())
    assert set(offender) == {"jsonrpc", "id", "result"}        # error 키 없음: 기존 형식 그대로
    assert offender["jsonrpc"] == "2.0" and offender["id"] == good_id
    assert set(offender["result"]) == {"content", "isError"}
    assert [part["type"] for part in offender["result"]["content"]] == ["text"]
    assert is_error_text(offender).startswith(RULE_METHOD)
    if sibling is not None:
        assert sibling["id"] == 9
        assert is_error_text(sibling) == f"RULE:batch-aborted (caused by element id={good_id})"
    assert recorder.calls == []


@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_wrong_typed_id_on_a_violation_free_sibling_is_a_notification(make_app, recorder, bad_id):
    """잘못된 id는 id 없음과 같다: 위반이 없는 형제는 id 없는 정상 요청처럼 응답 원소가 없다."""
    c = strict_client(make_app)
    sibling = {"jsonrpc": "2.0", "id": bad_id, "method": "tools/call",
               "params": {"name": "recall", "arguments": {"query": "q"}}}
    r = post_raw(c, "personal-alice", TOKEN, [method_offender(3), sibling])

    assert r.status_code == 200
    (first,) = r.json()
    assert first["id"] == 3 and is_error_text(first).startswith(RULE_METHOD)
    assert recorder.calls == []
