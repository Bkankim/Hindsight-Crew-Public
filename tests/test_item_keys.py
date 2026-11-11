"""작업 키 추출 모듈 gateway/item_keys.py 와 item_repo_aliases 정책 키 (issue #320).

행렬은 완료 기준 (a)~(d)를 그대로 따른다.
  (a) 태그·본문·별칭·번호만(project 1개/0개/2개) 추출, 숫자 경계, 대소문자, 공백 1개, URL·코드 블록
  (b) is_target 의 조건을 하나씩 뒤집은 행은 False, 모두 맞는 행은 True
  (c) observation·invalidated·같은 문서·늦은 mentioned_at·completion 후보는 False
  (d) 대상 수가 상한을 넘으면 over_cap
꺼짐 불변: item_repo_aliases 가 없는 정책은 빈 표로 로드되고 그 밖의 필드·태그 판정이 추가 전과 같다.
비교 기준은 origin/main 시점 템플릿 렌더본(tests/fixtures/policies/personal-bank-pre-item.json, 커밋된
사본)이다. 실행 중에 git ref 를 읽지 않는다.
"""
from __future__ import annotations

import ast
import json
from dataclasses import fields
from pathlib import Path

import pytest

from gateway import item_keys, rules
from gateway.bank_policies import BankPolicies, BankPolicy
from gateway.item_keys import (
    MAX_TARGETS_PER_COMPLETION,
    RULE_VERSION,
    extract_keys,
    find_targets,
    is_target,
    over_cap,
)

ROOT = Path(__file__).resolve().parents[1]
PRE_ITEM_TEMPLATE = ROOT / "tests" / "fixtures" / "policies" / "personal-bank-pre-item.json"
HC = "hindsight-crew"
ALIASES = {"hc": HC, HC: HC}
K251 = (HC, 251)


# --- (a) 키 추출 ---------------------------------------------------------------------------

