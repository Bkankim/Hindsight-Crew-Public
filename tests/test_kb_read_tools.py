"""지식 베이스 읽기 도구 허용 (issue #302, HC_KB_READ_TOOLS): 읽기 3개만 플래그로 연다, 쓰기 4개는 계속 거부한다.

fixtures/mcp/kb_*_ok.raw 는 합성 fixture 다(fixtures/mcp/kb_read_tools.note.md). 이 시험이 증명하는 것은 게이트웨이의
허용·거부·감사·바이트 무변조이지 upstream 응답 모양과의 일치가 아니다.

  (a) 플래그가 꺼져 있으면(미설정·"0"·알 수 없는 값) 7개 모두 거부, upstream 0회 - #247 의 거부 계약 그대로
  (c) 켜면 읽기 3개는 허용된 뱅크에서 upstream 으로 가고, 허용 안 된 뱅크는 ACL 거부다. 쓰기 4개는 계속 거부(upstream 0회)
  (d) 허용·거부 모두 감사 로그 줄이 남는다
  + tools/list 는 플래그와 무관하게 upstream 39도구를 그대로 통과시킨다(현행 동작 고정)
"""
import json

import pytest

from gateway import policy
from conftest import call
from test_mcp_handshake import _tools_from, _tools_list_app, _tools_list_req
from test_v1_contract_regression import (BANK, NEW_KNOWLEDGE_TOOLS, TOKEN, Replay, audit_lines, fixture_id, make, mcp,
                                         raw, result_of)

KB_READ = ("get_knowledge_base_tree", "search_knowledge_base", "get_knowledge_page")
KB_WRITE = ("create_knowledge_folder", "create_knowledge_page", "update_knowledge_node", "delete_knowledge_node")
FIXTURE = {"get_knowledge_base_tree": "kb_tree_ok", "search_knowledge_base": "kb_search_ok",
           "get_knowledge_page": "kb_page_ok"}
ARGS = {"get_knowledge_base_tree": {}, "search_knowledge_base": {"query": "현재 상태", "limit": 5},
        "get_knowledge_page": {"page_id": "kp-0001"}, "create_knowledge_folder": {"name": "f"},
        "create_knowledge_page": {"name": "p", "source_query": "q"},
        "update_knowledge_node": {"node_id": "kp-0001", "name": "n"}, "delete_knowledge_node": {"node_id": "kp-0001"}}
TEAM_BANK = "team-eng"                  # alice 가 teams 로 허용된 뱅크
FOREIGN_BANK = "personal-bob"           # alice 의 ACL 밖
FLAG_OFF = (None, "0", "", "off", "garbage")


