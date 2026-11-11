"""MCP tools/call 비 dict arguments 거부 (issue #61).

`params.arguments`가 dict도 아니고 null/누락도 아니면 게이트웨이는 500이 아니라
HTTP 200 + `result.isError=true` + `RULE:arguments-not-object`로 거부하고 upstream을
호출하지 않는다. 감사 줄은 다른 규칙 위반과 같은 `enforce-reject` /
`rule-violation:RULE:arguments-not-object`.

500의 원인: 규칙이 어떤 도구에서는 비 dict를 못 본 채 지나가고(`recall`, 비 retain
도구), 그 뒤 `policy.sanitize_arguments`의 `dict(arguments or {})`가 ValueError를
낸다. retain 계열은 `evaluate_item`이 비 dict를 `{}`로 취급해 엉뚱한 `RULE:tags-required`
를 돌려준다. 입구 가드(`rules.evaluate_tool_call`)가 도구 종류와 무관하게 먼저 막는다.

null / 누락 / `{}`는 정상 경로(인자 없는 도구 호출)라 기존 판정이 그대로여야 한다.
"""
import json

import pytest
from fastapi.testclient import TestClient

from gateway import rules
from gateway.bank_policies import BankPolicies, BankPolicy

from test_rules import REPO_POLICY_FILE, audit_entries, post_raw

BANK, TOKEN = "personal-alice", "tok-alice"
NOW = "2026-10-01T00:00:00Z"
STRICT_POLICY = BankPolicies.from_file(REPO_POLICY_FILE).for_bank(BANK)
RULE = "RULE:arguments-not-object"

TOOLS = ["recall", "retain", "sync_retain", "list_mental_models"]
MISSING = object()      # "arguments" 키 자체를 보내지 않는다


def strict_client(make_app):
    return TestClient(make_app(bank_policies=BankPolicies.from_file(REPO_POLICY_FILE)))


def tools_call(tool, arguments, req_id=1):
    params = {"name": tool}
    if arguments is not MISSING:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": params}


def last_audit(client):
    return audit_entries(client)[-1]


def article(json_type):
    return "an" if json_type in ("array", "object") else "a"


def quoted_example(text):
    """메시지가 인용한 `"arguments": <JSON 객체>; ...` 의 객체를 파싱한다."""
    _, _, tail = text.partition('"arguments": ')
    return json.loads(tail.split("; ", 1)[0])


# ============================ 단위: 입구 가드 ============================

@pytest.mark.parametrize("tool", TOOLS + [None, "unknown_tool"])
@pytest.mark.parametrize("arguments", [["query"], "query", 5, True, 1.5, [], "", 0, False])
def test_guard_rejects_non_dict_for_any_tool_kind(tool, arguments):
    v = rules.evaluate_tool_call(tool, arguments, BankPolicy(), NOW)

    assert not v.ok
    assert v.rule_ids == (RULE,)
    assert v.message.startswith(RULE)


@pytest.mark.parametrize("tool", TOOLS)
def test_guard_does_not_trigger_on_null_or_dict(tool):
    for arguments in (None, {}, {"query": "q"}):
        v = rules.evaluate_tool_call(tool, arguments, BankPolicy(), "2026-10-01T00:00:00Z")
        assert RULE not in v.rule_ids


# ============================ 회귀: 재현 요청 ============================

def test_issue_repro_request_is_isError_200_not_500(make_app, recorder):
    c = strict_client(make_app)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "recall", "arguments": ["query"]}}
    r = post_raw(c, BANK, TOKEN, body)

    assert r.status_code == 200
    data = r.json()
    assert data["jsonrpc"] == "2.0" and data["id"] == 1
    assert data["result"]["isError"] is True
    assert RULE in data["result"]["content"][0]["text"]
    assert recorder.calls == []


# ============================ 행렬: 비 dict x 도구 ============================

