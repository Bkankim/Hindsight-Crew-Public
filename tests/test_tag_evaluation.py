"""#36: 태그 전용 평가 함수 `rules.evaluate_tags` - 동작 불변 추출의 동등성 고정.

`evaluate_item` 은 태그 부분을 이 함수에 위임한다. 그래서 두 함수를 서로 비교하는
것만으로는 순환이다. 아래 ORACLE 은 추출 전 `evaluate_item` 의 태그 구간(`"tags" in out`
게이트 -> `normalize_tags` -> `out["tags"]` 교체 -> `_check_tags`)을 그대로 옮긴 것이고,
새 함수와 `evaluate_item` 양쪽이 이 기준과 같아야 한다. 그 밖에 리터럴 스냅샷으로
실제 정책 파일(personal-demo 등)의 출력을 못박는다.
"""
import os
from dataclasses import replace

import pytest

from gateway import rules
from gateway.bank_policies import BankPolicies, BankPolicy

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
REPO_POLICY_FILE = os.path.join(REPO_ROOT, "tests", "fixtures", "policies", "sample", "bank-policies.json")

NOW = "2026-07-26T12:00:00Z"
_ABSENT = object()

_FILE_POLICIES = BankPolicies.from_file(REPO_POLICY_FILE)
BANKS = ("personal-demo", "personal-alice", "team-eng", "project-demo-gm", "team-demo")

STRICT = BankPolicy(
    tags_required=True,
    tag_prefixes=("project", "kind", "topic", "user"),
    content_prefix_pattern=r"^\[[^\]]{1,12}\] ",
    allowed_strategies=(),
    timestamp_autofill=True,
)
LOOSE = BankPolicy()
NARROW = BankPolicy(tags_required=True, tag_prefixes=("project", "kind"), timestamp_autofill=True)
OPTIONAL = BankPolicy(tags_required=False, tag_prefixes=("project", "kind", "topic", "user"))
ALIASED = BankPolicies.from_dict({
    "banks": {"aliased": {
        "tags_required": True,
        "tag_prefixes": ["project", "kind", "topic", "user"],
        "tag_aliases": {"project:quince": "project:hickory-bell",
                        "topic:aside": "project:aside",
                        "kind:done": "kind:completion"},
    }}
}).for_bank("aliased")

POLICIES = {name: _FILE_POLICIES.for_bank(name) for name in BANKS}
POLICIES.update({"strict": STRICT, "loose": LOOSE, "narrow": NARROW,
                 "optional": OPTIONAL, "aliased": ALIASED})

# 입력 표본: test_rules.py 의 태그 사례 전부 + 경계 사례. (tags 값 자체를 넣는다)
LIST_CASES = [
    ["project:hindsight-crew", "kind:decision"],                    # 이미 합법
    ["project:ok", "한글", "::"],                                     # 문법 위반 여러 개
    ["한글"], ["프로브"], ["::"], [""], ["   "], [":"], ["project:"],
    ["skills"], ["Skills", "skills"], ["Skills", "topic:skills"],     # 대소문자 / 중복
    ["Project:HICKORY Bell"], ["  project:x  "], ["project:UPPER"],      # 공백 / 대문자
    ["skills", "Project:HICKORY Bell", "kind:decision"],
    ["projekt:hickory-bell"], ["Projekt:X"],                             # prefix 오타
    ["project:a", "project:a"],                                       # 발신자 중복
    ["project:quince"], ["project:quince", "kind:decision"],            # alias
    ["project:quince", "topic:quince"], ["project:quince", "project:hickory-bell"],
    ["project:hickory-bell", "project:quince"],
    ["aside"], ["topic:aside"], ["policy"], ["kind:done"], ["done"],
    ["project:hickory-bell", "kind:completion", "topic:memory-vault"],   # canonical
    ["project:myall-shore, kind:gotcha, topic:blog"],          # 합쳐진 문자열
    ["kind:gotcha topic:orchestration"],
    ["topic:Deploy Pipeline"], ["kind:a_b/c"],
    ["case:sa-0001", "lifecycle:open"], ["case:SA-0001", "severity:High"],
    ["anything at all"], ["a", "b", "c"],
    ["project:x", 5], [None], [5], [["project:x"]], [{"a": 1}],       # 비문자열 원소
    ["skills", None, "kind:decision"],
    [],                                                               # 빈 배열
    ("skills", "kind:decision"), ("project:x",), (),                  # tuple
]
NON_LIST_CASES = [
    "project:x", "", "skills", 5, 0, True, {"a": 1}, 1.5,             # 배열이 아님
    None,                                                             # 명시적 null
]
CASES = [pytest.param(v, id=repr(v)[:60]) for v in LIST_CASES + NON_LIST_CASES + [_ABSENT]]


def oracle(tags, policy):
    """추출 전 evaluate_item 의 태그 구간. 반환: (전달될 tags 또는 _ABSENT, normalized, violations)."""
    out = {} if tags is _ABSENT else {"tags": tags}
    normalized = ()
    if "tags" in out:
        fixed, normalized = rules.normalize_tags(out, policy)
        if normalized:
            out["tags"] = fixed
    violations = tuple(rules._check_tags(out, policy))
    return out.get("tags", _ABSENT), normalized, violations