@pytest.mark.parametrize("case, content, tags, expect", [
    # 태그 item:<repo>-<n>
    ("tag", "", ["item:hindsight-crew-251"], {K251}),
    ("tag-alias-repo", "", ["item:hc-251"], {K251}),
    ("tag-uppercase-is-folded", "", ["Item:HC-251"], {K251}),
    ("tag-last-dash-number-wins", "", ["item:foo-12-34"], {("foo-12", 34)}),
    ("tag-without-number", "", ["item:hindsight-crew"], set()),
    ("tag-empty-repo-slug", "", ["item:--251"], set()),
    ("tag-number-only", "", ["item:251"], set()),
    ("tag-zero-is-not-an-item", "", ["item:hc-0"], set()),
    ("tag-other-prefix-ignored", "", ["topic:hc-251", "kind:fix-251"], set()),
    ("tag-and-body-dedupe", "[완료] hc#251 머지", ["item:hindsight-crew-251"], {K251}),
    # 본문 <repo>#<n>, <repo> #<n>(공백 1개), 별칭
    ("body-repo", "[완료] hindsight-crew#251 머지", [], {K251}),
    ("body-repo-one-space", "[완료] hindsight-crew #251 머지", [], {K251}),
    ("body-repo-two-spaces", "[완료] hindsight-crew  #251 머지", [], set()),
    ("body-alias-upper", "[완료] HC#251 머지", [], {K251}),
    ("body-alias-lower-space", "[완료] hc #251 머지", [], {K251}),
    ("body-alias-mixed-case-canonical", "[완료] Hindsight-Crew#251", [], {K251}),
    ("body-owner-qualified", "[완료] Bkankim/Hindsight-Crew#251 머지", [], {K251}),
    ("body-owner-form-has-no-space", "[인계] CI/CD #251 남음", ["project:hc"], {K251}),
    ("body-owner-qualified-unknown-repo", "[완료] acme/Other-Repo#7", [], {("other-repo", 7)}),
    ("body-unknown-repo-token-is-not-a-key", "[완료] PR#12 과 foo#9", [], set()),
    ("body-two-repos", "[완료] HC#251, hindsight-crew #300", [], {K251, (HC, 300)}),
    ("body-repo-form-is-not-recounted-as-bare", "[완료] hc #251", ["project:other"], {K251}),
    # 번호만: project 태그가 정확히 하나일 때만
    ("bare-one-project", "[완료] #251 머지", ["project:hindsight-crew"], {K251}),
    ("bare-one-project-alias", "[완료] #251 머지", ["project:hc"], {K251}),
    ("bare-one-project-repeated-tag", "[완료] #251", ["project:hc", "project:hc"], {K251}),
    ("bare-no-project", "[완료] #251 머지", ["kind:completion"], set()),
    ("bare-no-tags", "[완료] #251 머지", None, set()),
    ("bare-two-projects", "[완료] #251 머지", ["project:hc", "project:other"], set()),
    ("bare-other-project", "[완료] #251 머지", ["project:other"], {("other", 251)}),
    ("bare-korean-adjacent", "이슈#251 완료, (#300)", ["project:hc"], {K251, (HC, 300)}),
    # 숫자 경계: #25 가 #251 에 걸리지 않는다, 숫자·영문이 붙으면 키가 아니다
    ("boundary-251-not-25", "[완료] #251", ["project:hc"], {K251}),
    ("boundary-25-not-251", "[완료] #25", ["project:hc"], {(HC, 25)}),
    ("boundary-longer-number", "[완료] #2511", ["project:hc"], {(HC, 2511)}),
    ("boundary-both-numbers", "[완료] HC#251 와 HC#25", [], {K251, (HC, 25)}),
    ("boundary-trailing-letter", "[완료] #251a #25b", ["project:hc"], set()),
    ("boundary-too-many-digits", "[완료] #12345678", ["project:hc"], set()),
    ("boundary-leading-letter", "[완료] x#251", ["project:hc"], set()),
    ("boundary-repo-suffix-letters", "[완료] xhc#251, a-hc#251", [], set()),
    ("boundary-html-entity", "[완료] &#251;", ["project:hc"], set()),
    ("boundary-double-hash", "[완료] ##251", ["project:hc"], set()),
    ("boundary-zero", "[완료] #0", ["project:hc"], set()),
    ("boundary-hex-color", "[완료] #ff0000 #00ff00", ["project:hc"], set()),
    ("boundary-punctuation-around", "[완료] (#251), #300.", ["project:hc"], {K251, (HC, 300)}),
    # 코드 블록·URL 안 번호는 키가 아니다(정책 고정). 인라인 코드는 키다.
    ("url-pull", "[완료] https://github.com/o/hindsight-crew/pull/251 머지", ["project:hc"], set()),
    ("url-issues", "[완료] https://github.com/o/hindsight-crew/issues/251", ["project:hc"], set()),
    ("url-fragment", "[완료] https://github.com/o/hc#251 참고", ["project:hc"], set()),
    ("url-then-text-number", "[완료] https://example.com/a/pull/9 근거, #251", ["project:hc"], {K251}),
    ("url-glued-comma-then-number", "[인계] 근거 https://github.com/o/r/pull/250,#251 남음", ["project:hc"], {K251}),
    ("url-tag-still-counts", "[완료] https://example.com/a/pull/9", ["item:hc-251"], {K251}),
    ("fence-masks-numbers", "[완료] 로그\n```\nfoo #251\nHC#300\n```\n끝", ["project:hc"], set()),
    ("fence-then-text", "```\nx #251\n```\n[완료] #300", ["project:hc"], {(HC, 300)}),
    ("fence-unterminated-masks-rest", "[완료] 앞\n```\nlog #251", ["project:hc"], set()),
    ("fence-longer-masks-inner-short-fence", "````\n```\nx #251\n```\n````\n[완료] #300", ["project:hc"], {(HC, 300)}),
    ("fence-tilde", "~~~\n#251\n~~~", ["project:hc"], set()),
    ("inline-code-counts", "[완료] 이슈 `#251` 머지", ["project:hc"], {K251}),
    ("non-string-content", None, ["item:hc-251"], {K251}),
])
def test_extract_keys(case, content, tags, expect):
    got = extract_keys(content, tags, ALIASES)
    assert got == frozenset(expect), case


