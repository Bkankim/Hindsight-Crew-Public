"""N2: 요청측 규칙 강제 (MCP) — 순수 규칙 함수 + 게이트웨이 배선.

두 층을 나눠 검증한다.

1) `gateway.rules.evaluate_item()` — 단일 item dict(MCP `params.arguments` 또는 N3의
   REST `items[i]`)에 대한 순수 함수. 원본 dict는 절대 변형하지 않는다.
2) `/mcp/<bank>/` 배선 — 순서 계약 **decide → rules → sanitize**. 형식 위반은 403이
   아니라 `CallToolResult isError:true` + `RULE:<id>`(P2), 배치에 위반이 하나라도
   있으면 **업스트림 전량 미전달** + 응답 shape 보존.

느슨한 defaults 뱅크(정책 미주입)는 기존 동작 그대로 통과해야 한다 — timestamp
autofill만 전 뱅크 보편 적용(N1 defaults).
"""
import json
import os
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from conftest import call
from gateway import rules
from gateway.bank_policies import BankPolicies, BankPolicy

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
REPO_POLICY_FILE = os.path.join(REPO_ROOT, "tests", "fixtures", "policies", "sample", "bank-policies.json")

NOW = "2026-07-26T12:00:00Z"

STRICT = BankPolicy(
    tags_required=True,
    tag_prefixes=("project", "kind", "topic", "user"),
    content_prefix_pattern=r"^\[[^\]]{1,12}\] ",
    allowed_strategies=(),          # [] = 어떤 전략명도 불허 (null 아님)
    timestamp_autofill=True,
)
LOOSE = BankPolicy()                # N1 defaults: autofill만 켜짐

# 엄격 프로필을 통과하는 최소 준수 페이로드.
GOOD = {
    "content": "[decision] gateway가 retain 규칙을 서버측에서 강제한다",
    "tags": ["project:hindsight-crew", "kind:decision"],
}


def strict_app(make_app):
    """리포의 실제 정책 파일을 주입한 앱 (personal-alice = 엄격)."""
    return make_app(bank_policies=BankPolicies.from_file(REPO_POLICY_FILE))