@pytest.mark.parametrize("tags", CASES)
@pytest.mark.parametrize("name", list(POLICIES))
def test_evaluate_tags_matches_pre_extraction_behaviour(name, tags):
    policy = POLICIES[name]
    want_tags, want_norm, want_viol = oracle(tags, policy)

    got = rules.evaluate_tags(None if tags is _ABSENT else tags, policy)

    assert got.normalized_tags == want_norm
    assert got.violations == want_viol
    assert got.warnings == ()
    if want_tags is not _ABSENT:
        assert got.tags == want_tags


@pytest.mark.parametrize("tags", CASES)
@pytest.mark.parametrize("name", list(POLICIES))
def test_evaluate_item_tag_outcome_equals_evaluate_tags(name, tags):
    policy = POLICIES[name]
    item = {"content": "[decision] x"} if tags is _ABSENT else {"content": "[decision] x", "tags": tags}
    want_tags, want_norm, want_viol = oracle(tags, policy)

    v = rules.evaluate_item(item, policy, NOW)
    tag_only = rules.evaluate_tags(None if tags is _ABSENT else tags, policy)

    assert v.normalized_tags == want_norm == tag_only.normalized_tags
    # evaluate_item 의 위반 = 태그 위반 + (content, strategy). 태그 위반은 앞에 온다.
    assert v.violations[:len(want_viol)] == want_viol == tag_only.violations
    assert all(x.rule not in (rules.RULE_TAGS_REQUIRED, rules.RULE_TAGS_GRAMMAR)
               for x in v.violations[len(want_viol):])
    if want_tags is _ABSENT:
        assert "tags" not in v.item
    else:
        assert v.item["tags"] == want_tags


def test_evaluate_tags_never_mutates_the_input():
    tags = ["Skills", "Project:HICKORY Bell", "kind:decision"]
    before = list(tags)
    got = rules.evaluate_tags(tags, STRICT)
    assert tags == before
    assert got.tags == ["topic:skills", "project:hickory-bell", "kind:decision"]
    assert got.tags is not tags


def test_evaluate_item_leaves_tags_object_alone_without_rewrites():
    tags = ("project:x", "kind:decision")          # 합법 tuple: 재작성이 없으면 그대로
    v = rules.evaluate_item({"content": "[a] b", "tags": tags}, STRICT, NOW)
    assert v.item["tags"] is tags and v.normalized_tags == ()


# --- 리터럴 스냅샷: 실제 정책 파일의 출력 -------------------------------------------

def _tags(bank, sent):
    r = rules.evaluate_tags(sent, POLICIES[bank])
    return r.tags, r.normalized_tags, tuple(v.rule for v in r.violations)


def _alias_only(sent):
    """#39 전 모양: personal-demo 에서 kind 필수·닫힌 목록 선언만 걷어낸 사본의 출력."""
    policy = replace(POLICIES["personal-demo"], tag_required_prefixes=(), tag_closed_prefixes=())
    r = rules.evaluate_tags(sent, policy)
    return r.tags, r.normalized_tags, tuple(v.rule for v in r.violations)


def test_snapshot_personal_demo_alias_and_normalization():
    assert _alias_only(["aside"]) == (["project:cedar-dial"], (("aside", "project:cedar-dial"),), ())
    assert _alias_only(["policy"]) == (["kind:decision"], (("policy", "kind:decision"),), ())
    assert _alias_only(["project:quince", "kind:decision"]) == (
        ["project:hickory-bell", "kind:decision"], (("project:quince", "project:hickory-bell"),), ())
    assert _alias_only(["프로브"]) == (["프로브"], (), ("RULE:tags-grammar",))


@pytest.mark.parametrize("bank", ["personal-alice", "team-eng"])
def test_snapshot_alias_free_banks_keep_mechanical_path(bank):
    # 같은 prefix 를 선언했어도 어휘/alias 가 없으면 한 slug 로 저장되는 기존 계약 그대로.
    assert _tags(bank, ["kind:gotcha topic:orchestration"]) == (
        ["kind:gotcha-topic-orchestration"],
        (("kind:gotcha topic:orchestration", "kind:gotcha-topic-orchestration"),), ())
    assert _tags(bank, ["project:quince"]) == (["project:quince"], (), ())
    assert _tags(bank, ["Skills", "topic:skills"]) == (
        ["topic:skills", "topic:skills"], (("Skills", "topic:skills"),), ())


def test_snapshot_shapes_of_missing_and_malformed_tags():
    assert _tags("personal-alice", None) == (None, (), ("RULE:tags-required",))
    assert _tags("personal-alice", []) == ([], (), ("RULE:tags-required",))
    assert _tags("personal-alice", "project:x") == ("project:x", (), ("RULE:tags-grammar",))
    assert _tags("team-demo", ["anything at all"]) == (["anything at all"], (), ())
    assert _tags("team-demo", None) == (None, (), ())


def test_snapshot_project_demogm_gm_own_prefixes():
    assert _tags("project-demo-gm", ["Case:SA-0001", "severity:High"]) == (
        ["case:sa-0001", "severity:high"],
        (("Case:SA-0001", "case:sa-0001"), ("severity:High", "severity:high")), ())
    assert _tags("project-demo-gm", ["skills"]) == (["skills"], (), ("RULE:tags-grammar",))