def test_extract_keys_alias_table_is_an_argument_only():
    # 표가 비면 별칭 HC 는 알려진 repo 가 아니고, 태그 repo 도 정규화되지 않는다.
    assert extract_keys("HC#251", [], {}) == frozenset()
    assert extract_keys("", ["item:hc-251"], {}) == frozenset({("hc", 251)})
    assert extract_keys("#251", ["project:hc"], {}) == frozenset({("hc", 251)})
    # 표의 키가 대문자로 와도 소문자로 읽는다(호출자 실수에 관대, 변경은 하지 않는다)
    table = {"HC": HC}
    assert extract_keys("hc#251", [], table) == frozenset({K251})
    assert table == {"HC": HC}


# --- (b)(c) 무효화 대상 판정 -----------------------------------------------------------------

def completion(**over):
    base = {"id": "c1", "text": "[완료] hc#251 머지 - 끝", "fact_type": "experience", "state": "valid",
            "tags": ["kind:completion", "project:hindsight-crew", "user:alice"],
            "mentioned_at": "2026-10-30T12:00:00Z", "document_id": "doc-completion"}
    base.update(over)
    return base


def candidate(**over):
    base = {"id": "m1", "text": "[인계] HC#251 다음 단계 - 리뷰 대기", "fact_type": "world", "state": "valid",
            "tags": ["kind:handoff", "project:hindsight-crew", "user:alice"],
            "mentioned_at": "2026-10-29T09:00:00Z", "document_id": "doc-candidate"}
    base.update(over)
    return base


def kind(tag, **over):
    return candidate(tags=[tag, "project:hindsight-crew", "user:alice"], **over)


@pytest.mark.parametrize("case, cand", [
    ("handoff", candidate()),
    ("status", kind("kind:status")),
    ("todo-old-tag", kind("kind:todo")),
    ("decision-label-보류", kind("kind:decision", text="[보류] hc#251 재개 조건 - 리뷰 후")),
    ("decision-label-대기", kind("kind:decision", text="[승인 대기] hc#251 머지 전")),
    ("decision-label-미완", kind("kind:decision", text="[미완] hc#251 남은 일")),
    ("decision-label-재개-대기", kind("kind:decision", text="[재개 대기] hc#251 진행")),
    ("decision-label-완료-대기", kind("kind:decision", text="[완료 대기] hc#251 진행")),
    ("decision-label-대기-중", kind("kind:decision", text="[대기 중] hc#251 진행")),
    ("decision-label-보류-only", kind("kind:decision", text="[보류] hc#251 진행")),
    ("decision-label-미완료", kind("kind:decision", text="[미완료] hc#251 남은 일")),
    ("decision-label-할-일", kind("kind:decision", text="[할 일] hc#251 정리")),
    ("decision-label-할일", kind("kind:decision", text="[할일] hc#251 정리")),
    ("decision-label-todo", kind("kind:decision", text="[TODO] hc#251 정리")),
    ("decision-label-todo-glued-to-hangul", kind("kind:decision", text="[todo정리] hc#251 정리")),
    ("decision-label-pending-glued-to-hangul", kind("kind:decision", text="[pending중] hc#251 정리")),
    ("decision-label-pending", kind("kind:decision", text="[Pending] hc#251 정리")),
    ("fact-type-experience", candidate(fact_type="experience")),
    ("type-key-fallback", {**{k: v for k, v in candidate().items() if k != "fact_type"}, "type": "world"}),
    ("content-key-fallback", {**{k: v for k, v in candidate().items() if k != "text"},
                              "content": "[인계] HC#251 리뷰 대기"}),
    ("link-by-item-tag-only", candidate(text="[인계] 다음 단계 - 리뷰 대기",
                                        tags=["kind:handoff", "project:hindsight-crew", "item:hc-251"])),
    ("link-by-bare-number", candidate(text="[인계] #251 다음 단계")),
    ("shares-one-of-two-projects", candidate(tags=["kind:handoff", "project:other", "project:hindsight-crew"])),
    ("also-carries-other-kind-tags", candidate(tags=["kind:handoff", "kind:ops", "project:hindsight-crew"])),
])
def test_is_target_true_rows(case, cand):
    assert is_target(completion(), cand, ALIASES) is True, case