NON_DICT = [
    pytest.param(["query"], "array", id="array"),
    pytest.param("query", "string", id="string"),
    pytest.param(5, "number", id="number"),
    pytest.param(True, "boolean", id="true"),
    # 스펙: 예외는 null/누락/{} 뿐이다. 이전엔 `or {}`가 falsy 비 dict를 조용히 {}로 삼켰다.
    pytest.param([], "array", id="empty-array"),
    pytest.param(0, "number", id="zero"),
    pytest.param(False, "boolean", id="false"),
    pytest.param("", "string", id="empty-string"),
]


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("arguments,json_type", NON_DICT)
def test_non_dict_arguments_are_rejected_on_the_isError_channel(
        make_app, recorder, tool, arguments, json_type):
    c = strict_client(make_app)
    r = post_raw(c, BANK, TOKEN, tools_call(tool, arguments, req_id="req-61"))

    assert r.status_code == 200                                  # 500도 403도 아니다
    data = r.json()
    assert isinstance(data, dict)
    assert data["jsonrpc"] == "2.0" and data["id"] == "req-61"   # id 에코
    assert data["result"]["isError"] is True
    text = data["result"]["content"][0]["text"]
    assert text.startswith(RULE)
    assert f"sent {article(json_type)} {json_type}." in text     # 무엇이 틀렸는지(관사 포함)
    assert '"arguments": {' in text                              # 올바른 예 1개
    example = quoted_example(text)
    if tool in rules.RETAIN_TOOLS:
        # retain 계열은 자기 규칙을 통과하는 예를 받는다(content + tags), {"query"}가 아니다.
        assert set(example) == {"content", "tags"}
        assert rules.evaluate_item(example, STRICT_POLICY, NOW).ok
    else:
        # 도구별 필수 인자(#107): recall 은 query, list_mental_models 는 필수 인자 없음 -> {}
        assert example == ({"query": "..."} if tool == "recall" else {})
    assert recorder.calls == []                                  # upstream 호출 0회

    entry = last_audit(c)
    assert entry["decision"] == "enforce-reject"
    assert entry["reason"] == "rule-violation:" + RULE
    assert entry["tool"] == tool


# ====================== 행렬: null / 누락 / {} 는 기존 동작 ======================

@pytest.mark.parametrize("tool", ["retain", "sync_retain"])
@pytest.mark.parametrize("arguments", [None, MISSING, {}], ids=["null", "missing", "empty-object"])
def test_absent_or_empty_arguments_on_retain_keep_existing_rule_verdict(
        make_app, recorder, tool, arguments):
    """retain에 `{}`/null이면 기존 규칙(tags-required 등)이 그대로 걸린다."""
    c = strict_client(make_app)
    r = post_raw(c, BANK, TOKEN, tools_call(tool, arguments))

    assert r.status_code == 200
    data = r.json()
    assert data["result"]["isError"] is True
    text = data["result"]["content"][0]["text"]
    assert text.startswith("RULE:tags-required")
    assert RULE not in text
    assert recorder.calls == []
    assert last_audit(c)["reason"] == "rule-violation:RULE:tags-required,RULE:content-prefix"


@pytest.mark.parametrize("arguments", [None, MISSING, {}], ids=["null", "missing", "empty-object"])
def test_absent_or_empty_arguments_on_recall_are_forwarded(make_app, recorder, arguments):
    c = strict_client(make_app)
    r = post_raw(c, BANK, TOKEN, tools_call("recall", arguments))

    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert len(recorder.calls) == 1
    assert recorder.last["body"]["params"]["arguments"] == {}
    assert last_audit(c)["reason"] == "allow:recall"


@pytest.mark.parametrize("arguments", [None, MISSING, {}], ids=["null", "missing", "empty-object"])
def test_absent_or_empty_arguments_on_list_mental_models_are_forwarded(
        make_app, recorder, arguments):
    c = strict_client(make_app)
    r = post_raw(c, BANK, TOKEN, tools_call("list_mental_models", arguments))

    assert r.status_code == 200
    assert "isError" not in r.json()["result"]
    assert len(recorder.calls) == 1
    # #247: #24(detail 기본값 채우기) 은퇴 - 인자는 그대로 {} 로 전달된다.
    assert recorder.last["body"]["params"]["arguments"] == {}
    assert last_audit(c)["reason"] == "allow:list_mental_models"


@pytest.mark.parametrize("tool,arguments,forwarded", [
    ("recall", {"query": "q"}, {"query": "q"}),
    ("list_mental_models", {"detail": "content"}, {"detail": "content"}),
    ("retain", {"content": "[decision] 본문",
                "tags": ["project:hindsight-crew", "kind:decision"]}, None),
])
def test_dict_arguments_pass_through_unchanged(make_app, recorder, tool, arguments, forwarded):
    c = strict_client(make_app)
    r = post_raw(c, BANK, TOKEN, tools_call(tool, arguments))

    assert r.status_code == 200
    assert len(recorder.calls) == 1
    sent = recorder.last["body"]["params"]["arguments"]
    if forwarded is not None:
        assert sent == forwarded
    else:
        assert sent["content"] == arguments["content"] and sent["tags"] == arguments["tags"]
    assert RULE not in json.dumps(r.json())