def _setenv(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("HC_KB_READ_TOOLS", raising=False)
    else:
        monkeypatch.setenv("HC_KB_READ_TOOLS", flag)


def _upstream():
    return Replay({**{t: FIXTURE[t] for t in KB_READ}, "recall": "recall_ok"})


def _expected(flag_on, tool, bank):
    """(HTTP 상태, 감사 reason). 행렬의 기대값은 한 곳에서만 계산한다."""
    if bank == FOREIGN_BANK:
        return 403, f"bank-not-in-acl:{bank}"
    if flag_on and tool in KB_READ:
        return 200, f"allow:{tool}"
    return 403, f"tool-not-whitelisted:{tool}"


# --------------------------------------------------------------------------
# 정책 집합: 읽기 3개는 READ_TOOLS 밖 별도 집합이다
# --------------------------------------------------------------------------

def test_kb_read_tools_are_a_separate_flag_set_outside_the_static_allow_lists():
    assert policy.KB_READ_TOOLS == frozenset(KB_READ)
    static = (policy.DESTRUCTIVE_TOOLS | policy.CORRECTIVE_TOOLS | policy.RETAIN_TOOLS | policy.READ_TOOLS)
    assert not set(NEW_KNOWLEDGE_TOOLS) & static
    assert set(KB_READ) | set(KB_WRITE) == set(NEW_KNOWLEDGE_TOOLS)


@pytest.mark.parametrize("flag,expected", [(None, False), ("0", False), ("", False), ("off", False),
                                           ("garbage", False), ("1", True), ("true", True), ("on", True)])
def test_flag_parsing_defaults_to_off(monkeypatch, flag, expected):
    _setenv(monkeypatch, flag)
    assert policy.kb_read_enabled() is expected


# --------------------------------------------------------------------------
# (a)(c)(d) 행렬: 플래그 x 도구(7) x 뱅크(개인·팀·ACL 밖)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bank", [BANK, TEAM_BANK, FOREIGN_BANK])
@pytest.mark.parametrize("tool", KB_READ + KB_WRITE)
@pytest.mark.parametrize("flag", [None, "0", "1"])
def test_kb_tool_matrix(monkeypatch, tmp_path, flag, tool, bank):
    _setenv(monkeypatch, flag)
    monkeypatch.setenv("HC_UNESCAPE", "0")                                  # 바이트 무변조 단언은 해제를 끈 상태에서 (해제는 아래 전용 시험)
    up = _upstream()
    client, log = make(up, tmp_path)
    resp = mcp(client, tool, ARGS[tool], 7, bank=bank)
    status, reason = _expected(flag == "1", tool, bank)
    assert resp.status_code == status
    lines = [e for e in audit_lines(log) if e.get("tool") == tool]
    assert len(lines) == 1                                                  # (d) 줄이 정확히 하나 남는다
    assert lines[0]["reason"] == reason and lines[0]["attempted_bank"] == bank
    assert lines[0]["decision"] == ("allow" if status == 200 else "deny")
    if status == 200:
        assert [c["body"]["params"]["name"] for c in up.calls] == [tool]    # upstream 으로 정확히 한 번
        assert lines[0]["resolved_bank"] == bank
        assert resp.content == raw(FIXTURE[tool])                           # 응답 바이트 무변조(스트리밍 통과)
    else:
        assert up.calls == []                                               # 거부는 upstream 0회
        assert resp.json()["reason"] == reason


@pytest.mark.parametrize("flag", FLAG_OFF)
@pytest.mark.parametrize("tool", KB_READ + KB_WRITE)
def test_flag_off_variants_keep_all_seven_denied(monkeypatch, tmp_path, flag, tool):
    _setenv(monkeypatch, flag)
    up = _upstream()
    client, log = make(up, tmp_path)
    assert mcp(client, "recall", {"query": "q", "max_tokens": 4096}, fixture_id("recall_ok")).status_code == 200
    before = len(up.calls)
    resp = mcp(client, tool, ARGS[tool], 7)
    assert resp.status_code == 403 and len(up.calls) == before
    denies = [e for e in audit_lines(log) if e.get("tool") == tool]
    assert [(e["decision"], e["reason"]) for e in denies] == [("deny", f"tool-not-whitelisted:{tool}")]


@pytest.mark.parametrize("tool", KB_READ + KB_WRITE)
def test_unknown_token_is_denied_with_the_flag_on(monkeypatch, tmp_path, tool):
    monkeypatch.setenv("HC_KB_READ_TOOLS", "1")
    up = _upstream()
    client, log = make(up, tmp_path)
    assert mcp(client, tool, ARGS[tool], 7, token="tok-nobody").status_code == 403
    assert up.calls == []
    assert [e["reason"] for e in audit_lines(log) if e.get("tool") == tool] == ["unknown-or-missing-token"]


@pytest.mark.parametrize("tool", KB_READ)
def test_kb_read_pins_the_bank_like_other_reads(monkeypatch, tmp_path, tool):
    """upstream 도구는 bank_id 인자를 받는다. 다른 읽기 도구와 같이 클라이언트가 보낸 뱅크 인자는 지워지고 X-Bank-Id 가 고정한다."""
    monkeypatch.setenv("HC_KB_READ_TOOLS", "1")
    up = _upstream()
    client, _ = make(up, tmp_path)
    args = {**ARGS[tool], "bank_id": FOREIGN_BANK, "bank": FOREIGN_BANK}
    assert mcp(client, tool, args, 7).status_code == 200
    sent = up.mcp_args(tool)
    assert sent == [ARGS[tool]]                                             # bank 계열 인자 제거, 나머지 그대로
    assert FOREIGN_BANK not in json.dumps(up.calls)


# --------------------------------------------------------------------------
# tools/list: 현행(통과) 동작 고정 - 플래그가 바꾸지 않는다
# --------------------------------------------------------------------------

@pytest.mark.parametrize("flag", [None, "1"])
def test_tools_list_passes_all_upstream_tools_regardless_of_the_flag(monkeypatch, tmp_path, flag):
    _setenv(monkeypatch, flag)
    upstream_tools = {t["name"]: t for t in result_of("tools_list")["tools"]}
    assert set(NEW_KNOWLEDGE_TOOLS) <= set(upstream_tools) and len(upstream_tools) == 39
    listed = _tools_from(call(_tools_list_app(tmp_path, raw("tools_list")), BANK, TOKEN, raw=_tools_list_req()))
    assert set(listed) == set(upstream_tools)                              # 필터하지 않는다
    for name in NEW_KNOWLEDGE_TOOLS:
        assert listed[name] == upstream_tools[name]                        # 설명·스키마도 그대로


# --------------------------------------------------------------------------
# 킬스위치 배선: 코드 플래그는 compose 배선이 있어야 컨테이너 안에서 끌 수 있다 (2026-09-22 HC_RECALL_PRUNE 실측)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["HC_RECALL_KIND_LABEL", "HC_KB_READ_TOOLS"])
def test_flag_is_wired_in_compose_gateway_environment_with_default_off(name):
    import os
    import re
    compose = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docker-compose.yml")
    with open(compose, encoding="utf-8") as fh:
        text = fh.read()
    assert re.search(rf"^\s+{name}: \$\{{{name}:-0\}}\s*$", text, re.M)


# --------------------------------------------------------------------------
# 한글 \uXXXX 해제 (rework #302-1): 허용된 KB 읽기 응답도 다른 조회 도구처럼 글자로 풀려 나간다
# --------------------------------------------------------------------------

def _korean_escaped(name):
    text = raw(name).decode("utf-8")
    assert "\\u" in text and not any("가" <= ch <= "힣" for ch in text)       # 합성 원본은 한글이 전부 \uXXXX 다
    return text


@pytest.mark.parametrize("tool", KB_READ)
def test_unescape_allowlist_covers_the_kb_read_tools(tool):
    from gateway import unescape
    assert tool in unescape.TOOLS
    assert not set(KB_WRITE) & unescape.TOOLS


@pytest.mark.parametrize("bank", [BANK, TEAM_BANK])
@pytest.mark.parametrize("tool", KB_READ)
@pytest.mark.parametrize("unescape_flag", [None, "1"])
def test_kb_read_response_is_unescaped_when_allowed(monkeypatch, tmp_path, tool, bank, unescape_flag):
    monkeypatch.setenv("HC_KB_READ_TOOLS", "1")
    if unescape_flag is None:
        monkeypatch.delenv("HC_UNESCAPE", raising=False)                    # 기본 ON
    else:
        monkeypatch.setenv("HC_UNESCAPE", unescape_flag)
    original = _korean_escaped(FIXTURE[tool])
    up = _upstream()
    client, log = make(up, tmp_path)
    resp = mcp(client, tool, ARGS[tool], fixture_id(FIXTURE[tool]), bank=bank)   # 응답은 요청 id 로 찾는다
    assert resp.status_code == 200 and len(up.calls) == 1
    assert resp.content != raw(FIXTURE[tool])
    text = resp.content.decode("utf-8")
    assert "\\u" not in text and "프로젝트 현재 상태" in json.dumps(json.loads(text.split("data: ", 1)[1].strip()), ensure_ascii=False)
    # 의미는 그대로: 풀기 전후 안쪽 JSON 값이 같고, 두 벌(content·structuredContent)이 어긋나지 않는다
    before = json.loads(original.split("data: ", 1)[1].strip())["result"]
    after = json.loads(text.split("data: ", 1)[1].strip())["result"]
    assert json.loads(after["content"][0]["text"]) == json.loads(before["content"][0]["text"])
    assert after["structuredContent"]["result"] == after["content"][0]["text"]
    assert [e for e in audit_lines(log) if e["decision"] == "rewrite-failure"] == []


@pytest.mark.parametrize("tool", KB_READ)
def test_kb_read_response_stays_raw_when_unescape_is_off(monkeypatch, tmp_path, tool):
    monkeypatch.setenv("HC_KB_READ_TOOLS", "1")
    monkeypatch.setenv("HC_UNESCAPE", "0")
    client, _ = make(_upstream(), tmp_path)
    assert mcp(client, tool, ARGS[tool], fixture_id(FIXTURE[tool])).content == raw(FIXTURE[tool])


@pytest.mark.parametrize("unescape_flag", [None, "1", "0"])
@pytest.mark.parametrize("tool", KB_READ + KB_WRITE)
def test_flag_off_denial_is_unchanged_by_the_unescape_allowlist(monkeypatch, tmp_path, tool, unescape_flag):
    _setenv(monkeypatch, None)
    if unescape_flag is not None:
        monkeypatch.setenv("HC_UNESCAPE", unescape_flag)
    up = _upstream()
    client, log = make(up, tmp_path)
    resp = mcp(client, tool, ARGS[tool], 7)
    assert resp.status_code == 403 and up.calls == []
    assert [e["reason"] for e in audit_lines(log) if e.get("tool") == tool] == [f"tool-not-whitelisted:{tool}"]


# --------------------------------------------------------------------------
# arguments-not-object 예시 (rework #302-2): 허용된 KB 읽기 도구도 정직한 인자 예시를 인용한다
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tool", KB_READ)
def test_arguments_not_object_example_names_the_required_arguments_of_kb_read_tools(tool):
    from gateway import rules
    from gateway.bank_policies import BankPolicy
    required = {t["name"]: t["inputSchema"].get("required", []) for t in result_of("tools_list")["tools"]}[tool]
    message = rules.evaluate_tool_call(tool, ["x"], BankPolicy(), "2026-10-30T00:00:00Z").message
    assert "does not know the argument names" not in message
    assert json.dumps({name: "..." for name in required}, ensure_ascii=False) in message