@pytest.mark.parametrize("case, cand", [
    # 조건 하나씩 뒤집기
    ("no-shared-key", candidate(text="[인계] HC#300 다음 단계")),
    ("no-key-at-all", candidate(text="[인계] 다음 단계 - 리뷰 대기")),
    ("bare-number-needs-single-project", candidate(
        text="[인계] #251 다음 단계", tags=["kind:handoff", "project:hindsight-crew", "project:other"])),
    ("same-key-no-shared-project", candidate(
        text="[인계] hindsight-crew#251 다음 단계", tags=["kind:handoff", "project:other"])),
    ("candidate-without-project", candidate(text="[인계] hc#251", tags=["kind:handoff"])),
    ("kind-gotcha", kind("kind:gotcha")),
    ("kind-fix", kind("kind:fix")),
    ("kind-rule", kind("kind:rule")),
    ("decision-no-pending-label", kind("kind:decision", text="[결정] hc#251 방식 확정 - 예약 방식")),
    ("decision-without-label", kind("kind:decision", text="hc#251 보류 대기 할 일 todo pending")),
    ("decision-label-보류-해제", kind("kind:decision", text="[보류 해제] hc#251 재개 결정")),
    ("decision-label-대기-해소", kind("kind:decision", text="[대기 해소] hc#251 진행")),
    ("decision-label-보류-종료", kind("kind:decision", text="[보류 종료] hc#251 진행")),
    ("decision-label-대기-끝", kind("kind:decision", text="[대기 끝] hc#251 진행")),
    ("decision-label-대기업-동향", kind("kind:decision", text="[대기업 동향] hc#251 진행")),
    ("decision-label-대기업-is-another-word", kind("kind:decision", text="[대기업 협의] hc#251 진행")),
    ("decision-pending-word-only-in-body", kind("kind:decision", text="[결정] hc#251 - 보류 대기 TODO")),
    ("no-kind-tag", candidate(tags=["project:hindsight-crew"])),
    ("candidate-is-completion", candidate(tags=["kind:handoff", "kind:completion", "project:hindsight-crew"])),
    ("candidate-completion-only", kind("kind:completion")),
    ("mentioned-later", candidate(mentioned_at="2026-10-31T00:00:00Z")),
    ("mentioned-equal", candidate(mentioned_at="2026-10-30T12:00:00Z")),
    ("mentioned-equal-other-offset", candidate(mentioned_at="2026-10-30T21:00:00+09:00")),
    ("mentioned-missing", {k: v for k, v in candidate().items() if k != "mentioned_at"}),
    ("mentioned-unparseable", candidate(mentioned_at="어제")),
    ("same-document", candidate(document_id="doc-completion")),
    # (c) 후보 자격
    ("observation", candidate(fact_type="observation")),
    ("observation-via-type-key", {**{k: v for k, v in candidate().items() if k != "fact_type"},
                                   "type": "observation"}),
    ("fact-type-missing", {k: v for k, v in candidate().items() if k != "fact_type"}),
    ("invalidated", candidate(state="invalidated")),
    ("state-missing", {k: v for k, v in candidate().items() if k != "state"}),
    ("archived", candidate(state="archived")),
    ("not-a-dict", None),
])
def test_is_target_false_rows(case, cand):
    assert is_target(completion(), cand, ALIASES) is False, case


def test_is_target_documents_without_ids_are_never_the_same_document():
    comp = completion(document_id=None)
    assert is_target(comp, candidate(document_id=None), ALIASES) is True