# ============== loose 뱅크(BankPolicy() 기본값, 정책 미주입) ==============
# 예전엔 retain에 비 dict를 보내면 evaluate_item이 {}로 읽어 조용히 upstream에 {}를 전달했다.

@pytest.mark.parametrize("tool", ["recall", "retain", "sync_retain", "list_mental_models"])
@pytest.mark.parametrize("arguments,json_type", NON_DICT)
def test_non_dict_arguments_are_rejected_on_a_loose_bank_too(
        make_app, recorder, tool, arguments, json_type):
    c = TestClient(make_app())                                   # bank_policies=None = defaults-only
    r = post_raw(c, BANK, TOKEN, tools_call(tool, arguments))

    assert r.status_code == 200
    data = r.json()
    assert data["result"]["isError"] is True
    text = data["result"]["content"][0]["text"]
    assert text.startswith(RULE)
    if tool in rules.RETAIN_TOOLS:
        assert set(quoted_example(text)) == {"content", "tags"}
    assert recorder.calls == []                                  # {} 가 조용히 전달되지 않는다
    entry = last_audit(c)
    assert entry["decision"] == "enforce-reject"
    assert entry["reason"] == "rule-violation:" + RULE


@pytest.mark.parametrize("arguments,type_name,expected", [
    (["query"], "array", "an array"),
    ([], "array", "an array"),
    ("query", "string", "a string"),
    ("", "string", "a string"),
    (5, "number", "a number"),
    (1.5, "number", "a number"),
    (True, "boolean", "a boolean"),
    (False, "boolean", "a boolean"),
])
def test_message_article_matches_the_type_name(arguments, type_name, expected):
    v = rules.evaluate_tool_call("recall", arguments, BankPolicy(), NOW)

    assert f"the call sent {expected}." in v.message
    assert type_name in expected


# ============================ 배치 ============================

def test_batch_with_only_one_non_dict_element_aborts_the_whole_batch(make_app, recorder):
    c = strict_client(make_app)
    batch = [
        tools_call("recall", {"query": "ok"}, req_id=1),
        tools_call("recall", ["query"], req_id=2),
    ]
    r = post_raw(c, BANK, TOKEN, batch)

    assert r.status_code == 200
    assert recorder.calls == []                                  # 배치 전체 미전달
    data = r.json()
    assert isinstance(data, list) and len(data) == 2             # 배열 -> 배열
    by_id = {el["id"]: el for el in data}
    assert by_id[2]["result"]["isError"] is True
    assert by_id[2]["result"]["content"][0]["text"].startswith(RULE)
    sibling = by_id[1]["result"]
    assert sibling["isError"] is True
    assert sibling["content"][0]["text"] == "RULE:batch-aborted (caused by element id=2)"
    rejects = [e for e in audit_entries(c) if e["decision"] == "enforce-reject"]
    assert [e["reason"] for e in rejects] == ["rule-violation:" + RULE]


# ===== 이슈 #107-5·6: 거부 메시지가 인용하는 `arguments` 예시의 정확성 =====
# 5. retain 계열이 아니면 무조건 {"query": "..."}를 주던 탓에 get_memory(memory_id 필요)에도 틀린 예시가 나갔다.
#    도구별 필수 인자를 반영하고, 스키마를 모르는 도구는 모른다고 밝힌다.
# 6. 뱅크 패턴을 만족하는 내장 예시가 없으면 placeholder 문자열이 예시 content 로 나가 재시도가
#    RULE:content-prefix 로 다시 거부됐다. placeholder 를 내지 않고 예시가 없다고 알린다(_check_content 와 같다).

import os                                                                  # noqa: E402

from gateway import policy as gateway_policy                               # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
_POLICIES = BankPolicies.from_file(REPO_POLICY_FILE)
PLACEHOLDER = "<content matching"
_NO_SCHEMA_PHRASE = "does not know"