def audit_entries(client):
    with open(client.app.state._audit_path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def decisions(client):
    return [e["decision"] for e in audit_entries(client)]


def post_raw(client, bank, token, body):
    return client.post(
        f"/mcp/{bank}/",
        content=json.dumps(body),
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
    )


# ============================ 단위: evaluate_item ============================

def test_missing_timestamp_is_autofilled_with_caller_supplied_now():
    src = dict(GOOD)
    v = rules.evaluate_item(src, STRICT, NOW)
    assert v.ok
    assert v.item["timestamp"] == NOW
    assert v.autofilled_timestamp == NOW


def test_unset_sentinel_is_overwritten():
    """`"unset"`은 upstream 공식 특수값 — 이 경로로 D1-① 재발을 우회할 수 없어야 한다."""
    v = rules.evaluate_item(dict(GOOD, timestamp="unset"), STRICT, NOW)
    assert v.ok
    assert v.item["timestamp"] == NOW
    assert v.autofilled_timestamp == NOW


def test_explicit_timestamp_is_respected():
    v = rules.evaluate_item(dict(GOOD, timestamp="2020-01-02T03:04:05Z"), STRICT, NOW)
    assert v.ok
    assert v.item["timestamp"] == "2020-01-02T03:04:05Z"
    assert v.autofilled_timestamp is None


def test_autofill_disabled_profile_leaves_timestamp_absent():
    pol = BankPolicy(timestamp_autofill=False)
    v = rules.evaluate_item({"content": "x"}, pol, NOW)
    assert v.ok
    assert "timestamp" not in v.item
    assert v.autofilled_timestamp is None


def test_evaluate_item_never_mutates_the_source_dict():
    src = {"content": "raw", "timestamp": "unset", "tags": ["project:x"]}
    snapshot = json.loads(json.dumps(src))
    v = rules.evaluate_item(src, LOOSE, NOW)
    assert src == snapshot                 # 원본 불변
    assert v.item is not src
    assert v.item["timestamp"] == NOW


def test_tags_required_violation_message_carries_example_and_prefixes():
    v = rules.evaluate_item({"content": GOOD["content"]}, STRICT, NOW)
    assert not v.ok
    assert [x.rule for x in v.violations] == ["RULE:tags-required"]
    msg = v.violations[0].message
    assert msg.startswith("RULE:tags-required")
    assert "project:" in msg                             # 유효 예시
    assert "kind" in msg and "topic" in msg              # 허용 prefix 목록(교정 근거)


def test_empty_tag_list_is_tags_required():
    v = rules.evaluate_item(dict(GOOD, tags=[]), STRICT, NOW)
    assert [x.rule for x in v.violations] == ["RULE:tags-required"]


def test_tag_grammar_violation_names_the_offending_tags():
    # 정규화(N2b) 도입 후 "Kind:Bad"·"nocolon"은 기계적으로 고쳐지므로 더 이상 위반이
    # 아니다. 문법 강제 자체가 살아 있음은 고칠 수 없는 값(슬러그가 안 나오는 값)으로
    # 계속 증명한다 — 규칙이 느슨해진 게 아니라 고칠 수 있는 것만 고친다.
    v = rules.evaluate_item(dict(GOOD, tags=["project:ok", "한글", "::"]), STRICT, NOW)
    assert [x.rule for x in v.violations] == ["RULE:tags-grammar"]
    msg = v.violations[0].message
    assert "한글" in msg and "::" in msg
    assert "project:ok" not in msg                       # 통과한 태그는 지적하지 않는다
    assert "project" in msg                              # 허용 prefix 목록


def test_tag_grammar_is_skipped_when_no_prefixes_declared():
    pol = BankPolicy(tags_required=True, tag_prefixes=())
    v = rules.evaluate_item({"content": "c", "tags": ["anything at all"]}, pol, NOW)
    assert v.ok


def test_non_list_tags_is_a_grammar_violation():
    v = rules.evaluate_item(dict(GOOD, tags="project:x"), STRICT, NOW)
    assert [x.rule for x in v.violations] == ["RULE:tags-grammar"]


def test_content_prefix_violation_message_carries_matching_example():
    import re
    v = rules.evaluate_item(dict(GOOD, content="접두 없는 본문"), STRICT, NOW)
    assert [x.rule for x in v.violations] == ["RULE:content-prefix"]
    msg = v.violations[0].message
    assert STRICT.content_prefix_pattern in msg          # 교정 근거(패턴 자체)
    # 메시지가 제시한 예시는 실제로 패턴을 통과해야 한다 (거짓 안내 금지).
    quoted = [seg for seg in msg.split('"') if re.search(STRICT.content_prefix_pattern, seg)]
    assert quoted, msg


def test_missing_content_violates_prefix_rule_when_pattern_declared():
    v = rules.evaluate_item({"tags": GOOD["tags"]}, STRICT, NOW)
    assert [x.rule for x in v.violations] == ["RULE:content-prefix"]


def test_content_prefix_not_checked_when_pattern_is_null():
    v = rules.evaluate_item({"content": "접두 없음"}, LOOSE, NOW)
    assert v.ok


def test_strategy_unknown_when_whitelist_is_empty():
    v = rules.evaluate_item(dict(GOOD, strategy="deep"), STRICT, NOW)
    assert [x.rule for x in v.violations] == ["RULE:strategy-unknown"]
    assert "deep" in v.violations[0].message


def test_strategy_in_whitelist_passes_and_null_whitelist_is_unrestricted():
    pol = BankPolicy(allowed_strategies=("summary", "deep"))
    assert rules.evaluate_item({"content": "c", "strategy": "deep"}, pol, NOW).ok
    bad = rules.evaluate_item({"content": "c", "strategy": "nope"}, pol, NOW)
    assert [x.rule for x in bad.violations] == ["RULE:strategy-unknown"]
    assert "summary" in bad.violations[0].message        # 허용 목록이 교정 근거
    assert rules.evaluate_item({"content": "c", "strategy": "anything"}, LOOSE, NOW).ok


def test_absent_strategy_is_fine_even_with_empty_whitelist():
    assert rules.evaluate_item(dict(GOOD), STRICT, NOW).ok


def test_rejection_text_states_the_timestamp_the_gateway_would_record():
    """거부 시엔 아무것도 저장되지 않으므로, 재시도 기준 시각을 모델에게 알려준다."""
    v = rules.evaluate_item({"content": GOOD["content"]}, STRICT, NOW)
    assert not v.ok
    assert NOW in v.message
    # 위반이 없으면 그 힌트는 붙지 않는다 (성공 주석은 N5 담당).
    assert rules.evaluate_item(dict(GOOD), STRICT, NOW).message == ""


def test_multiple_violations_accumulate():
    v = rules.evaluate_item({"content": "no prefix", "strategy": "deep"}, STRICT, NOW)
    assert {x.rule for x in v.violations} == {
        "RULE:tags-required", "RULE:content-prefix", "RULE:strategy-unknown"
    }


def test_non_retain_tool_passes_unchecked():
    """recall 등은 규칙 대상이 아니다 — 인자를 손대지도, 위반을 만들지도 않는다."""
    args = {"query": "무엇이든"}
    v = rules.evaluate_tool_call("recall", args, STRICT, NOW)
    assert v.ok
    assert v.item == args
    assert v.autofilled_timestamp is None


@pytest.mark.parametrize("tool", sorted(rules.RETAIN_TOOLS))
def test_retain_family_is_checked(tool):
    v = rules.evaluate_tool_call(tool, {"content": "no prefix"}, STRICT, NOW)
    assert not v.ok


# ======================= 통합: /mcp/<bank>/ 배선 ============================

def test_autofill_reaches_upstream_and_is_audited(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = call(c, "personal-alice", "tok-alice", name="sync_retain", arguments=dict(GOOD))
    assert r.status_code == 200
    args = recorder.last["body"]["params"]["arguments"]
    assert args["timestamp"].endswith("Z") and args["timestamp"] != "unset"
    assert args["content"] == GOOD["content"]           # 보정 외 원본 유지
    assert "enforce-autofill" in decisions(c)


def test_unset_timestamp_is_overwritten_on_the_wire(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = call(c, "personal-alice", "tok-alice", name="sync_retain",
             arguments=dict(GOOD, timestamp="unset"))
    assert r.status_code == 200
    assert recorder.last["body"]["params"]["arguments"]["timestamp"] != "unset"


def test_explicit_timestamp_is_not_overwritten_on_the_wire(make_app, recorder):
    c = TestClient(strict_app(make_app))
    call(c, "personal-alice", "tok-alice", name="sync_retain",
         arguments=dict(GOOD, timestamp="2020-01-02T03:04:05Z"))
    assert recorder.last["body"]["params"]["arguments"]["timestamp"] == "2020-01-02T03:04:05Z"
    assert "enforce-autofill" not in decisions(c)


def test_untagged_retain_is_isError_200_with_id_echo(make_app, recorder):
    c = TestClient(strict_app(make_app))
    body = {"jsonrpc": "2.0", "id": "req-77", "method": "tools/call",
            "params": {"name": "sync_retain", "arguments": {"content": GOOD["content"]}}}
    r = post_raw(c, "personal-alice", "tok-alice", body)

    assert r.status_code == 200                                  # 형식 위반은 403이 아니다
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert isinstance(data, dict)                                # 단일 요청 → 단일 객체
    assert data["jsonrpc"] == "2.0" and data["id"] == "req-77"   # id 에코 0오차
    assert data["result"]["isError"] is True
    text = data["result"]["content"][0]["text"]
    assert text.startswith("RULE:tags-required")
    assert recorder.calls == []                                  # 업스트림 미전달
    assert "enforce-reject" in decisions(c)


def test_bad_tag_grammar_is_rejected(make_app, recorder):
    # "Project:Nope"는 이제 project:nope로 정규화되어 통과한다(N2b). 거부 경로는 고칠 수
    # 없는 태그로 검증한다.
    c = TestClient(strict_app(make_app))
    r = call(c, "personal-alice", "tok-alice", name="sync_retain",
             arguments=dict(GOOD, tags=["한글"]))
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == 1                                       # conftest.call 의 기본 id
    assert data["result"]["content"][0]["text"].startswith("RULE:tags-grammar")
    assert recorder.calls == []


def test_batch_with_one_violation_is_not_forwarded_and_keeps_array_shape(make_app, recorder):
    c = TestClient(strict_app(make_app))
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "recall", "arguments": {"query": "ok"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "sync_retain", "arguments": {"content": "no prefix, no tags"}}},
    ]
    r = post_raw(c, "personal-alice", "tok-alice", batch)

    assert r.status_code == 200
    assert recorder.calls == []                                  # 배치 전체 미전달
    data = r.json()
    assert isinstance(data, list) and len(data) == 2             # 배열 → 배열
    by_id = {el["id"]: el for el in data}
    assert by_id[2]["result"]["isError"] is True
    assert "RULE:tags-required" in by_id[2]["result"]["content"][0]["text"]
    sibling = by_id[1]["result"]
    assert sibling["isError"] is True
    assert sibling["content"][0]["text"] == "RULE:batch-aborted (caused by element id=2)"


def test_notification_elements_are_excluded_from_the_rejection_array(make_app, recorder):
    c = TestClient(strict_app(make_app))
    batch = [
        {"jsonrpc": "2.0", "method": "ping"},                    # notification: 응답 없음
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
         "params": {"name": "sync_retain", "arguments": {"content": "bad"}}},
    ]
    r = post_raw(c, "personal-alice", "tok-alice", batch)
    assert r.status_code == 200
    data = r.json()
    assert [el["id"] for el in data] == [9]
    assert recorder.calls == []