def test_is_target_mentioned_at_compares_instants_not_strings():
    # 문자열로는 "2026-10-30T12:00:00Z" < "2026-10-30T20:00:00+09:00" 이지만 실제 시각은 후보가 늦다
    later = candidate(mentioned_at="2026-10-30T22:00:00+09:00")
    assert is_target(completion(), later, ALIASES) is False
    earlier = candidate(mentioned_at="2026-10-30T20:00:00+09:00")        # = 11:00Z
    assert is_target(completion(), earlier, ALIASES) is True
    # 오프셋 없는 시각은 UTC 로 읽는다
    assert is_target(completion(), candidate(mentioned_at="2026-10-30T11:00:00"), ALIASES) is True


def test_is_target_alias_table_is_used_for_both_sides():
    cand = candidate(text="[인계] HC#251 다음 단계")
    assert is_target(completion(text="[완료] hindsight-crew#251"), cand, ALIASES) is True
    assert is_target(completion(text="[완료] hindsight-crew#251"), cand, {}) is False


def test_is_target_inputs_are_not_mutated():
    comp, cand = completion(), candidate()
    before = (json.dumps(comp, sort_keys=True), json.dumps(cand, sort_keys=True))
    is_target(comp, cand, ALIASES)
    assert (json.dumps(comp, sort_keys=True), json.dumps(cand, sort_keys=True)) == before


# --- (d) 상한 -----------------------------------------------------------------------------

def test_constants():
    assert MAX_TARGETS_PER_COMPLETION == 10
    assert isinstance(RULE_VERSION, str) and RULE_VERSION


@pytest.mark.parametrize("count, expect", [(0, False), (9, False), (10, False), (11, True), (50, True)])
def test_over_cap_counts_strictly_more_than_the_cap(count, expect):
    assert over_cap(count) is expect
    assert over_cap([object()] * count) is expect


def test_over_cap_cap_can_be_overridden():
    assert over_cap(3, cap=2) is True and over_cap(2, cap=2) is False


def _many(n, **over):
    return [candidate(id=f"m{i}", document_id=f"doc-{i}", **over) for i in range(n)]


@pytest.mark.parametrize("n, expect_over", [(0, False), (1, False), (10, False), (11, True), (25, True)])
def test_find_targets_flags_over_cap_and_withholds_invalidation(n, expect_over):
    result = find_targets(completion(), _many(n), ALIASES)
    assert len(result.targets) == n and result.over_cap is expect_over
    assert len(result.invalidate) == (0 if expect_over else n)


def test_find_targets_counts_only_real_targets_and_each_memory_once():
    pool = (_many(9) + _many(9)                                                   # 같은 id 중복은 한 번
            + [candidate(id="x1", fact_type="observation"), candidate(id="x2", state="invalidated"),
               candidate(id="x3", text="[인계] HC#999"), kind("kind:gotcha", id="x4")])
    result = find_targets(completion(), pool, ALIASES)
    assert [m["id"] for m in result.targets] == [f"m{i}" for i in range(9)]
    assert result.over_cap is False and result.invalidate == result.targets


def test_find_targets_cap_override_and_order_is_input_order():
    result = find_targets(completion(), _many(4)[::-1], ALIASES, cap=3)
    assert [m["id"] for m in result.targets] == ["m3", "m2", "m1", "m0"]
    assert result.over_cap is True


# --- D8: 순수 모듈 --------------------------------------------------------------------------