def _tools_list_schemas():
    """fixtures/mcp/tools_list.raw (실측 tools/list)의 {도구: required 인자 이름 목록}."""
    raw = open(os.path.join(_REPO_ROOT, "fixtures", "mcp", "tools_list.raw"), encoding="utf-8").read()
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[len("data:"):].strip()
            break
    return {t["name"]: list(t["inputSchema"].get("required") or [])
            for t in json.loads(raw)["result"]["tools"]}


SCHEMAS = _tools_list_schemas()
GATEWAY_TOOLS = sorted(gateway_policy.READ_TOOLS | gateway_policy.CORRECTIVE_TOOLS
                       | gateway_policy.DESTRUCTIVE_TOOLS)


def message_for(tool, policy=None):
    return rules.evaluate_tool_call(tool, ["x"], policy or BankPolicy(), NOW).message


@pytest.mark.parametrize("tool", GATEWAY_TOOLS)
def test_example_names_the_required_arguments_of_every_gateway_tool(tool):
    """게이트웨이가 허용하는 모든 도구: 인용 예시의 키가 실측 스키마의 required 와 같다."""
    text = message_for(tool)

    example = quoted_example(text)
    assert set(example) == set(SCHEMAS[tool])
    assert _NO_SCHEMA_PHRASE not in text


def test_issue_repro_get_memory_is_told_to_send_memory_id_not_query():
    example = quoted_example(message_for("get_memory"))

    assert set(example) == {"memory_id"}
    assert "query" not in example


@pytest.mark.parametrize("tool", [None, "unknown_tool", "list_banks"])
def test_unknown_tool_schema_is_admitted_instead_of_guessed(tool):
    text = message_for(tool)

    assert text.startswith(RULE)
    assert _NO_SCHEMA_PHRASE in text and "tools/list" in text      # 모른다고 밝히고 스키마 출처를 알려 준다
    assert '"arguments": {' not in text                           # 추측한 예시를 인용하지 않는다
    assert '{"query"' not in text


def test_every_gateway_tool_has_a_known_schema_example():
    """도구가 정책 집합에 새로 생기면 예시 표도 같이 늘어야 한다는 드리프트 가드."""
    unknown = [t for t in GATEWAY_TOOLS if _NO_SCHEMA_PHRASE in message_for(t)]
    assert unknown == []


# --- 6: 모든 정책 뱅크 x retain 계열 -----------------------------------------------------

BANKS = sorted(_POLICIES.bank_names())
BANK_POLICIES = [pytest.param(name, id=name) for name in BANKS] + [pytest.param(None, id="defaults")]


def policy_of(name):
    return _POLICIES.defaults if name is None else _POLICIES.for_bank(name)


@pytest.mark.parametrize("tool", sorted(rules.RETAIN_TOOLS))
@pytest.mark.parametrize("bank", BANK_POLICIES)
def test_retain_example_never_quotes_a_placeholder_and_is_accepted_when_given(bank, tool):
    pol = policy_of(bank)
    text = message_for(tool, pol)

    assert PLACEHOLDER not in text                                # 어떤 뱅크도 placeholder 를 예시로 받지 않는다
    if pol.content_prefix_pattern and rules._content_example(pol.content_prefix_pattern) is None:
        assert "No built-in example" in text                      # 예시가 없으면 없다고 알린다
        assert '"arguments": {' not in text
        assert pol.content_prefix_pattern in text
    else:
        example = quoted_example(text)
        assert rules.evaluate_item(example, pol, NOW).ok          # 인용한 예시는 그대로 통과한다


def test_repo_policy_has_a_bank_without_a_builtin_content_example():
    """회귀 재현이 실제 정책 파일에 걸려 있다는 증거: 내장 예시 없는 뱅크가 최소 하나 있다."""
    missing = [n for n in BANKS if policy_of(n).content_prefix_pattern
               and rules._content_example(policy_of(n).content_prefix_pattern) is None]
    assert missing == ["project-demo-gm"]


@pytest.mark.parametrize("tool", sorted(rules.RETAIN_TOOLS))
def test_synthetic_bank_with_unmatchable_content_pattern_reports_no_example(tool):
    pol = BankPolicy(content_prefix_pattern=r"^\[ZZZ-only\] ")
    text = message_for(tool, pol)

    assert PLACEHOLDER not in text
    assert "No built-in example" in text and r"^\[ZZZ-only\] " in text