def test_notification_only_violation_returns_empty_array(make_app, recorder):
    c = TestClient(strict_app(make_app))
    body = {"jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "sync_retain", "arguments": {"content": "bad"}}}
    r = post_raw(c, "personal-alice", "tok-alice", body)
    assert r.status_code == 200
    assert r.json() == []
    assert recorder.calls == []


def test_loose_bank_keeps_pre_n2_behaviour_with_autofill_only(make_app, recorder):
    """정책 미주입(defaults-only) 뱅크: 무태그·무접두 retain도 그대로 통과."""
    c = TestClient(make_app())
    r = call(c, "personal-alice", "tok-alice", name="sync_retain",
             arguments={"content": "자유 형식", "strategy": "whatever"})
    assert r.status_code == 200
    args = recorder.last["body"]["params"]["arguments"]
    assert args["content"] == "자유 형식"
    assert args["strategy"] == "whatever"
    assert args["timestamp"].endswith("Z")                       # autofill은 전 뱅크
    assert "enforce-reject" not in decisions(c)


def test_unregistered_bank_under_strict_file_is_unaffected(make_app, recorder):
    """정책 파일 미등록 뱅크(personal-bob)는 defaults 폴백 — 강제는 등록된 뱅크에만.

    2026-07-26 이전엔 team-eng이 이 자리였다(옵션 B로 등록되며 교체).
    """
    c = TestClient(strict_app(make_app))
    r = call(c, "personal-bob", "tok-bob", name="sync_retain", arguments={"content": "밥의 노트"})
    assert r.status_code == 200
    args = recorder.last["body"]["params"]["arguments"]
    assert args["content"] == "밥의 노트"                        # 무태그·무접두도 통과
    assert args["timestamp"].endswith("Z")                       # autofill은 전 뱅크
    assert "enforce-reject" not in decisions(c)


# ============ 팀 뱅크 = 태그만 강제 (옵션 B 절충, 2026-07-26 운영자 승인) ============
#
# team-eng은 `tags_required`와 tag 문법만 강제한다. content 접두(`[label] `)는 개인
# 뱅크 전용으로 남는다 — 팀 노트는 작성자·주제가 다양해 12자 라벨 어휘를 공유 강제하면
# 라벨이 의미를 잃기 때문. 정책 파일 한 엔트리로만 달성되어야 하며(코드 무수정),
# 귀속 각인(hc_member)은 규칙 강제와 무관하게 계속 살아 있어야 한다.

def test_team_bank_untagged_retain_is_rejected_by_tag_rule(make_app, recorder):
    c = TestClient(strict_app(make_app))
    body = {"jsonrpc": "2.0", "id": "team-1", "method": "tools/call",
            "params": {"name": "sync_retain", "arguments": {"content": "팀 노트"}}}
    r = post_raw(c, "team-eng", "tok-alice", body)

    assert r.status_code == 200                                  # 형식 위반은 403이 아니다
    data = r.json()
    assert data["id"] == "team-1"
    assert data["result"]["isError"] is True
    assert data["result"]["content"][0]["text"].startswith("RULE:tags-required")
    assert recorder.calls == []                                  # 업스트림 미전달
    assert "enforce-reject" in decisions(c)


def test_team_bank_tag_grammar_is_enforced(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = call(c, "team-eng", "tok-alice", name="sync_retain",
             arguments={"content": "팀 노트", "tags": ["한글"]})   # 정규화 불가 = 여전히 거부
    assert r.status_code == 200
    assert r.json()["result"]["content"][0]["text"].startswith("RULE:tags-grammar")
    assert recorder.calls == []


def test_team_bank_accepts_content_without_prefix_when_tags_are_valid(make_app, recorder):
    """태그**만** 강제 — 개인 뱅크였다면 RULE:content-prefix로 거부됐을 본문이 통과한다."""
    c = TestClient(strict_app(make_app))
    r = call(c, "team-eng", "tok-alice", name="sync_retain",
             arguments={"content": "접두 없는 팀 노트",
                        "tags": ["project:hindsight-crew", "kind:decision"]})
    assert r.status_code == 200
    args = recorder.last["body"]["params"]["arguments"]
    assert args["content"] == "접두 없는 팀 노트"                 # 본문 무변형
    assert args["tags"] == ["project:hindsight-crew", "kind:decision"]
    assert args["timestamp"].endswith("Z")                       # autofill은 여전히 적용
    assert "enforce-reject" not in decisions(c)


def test_team_attribution_coexists_with_tag_enforcement(make_app, recorder):
    """순서 계약(rules → sanitize): 태그 강제를 통과한 뒤에도 귀속 위조는 덮어써진다."""
    c = TestClient(strict_app(make_app))
    r = call(c, "team-eng", "tok-alice", name="sync_retain",
             arguments={"content": "팀 결정 노트",
                        "tags": ["project:hindsight-crew", "kind:decision"],
                        "metadata": {"hc_member": "bob", "keep": 1}})
    assert r.status_code == 200
    md = recorder.last["body"]["params"]["arguments"]["metadata"]
    assert md["hc_member"] == "alice"                             # 위조 덮어쓰기 유지
    assert md["keep"] == 1


def test_recall_on_strict_bank_is_untouched(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = call(c, "personal-alice", "tok-alice", name="recall", arguments={"query": "q"})
    assert r.status_code == 200
    assert recorder.last["body"]["params"]["arguments"] == {"query": "q"}


def test_acl_403_paths_are_unchanged_under_strict_policies(make_app, recorder):
    """인증·ACL·미허가 도구는 여전히 403 (P2: 형식 위반만 isError)."""
    c = TestClient(strict_app(make_app))
    assert call(c, "personal-bob", "tok-alice", name="sync_retain",
                arguments=dict(GOOD)).status_code == 403
    assert call(c, "personal-alice", "tok-nope", name="sync_retain",
                arguments=dict(GOOD)).status_code == 403
    assert call(c, "personal-alice", "tok-alice", name="exfiltrate_everything",
                arguments={}).status_code == 403
    assert recorder.calls == []


def test_rules_run_after_sanitize_inputs_and_before_forward(make_app, recorder):
    """순서 계약: 규칙 통과 후에도 bank 밀반입 인자는 여전히 제거된다."""
    c = TestClient(strict_app(make_app))
    r = call(c, "personal-alice", "tok-alice", name="sync_retain",
             arguments=dict(GOOD, bank="personal-bob", bank_id="team-eng"))
    assert r.status_code == 200
    args = recorder.last["body"]["params"]["arguments"]
    assert "bank" not in args and "bank_id" not in args
    assert args["timestamp"].endswith("Z")


def test_autofill_is_stashed_on_request_state_for_n5(make_app):
    """N5(성공 응답 주석)가 소비할 수 있도록 요청 스코프에 보관만 한다."""
    app = strict_app(make_app)
    seen = {}

    @app.middleware("http")
    async def capture(request, call_next):
        response = await call_next(request)
        seen["autofills"] = list(getattr(request.state, "rule_autofills", []) or [])
        return response

    c = TestClient(app)
    call(c, "personal-alice", "tok-alice", name="sync_retain", arguments=dict(GOOD))
    assert seen["autofills"], "autofill 정보가 request.state에 보관되지 않았다"
    assert seen["autofills"][0]["timestamp"].endswith("Z")
    assert seen["autofills"][0]["tool"] == "sync_retain"


# ============================ 태그 정규화 (N2b) ============================
# 기계적으로 고칠 수 있는 태그 위반은 거부가 아니라 정규화한다. 거부는 왕복을 강요하고,
# 모델이 할 수 있는 유일한 응답이 "같은 태그를 문법에 맞게 다시 쓰는 것"뿐이기 때문이다
# (2026-07-27). 고칠 수 없는 것은 그대로 거부한다 - 정규화는 강제의 구멍이 아니다.

def test_fixable_tags_are_normalized_instead_of_rejected():
    src = {"content": GOOD["content"], "tags": ["skills", "Project:HICKORY Bell", "kind:decision"]}
    v = rules.evaluate_item(src, STRICT, NOW)

    assert v.ok, v.message
    assert v.item["tags"] == ["topic:skills", "project:hickory-bell", "kind:decision"]
    assert v.normalized_tags == (("skills", "topic:skills"),
                                 ("Project:HICKORY Bell", "project:hickory-bell"))
    assert src["tags"] == ["skills", "Project:HICKORY Bell", "kind:decision"]   # 원본 불변


def test_already_legal_tags_are_not_reported_as_normalized():
    v = rules.evaluate_item(dict(GOOD), STRICT, NOW)
    assert v.ok and v.normalized_tags == ()


def test_tag_with_no_ascii_slug_is_still_a_grammar_violation():
    """조용한 삭제 금지: 슬러그가 안 나오는 태그를 버리고 통과시키면 모델은 태그가
    사라진 걸 모른 채 성공으로 읽는다. 정규화가 불가능하면 거부가 정직한 답이다."""
    v = rules.evaluate_item(dict(GOOD, tags=["한글"]), STRICT, NOW)
    assert not v.ok
    assert v.rule_ids == ("RULE:tags-grammar",)
    assert "한글" in v.message


def test_normalization_does_not_apply_to_banks_without_declared_prefixes():
    v = rules.evaluate_item(dict(GOOD, tags=["skills"]), LOOSE, NOW)
    assert v.ok and v.normalized_tags == ()
    assert v.item["tags"] == ["skills"]          # 문법이 없으면 고칠 근거도 없다


def test_normalized_tags_are_forwarded_and_audited(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = call(c, "personal-alice", "tok-alice", name="sync_retain",
             arguments=dict(GOOD, tags=["skills", "kind:decision"]))

    assert r.status_code == 200
    assert recorder.calls, "정규화 가능한 태그는 업스트림까지 가야 한다"
    assert recorder.last["body"]["params"]["arguments"]["tags"] == ["topic:skills", "kind:decision"]
    assert "enforce-normalize" in decisions(c)


def test_prefixless_tag_is_not_filed_under_an_arbitrary_prefix():
    """topic 같은 범용 접두사를 선언하지 않은 뱅크에서는 접두사 없는 태그를 고칠 수 없다.
    첫 번째 선언 접두사에 넣는 건 의미를 지어내는 것이라 정규화의 계약 위반이다."""
    narrow = BankPolicy(tags_required=True, tag_prefixes=("project", "kind"), timestamp_autofill=True)
    v = rules.evaluate_item({"content": "[x] ok", "tags": ["skills"]}, narrow, NOW)

    assert not v.ok
    assert v.rule_ids == ("RULE:tags-grammar",)
    assert v.normalized_tags == ()
    assert v.item["tags"] == ["skills"]          # 손대지 않은 채 거부


def test_prefix_is_still_folded_on_a_bank_without_topic():
    narrow = BankPolicy(tags_required=True, tag_prefixes=("project", "kind"), timestamp_autofill=True)
    v = rules.evaluate_item({"content": "[x] ok", "tags": ["Project:HICKORY Bell"]}, narrow, NOW)
    assert v.ok and v.item["tags"] == ["project:hickory-bell"]


def test_normalization_does_not_forward_a_duplicate_it_created():
    v = rules.evaluate_item(dict(GOOD, tags=["Skills", "skills"]), STRICT, NOW)
    assert v.ok
    assert v.item["tags"] == ["topic:skills"]    # 같은 값을 두 번 저장하지 않는다
    assert v.normalized_tags == (("Skills", "topic:skills"), ("skills", "topic:skills"))


def test_duplicates_the_caller_sent_legally_are_left_alone():
    """정규화가 만들지 않은 중복까지 건드리면 그건 다른 계약 변경이다."""
    v = rules.evaluate_item(dict(GOOD, tags=["project:a", "project:a"]), STRICT, NOW)
    assert v.ok and v.item["tags"] == ["project:a", "project:a"]


def test_unknown_prefix_shape_is_rejected_not_buried_under_topic():
    """"projekt:linden" 는 오타지 topic 이 아니다. topic:projekt-linden 로 묻으면 모델은
    영영 잘못된 접두사로 저장하게 된다 - 거부해야 허용 접두사 목록을 배운다."""
    v = rules.evaluate_item(dict(GOOD, tags=["projekt:hickory-bell"]), STRICT, NOW)

    assert not v.ok
    assert v.rule_ids == ("RULE:tags-grammar",)
    assert v.normalized_tags == ()
    assert "projekt:hickory-bell" in v.message and "project" in v.message


def test_prefixless_tag_with_no_colon_still_falls_back_to_topic():
    v = rules.evaluate_item(dict(GOOD, tags=["skills"]), STRICT, NOW)
    assert v.ok and v.item["tags"] == ["topic:skills"]


def test_declared_prefix_with_messy_tail_is_still_normalized():
    v = rules.evaluate_item(dict(GOOD, tags=["Project:HICKORY Bell"]), STRICT, NOW)
    assert v.ok and v.item["tags"] == ["project:hickory-bell"]


# ============================ 태그 별칭 재작성 (N2c) ============================
# 기계적 정규화가 끝난 형태에 뱅크가 선언한 tag_aliases 를 1홉 적용한다. 순서가 계약이다:
# 기계 정규화 → 별칭 → 문법 검사. 별칭 키가 "정규화 이후 형태"로 선언돼 있기 때문이고
# (bare `aside` 는 topic:aside 로 도착한다), 문법 검사가 마지막이라 별칭 결과도 검사를 받는다.
# 전이 해석은 하지 않는다 - 로더가 값이 다시 키가 되는 선언을 이미 막았다(Todo 1).


def personal_demo_policy():
    """실제 선언(정책 파일)로 검증한다 - 테스트 전용 맵은 배포되는 맵과 갈라진다."""
    return BankPolicies.from_file(REPO_POLICY_FILE).for_bank("personal-demo")


def personal_demo_alias_only_policy():
    """#39 전: personal-demo 에서 kind 필수·닫힌 목록 선언만 걷어낸 사본. 별칭 재작성만 보던 테스트용."""
    return replace(personal_demo_policy(), tag_required_prefixes=(), tag_closed_prefixes=())


def aliased_app(make_app):
    """conftest 에 personal-demo 토큰이 없어, 같은 형상의 별칭 맵을 alice 뱅크에 주입한다."""
    pols = BankPolicies.from_dict({
        "banks": {"personal-alice": {
            "tags_required": True,
            "tag_prefixes": ["project", "kind", "topic", "user"],
            "content_prefix_pattern": r"^\[[^\]]{1,12}\] ",
            "allowed_strategies": [],
            "tag_aliases": {"project:quince": "project:hickory-bell",
                            "topic:aside": "project:aside"},
        }}
    })
    return make_app(bank_policies=pols)


def test_obsolete_tag_is_rewritten_to_its_canonical_form_and_reported():
    v = rules.evaluate_item(dict(GOOD, tags=["project:quince", "kind:decision"]),
                            personal_demo_policy(), NOW)
    assert v.ok, v.message
    assert v.item["tags"] == ["project:hickory-bell", "kind:decision"]
    assert v.normalized_tags == (("project:quince", "project:hickory-bell"),)


def test_bare_tag_is_normalized_then_aliased_within_one_reported_hop():
    """`aside` → topic:aside(기계) → project:cedar-dial(별칭). 보고는 (보낸 값, 전달된 값)이라
    중간 형태는 노출하지 않는다 - 모델이 다음 번에 써야 할 값은 최종형이다."""
    v = rules.evaluate_item(dict(GOOD, tags=["aside"]), personal_demo_alias_only_policy(), NOW)
    assert v.ok, v.message
    assert v.item["tags"] == ["project:cedar-dial"]
    assert v.normalized_tags == (("aside", "project:cedar-dial"),)


def test_bare_policy_tag_crosses_axes_into_kind_decision():
    v = rules.evaluate_item(dict(GOOD, tags=["policy"]), personal_demo_policy(), NOW)
    assert v.ok, v.message
    assert v.item["tags"] == ["kind:decision"]
    assert v.normalized_tags == (("policy", "kind:decision"),)


def test_canonical_tags_pass_through_untouched():
    tags = ["project:hickory-bell", "kind:completion", "topic:memory-vault"]
    v = rules.evaluate_item(dict(GOOD, tags=list(tags)), personal_demo_policy(), NOW)
    assert v.ok and v.item["tags"] == tags
    assert v.normalized_tags == ()


def test_alias_rewrite_does_not_recurse_beyond_one_hop():
    """값이 다시 키인 맵은 로드에서 막히므로(Todo 1) 요청 경로는 정확히 1홉이다.
    여기서는 '한 번만' 적용된다는 사실 자체를 못 박는다."""
    policy = BankPolicy(
        tags_required=True, tag_prefixes=("kind",), timestamp_autofill=True,
        tag_aliases={"kind:done": "kind:completion"},
    )
    v = rules.evaluate_item({"content": "x", "tags": ["kind:done"]}, policy, NOW)
    assert v.item["tags"] == ["kind:completion"]
    assert v.normalized_tags == (("kind:done", "kind:completion"),)


def test_alias_created_duplicate_collapses():
    v = rules.evaluate_item(dict(GOOD, tags=["project:quince", "topic:quince"]),
                            personal_demo_alias_only_policy(), NOW)
    assert v.ok and v.item["tags"] == ["project:hickory-bell"]      # 같은 값 두 번 저장 금지
    assert v.normalized_tags == (("project:quince", "project:hickory-bell"),
                                 ("topic:quince", "project:hickory-bell"))   # 둘 다 보고된다


def test_alias_collapse_is_order_independent():
    """이행기에는 모델이 구/신 태그를 같이 보낸다. 어느 순서든 정규 태그 하나만 남는다.

    이 collapse 는 별칭을 선언한 뱅크에서만 돈다. 별칭 없는 뱅크의 기계 정규화 경로는
    별칭 도입 전과 바이트 동일해야 하고, 그쪽은 ALIAS_FREE_SNAPSHOT 이 못 박는다.
    """
    pol = personal_demo_policy()
    canonical_first = rules.evaluate_item(
        dict(GOOD, tags=["project:hickory-bell", "project:quince"]), pol, NOW)
    obsolete_first = rules.evaluate_item(
        dict(GOOD, tags=["project:quince", "project:hickory-bell"]), pol, NOW)

    assert canonical_first.item["tags"] == ["project:hickory-bell"]
    assert obsolete_first.item["tags"] == ["project:hickory-bell"]
    assert canonical_first.normalized_tags == (("project:quince", "project:hickory-bell"),)
    assert obsolete_first.normalized_tags == (("project:quince", "project:hickory-bell"),)


def test_unfixable_tag_is_still_a_grammar_violation_on_an_aliased_bank():
    """별칭은 강제의 구멍이 아니다 - 문법 검사는 별칭 뒤에도 그대로 돈다."""
    v = rules.evaluate_item(dict(GOOD, tags=["프로브"]), personal_demo_alias_only_policy(), NOW)
    assert not v.ok
    assert v.rule_ids == ("RULE:tags-grammar",)
    assert "프로브" in v.message


# 별칭 맵이 없는 뱅크는 별칭 도입 전과 바이트 동일해야 한다 (스냅샷). 여기 값들은
# a92f1ae 이전 normalize_tags 의 실제 출력이다 - 마지막 케이스가 핵심이다: 재작성이
# 만든 값과 "같은 값이 이미 정상 형태로 도착한" 조합에서 예전 코드는 둘 다 전달했고,
# 별칭 없는 뱅크에서는 그 동작이 그대로여야 한다(순서 독립 collapse 는 별칭 뱅크 전용).
ALIAS_FREE_SNAPSHOT = [
    (["skills"], ["topic:skills"], (("skills", "topic:skills"),)),
    (["Project:HICKORY Bell"], ["project:hickory-bell"], (("Project:HICKORY Bell", "project:hickory-bell"),)),
    (["project:quince"], ["project:quince"], ()),          # 별칭이 없으면 폐기 태그도 그대로
    (["kind:done"], ["kind:done"], ()),
    (["Skills", "skills"], ["topic:skills"],
     (("Skills", "topic:skills"), ("skills", "topic:skills"))),
    (["project:a", "project:a"], ["project:a", "project:a"], ()),   # 발신자 중복은 그대로
    (["project:hindsight-crew", "kind:decision"], ["project:hindsight-crew", "kind:decision"], ()),
    # 재작성이 만든 값 + 뒤늦게 정상 형태로 도착한 같은 값: 예전 코드는 둘 다 전달했다.
    (["Skills", "topic:skills"], ["topic:skills", "topic:skills"],
     (("Skills", "topic:skills"),)),
]


@pytest.mark.parametrize("sent, forwarded, reported", ALIAS_FREE_SNAPSHOT)
def test_bank_without_aliases_keeps_pre_alias_behaviour(sent, forwarded, reported):
    v = rules.evaluate_item(dict(GOOD, tags=list(sent)), STRICT, NOW)
    assert v.item["tags"] == forwarded
    assert v.normalized_tags == reported


def test_loose_bank_without_prefixes_is_untouched_by_aliases():
    v = rules.evaluate_item(dict(GOOD, tags=["project:quince"]), LOOSE, NOW)
    assert v.ok and v.item["tags"] == ["project:quince"] and v.normalized_tags == ()


# --- 배선: 별칭 결과가 업스트림까지 간다 / 위반은 isError 채널 ---------------------

def test_aliased_tags_reach_upstream_canonical_and_are_audited(make_app, recorder):
    c = TestClient(aliased_app(make_app))
    r = call(c, "personal-alice", "tok-alice", name="sync_retain",
             arguments=dict(GOOD, tags=["project:quince", "aside"]))

    assert r.status_code == 200
    assert recorder.calls, "별칭으로 고쳐진 태그는 업스트림까지 가야 한다"
    assert recorder.last["body"]["params"]["arguments"]["tags"] == [
        "project:hickory-bell", "project:aside"]
    assert "enforce-normalize" in decisions(c)


def test_unfixable_tag_on_an_aliased_bank_is_isError_200_and_not_forwarded(make_app, recorder):
    c = TestClient(aliased_app(make_app))
    r = call(c, "personal-alice", "tok-alice", name="sync_retain",
             arguments=dict(GOOD, tags=["프로브"]))

    assert r.status_code == 200                                  # 형식 위반은 422가 아니다
    data = r.json()
    assert data["result"]["isError"] is True
    assert data["result"]["content"][0]["text"].startswith("RULE:tags-grammar")
    assert recorder.calls == []


def test_recall_is_not_alias_rewritten(make_app, recorder):
    """별칭은 retain 계열에만 적용된다 - recall 태그 필터를 몰래 바꾸면 조회 결과가 달라진다."""
    c = TestClient(aliased_app(make_app))
    r = call(c, "personal-alice", "tok-alice", name="recall",
             arguments={"query": "q", "tags": ["project:quince"]})

    assert r.status_code == 200
    assert recorder.last["body"]["params"]["arguments"]["tags"] == ["project:quince"]


# --- initialize instructions: 파생 한 줄 -----------------------------------------

def test_instructions_advertise_alias_rewriting_with_two_examples():
    pol = personal_demo_policy()
    text = rules.instructions_for(pol)

    assert text is not None
    # 선언 순서 앞 2쌍만 렌더링된다. 부분문자열이 아니라 렌더링된 쌍 형태로 세야 한다:
    # "project:hindsight" 는 별칭 키이면서 예시 태그 project:hindsight-crew 의 부분문자열이다.
    assert text.count(" -> ") == 2, "예시는 2쌍만 인용한다 (68개 나열 금지)"
    for old, new in list(pol.tag_aliases.items())[:2]:
        assert f'"{old}" -> "{new}"' in text
    assert len(text.encode("utf-8")) <= 2048 * 2          # 핸드셰이크에 실릴 크기


def test_bank_without_aliases_gets_no_alias_line():
    text = rules.instructions_for(STRICT)
    assert text is not None and " -> " not in text


# ======================= 읽기 인자 가드: recall max_tokens 하한 ==================
# 업스트림 _filter_by_token_budget 은 누적 토큰이 max_tokens 를 넘기는 첫 결과에서
# BREAK 한다(자르지 않는다). 그래서 최상위 메모리 하나보다 작은 예산은 "매칭 없음"과
# 구분되지 않는 빈 결과를 낸다. 조용한 clamp 도, force 우회 플래그도 아닌 요청측 거절.

FLOORED = BankPolicy(recall_max_tokens_floor=4096)
NO_FLOOR = BankPolicy(recall_max_tokens_floor=0)


def test_recall_below_the_floor_is_one_violation_with_the_self_correction_text():
    v = rules.evaluate_tool_call("recall", {"query": "q", "max_tokens": 300}, FLOORED, NOW)

    assert not v.ok
    assert v.rule_ids == (rules.RULE_RECALL_MAX_TOKENS_FLOOR,)
    assert v.rule_ids[0] == "RULE:recall-max-tokens-floor"
    assert v.message.startswith("RULE:recall-max-tokens-floor")
    assert "max_tokens=300" in v.message                 # 무엇이 잘못됐는지
    assert "4096" in v.message                           # 유효 예시 + 하한
    assert "bank_id is ignored" in v.message             # 뱅크 전환은 자가교정이 아니다


def test_recall_at_and_above_the_floor_passes_untouched():
    for budget in (4096, 8192):
        args = {"query": "q", "max_tokens": budget}
        v = rules.evaluate_tool_call("recall", args, FLOORED, NOW)
        assert v.ok, budget
        assert v.item == args and v.item is args         # 손대지 않는다


def test_recall_without_max_tokens_passes():
    """부재는 위반이 아니다. 업스트림 기본값 4096 이 그대로 적용된다."""
    v = rules.evaluate_tool_call("recall", {"query": "q"}, FLOORED, NOW)
    assert v.ok and v.violations == ()


def test_bool_max_tokens_is_not_a_budget():
    """JSON true 는 예산이 아니다. int 서브클래스라고 1 로 읽으면 가드가 조용히 꺼진다."""
    v = rules.evaluate_tool_call("recall", {"query": "q", "max_tokens": True}, FLOORED, NOW)
    assert v.ok


def test_non_numeric_max_tokens_passes_untouched():
    """타입 오류는 업스트림 소관. 예산 훈계로 답하면 모델이 엉뚱한 재시도를 배운다."""
    args = {"query": "q", "max_tokens": "300"}
    v = rules.evaluate_tool_call("recall", args, FLOORED, NOW)
    assert v.ok and v.item == args


def test_float_below_the_floor_is_rejected_too():
    v = rules.evaluate_tool_call("recall", {"query": "q", "max_tokens": 300.5}, FLOORED, NOW)
    assert v.rule_ids == (rules.RULE_RECALL_MAX_TOKENS_FLOOR,)


def test_floor_zero_bank_accepts_a_small_budget():
    """0 = 하한 없음. 저예산 실험용 뱅크는 그대로 통과해야 한다."""
    v = rules.evaluate_tool_call("recall", {"query": "q", "max_tokens": 300}, NO_FLOOR, NOW)
    assert v.ok


@pytest.mark.parametrize("tool", ["get_operation", "list_memories", "list_banks"])
def test_other_read_tools_are_not_budget_checked(tool):
    """가드는 recall 하나에만 붙는다. 비-retain 도구 전반으로 넓히지 않는다."""
    args = {"max_tokens": 1}
    v = rules.evaluate_tool_call(tool, args, FLOORED, NOW)
    assert v.ok and v.item is args


def test_retain_is_unaffected_by_the_recall_guard():
    """retain 계열은 여전히 evaluate_item 이 판정한다(max_tokens 라는 인자도 없다)."""
    v = rules.evaluate_tool_call("retain", dict(GOOD), STRICT, NOW)
    assert v.ok and v.autofilled_timestamp == NOW
    bad = rules.evaluate_tool_call("retain", {"content": "no prefix"}, STRICT, NOW)
    assert bad.rule_ids and rules.RULE_RECALL_MAX_TOKENS_FLOOR not in bad.rule_ids


def test_the_guard_never_mutates_the_callers_dict():
    args = {"query": "q", "max_tokens": 300}
    rules.evaluate_tool_call("recall", args, FLOORED, NOW)
    assert args == {"query": "q", "max_tokens": 300}


def floored_app(make_app):
    """conftest 토큰이 닿는 뱅크에 하한만 선언한 앱 (리포 파일의 alice 는 하한 0)."""
    return make_app(bank_policies=BankPolicies.from_dict({
        "banks": {"personal-alice": {"recall_max_tokens_floor": 4096}}
    }))


def test_low_budget_recall_is_isError_200_and_never_forwarded(make_app, recorder):
    c = TestClient(floored_app(make_app))
    r = call(c, "personal-alice", "tok-alice", name="recall",
             arguments={"query": "q", "max_tokens": 300})

    assert r.status_code == 200                                  # 형식 위반은 422도 403도 아니다
    data = r.json()
    assert data["result"]["isError"] is True
    assert data["result"]["content"][0]["text"].startswith("RULE:recall-max-tokens-floor")
    assert recorder.calls == []                                  # 업스트림에 닿지 않는다
    assert "enforce-reject" in decisions(c)


def test_compliant_recall_still_reaches_upstream_on_a_floored_bank(make_app, recorder):
    c = TestClient(floored_app(make_app))
    r = call(c, "personal-alice", "tok-alice", name="recall",
             arguments={"query": "q", "max_tokens": 4096})

    assert r.status_code == 200
    assert recorder.last["body"]["params"]["arguments"] == {"query": "q", "max_tokens": 4096}


def test_instructions_advertise_the_floor_only_when_one_is_declared():
    with_floor = rules.instructions_for(replace(STRICT, recall_max_tokens_floor=4096))
    without = rules.instructions_for(replace(STRICT, recall_max_tokens_floor=0))

    assert with_floor is not None and "recall: omit max_tokens or send >= 4096" in with_floor
    assert without is not None and "max_tokens" not in without