def test_item_keys_imports_only_the_standard_library():
    """네트워크·LLM·임베딩 호출이 이 모듈에 없다(D8, #322 결정성). import 를 정적으로 본다."""
    tree = ast.parse(Path(item_keys.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "상대 import 없음: 게이트웨이 다른 모듈에 기대지 않는다"
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "re", "datetime", "dataclasses", "functools", "typing"}, imported


# --- 꺼짐 불변: item_repo_aliases 가 없는 정책 ----------------------------------------------------

def _pre_item_policy(owner="alice"):
    rendered = PRE_ITEM_TEMPLATE.read_text(encoding="utf-8").replace("__OWNER__", owner)
    return BankPolicies.from_dict(json.loads(rendered), source="pre-item-template").for_bank(f"personal-{owner}")


# origin/main 시점 판정(item 추가 전 코드에서 실측한 값). (입력 태그, 전달될 태그, 위반 수)
PRE_ITEM_TAG_VERDICTS = [
    (["kind:decision", "user:alice"], ["kind:decision", "user:alice"], 0),
    (["kind:todo", "user:alice"], ["kind:decision", "user:alice"], 0),                    # 별칭
    (["Kind:Done", "user:alice"], ["kind:completion", "user:alice"], 0),                  # 정규화 + 별칭
    (["topic:Verify", "user:alice"], ["kind:verification", "user:alice"], 0),             # 별칭(topic -> kind)
    (["decision", "user:alice"], ["kind:decision", "user:alice"], 0),                     # 닫힌 목록 맨 단어
    (["kind:bogus", "user:alice"], ["kind:bogus", "user:alice"], 1),                      # 닫힌 목록
    (["kind:decision", "user:demo"], ["kind:decision", "user:demo"], 1),                      # 닫힌 user
    (["kind:decision", "user:alice", "project:Hindsight Crew"],
     ["kind:decision", "user:alice", "project:hindsight-crew"], 0),                      # 정규화
    (["kind:decision", "user:alice", "item:hindsight-crew-251"],
     ["kind:decision", "user:alice", "item:hindsight-crew-251"], 1),                      # item 은 추가 전 정책에서 거부
    (["user:alice"], ["user:alice"], 1),                                                  # kind 필수
]


def test_policy_without_item_repo_aliases_unchanged():
    policy = _pre_item_policy()
    # 새 필드는 빈 표
    assert policy.item_repo_aliases == {}
    # 그 밖의 필드 값: 렌더본 JSON 이 말하는 값 그대로(추가 전 로더 변환 규칙)
    raw = json.loads(PRE_ITEM_TEMPLATE.read_text(encoding="utf-8").replace("__OWNER__", "alice")
                     )["banks"]["personal-alice"]
    expect = {
        "tags_required": True,
        "tag_prefixes": ("project", "kind", "topic", "user"),
        "content_prefix_pattern": raw["content_prefix_pattern"],
        "allowed_strategies": (),
        "blocked_operations": frozenset(),
        "timestamp_autofill": True,
        "recall_max_tokens_floor": 4096,
        "instructions_text": raw["instructions_text"],
        "tag_aliases": raw["tag_aliases"],
        "tag_vocabulary": {p: tuple(v) for p, v in raw["tag_vocabulary"].items()},
        "tag_closed_prefixes": ("kind", "user"),
        "tag_required_prefixes": ("kind",),
        "tag_candidate_prefixes": (),
    }
    got = {f.name: getattr(policy, f.name) for f in fields(BankPolicy) if f.name != "item_repo_aliases"}
    assert got == expect
    assert len(policy.tag_aliases) == 30 and "item" not in policy.tag_prefixes
    # 기존 태그 판정(정규화·별칭·닫힌 목록)
    for sent, forwarded, violations in PRE_ITEM_TAG_VERDICTS:
        verdict = rules.evaluate_tags(sent, policy)
        assert (list(verdict.tags), len(verdict.violations)) == (forwarded, violations), sent


def test_policy_without_item_repo_aliases_unchanged_for_existing_test_policies():
    demo = BankPolicies.from_file(str(ROOT / "tests" / "fixtures" / "policies" / "sample" / "bank-policies.json"))
    tracked = BankPolicies.from_file(str(ROOT / "policies" / "bank-policies.json"))
    assert demo.bank_names() and tracked.bank_names()
    for policies in (demo, tracked):
        assert policies.defaults.item_repo_aliases == {}
        for name in policies.bank_names():
            assert policies.for_bank(name).item_repo_aliases == {}, name
    assert BankPolicy().item_repo_aliases == {} and BankPolicies.defaults_only().defaults.item_repo_aliases == {}
