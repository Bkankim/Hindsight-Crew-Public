"""작업 키 규칙 v2.1 (issue #410, R7-5): 열거 접두 전파 + 문장 단일 명시 리포. v1·v2 는 그대로.

v2.1 은 v2 위에 두 가지만 더한다.
  * 열거 접두 전파: 명시 리포 접두(알려진 리포 이름·별칭·`owner/repo`) 뒤에 `·`·`,`·`/`·공백(과 번호 바로 뒤 괄호 설명)으로
    이어지는 `#N` 열거는 접두 리포 키다. 다른 명시 접두나 문장 끝에서 멈춘다.
  * 문장 단일 명시 리포: 같은 문장에 명시 리포가 정확히 하나면 접두 없는 `#N` 은 그 리포 키, 둘 이상이면 키가 아니다.
    명시 리포가 없으면 v2 와 같다(project 태그).

동작이 바뀌는 경우(CHANGE)는 v2.1 진입점으로 통과하고 v2 경로로는 단언이 실패해야 한다. 그 red 는 `HC_R410_RED=1` 로
이 파일을 돌리면 CHANGE 경우가 v2 경로로 바뀌어 AssertionError 로 재현된다(완료 기준의 red 기록용). 같은 표의 `v2` 열은
v2 경로의 지금 결과를 고정한다(v2 바이트 불변 가드). 문장은 모두 합성이다(실제 번호·본문 아님).
"""
from __future__ import annotations

import os

import pytest

from gateway import item_keys, item_roles
from gateway.item_keys import extract_keys

HC = "hindsight-crew"
AR = "alpha-repo"
ALIASES = {"hc": HC, HC: HC, "ar": AR, AR: AR}
P = ["project:hindsight-crew"]
RED = os.environ.get("HC_R410_RED") == "1"
NEW = "v2" if RED else "v2.1"


def keys(path, content, tags, aliases=ALIASES):
    if path == "v1":
        return extract_keys(content, tags, aliases)
    if path == "v2":
        return item_keys.extract_keys_v2(content, tags, aliases)
    return item_keys.extract_keys_v21(content, tags, aliases)


def hc(*ns):
    return {(HC, n) for n in ns}


def ar(*ns):
    return {(AR, n) for n in ns}


# (경우, 본문, 태그, v2.1 기대, v2 지금 결과)
CHANGES = [
    # 열거 접두 전파
    ("enum-middot", "[완료] ar #3·#4·#5 머지", P, ar(3, 4, 5), ar(3) | hc(4, 5)),
    ("enum-comma", "[완료] ar #3, #4 머지", P, ar(3, 4), ar(3) | hc(4)),
    ("enum-slash", "[완료] ar#3/#4 머지", P, ar(3, 4), ar(3)),
    ("enum-space", "[완료] ar #3 #4 머지", P, ar(3, 4), ar(3) | hc(4)),
    ("enum-owner-prefix", "[완료] org/alpha-repo#3·#4", P, ar(3, 4), ar(3) | hc(4)),
    ("enum-stops-at-other-prefix", "[진행] ar #3·#4, hc #7·#8 남음", P, ar(3, 4) | hc(7, 8), ar(3) | hc(4, 7, 8)),
    ("enum-stops-at-sentence-end", "[진행] ar #3·#4 끝. #9 남음", P, ar(3, 4) | hc(9), ar(3) | hc(4, 9)),
    ("enum-stops-at-newline", "[진행] ar #3·#4 끝\n#9 남음", P, ar(3, 4) | hc(9), ar(3) | hc(4, 9)),
    ("enum-paren-note", "[인계] ar #3·#4(설명), #5 와 hc #7 남음", P, ar(3, 4, 5) | hc(7), ar(3) | hc(4, 5, 7)),
    ("enum-after-range", "[완료] ar #3~#5·#9", P, ar(3, 4, 5, 9), ar(3, 4, 5) | hc(9)),
    # 문장 단일 명시 리포
    ("sentence-single", "[진행] ar #3 끝났고 #9 남음", P, ar(3, 9), ar(3) | hc(9)),
    ("sentence-single-no-project", "[진행] ar #3 끝났고 #9 남음", [], ar(3, 9), ar(3)),
    ("sentence-single-alias-same-repo", "[진행] ar #3 와 alpha-repo #4 끝, 이어서 #9 남음", P, ar(3, 4, 9),
     ar(3, 4) | hc(9)),
    ("sentence-two-repos-drops-bare", "[진행] ar #3 와 hc #7 끝, 이어서 #9 남음", P, ar(3) | hc(7), ar(3) | hc(7, 9)),
    ("sentence-two-repos-word-breaks-enum", "[인계] ar #3 그리고 #5, hc #7", P, ar(3) | hc(7), ar(3) | hc(5, 7)),
    ("range-over-cap-end-follows-sentence-repo", "[완료] ar #1~#30", P, ar(1, 30), ar(1) | hc(30)),
    # 사전 연막 거짓 열림과 같은 모양의 합성 문장(접두 하나 + 열거 + 괄호 설명 + 쉼표 뒤 열거)
    ("preflight-shape", "[인계] ar #11·#12·#13·#14(후속 정리), #15·#16·#17 남음", P, ar(11, 12, 13, 14, 15, 16, 17),
     ar(11) | hc(12, 13, 14, 15, 16, 17)),
    ("preflight-shape-two-sentences", "[인계] ar #11·#12(정리), #13 남음. 다음은 #40 처리", P,
     ar(11, 12, 13) | hc(40), ar(11) | hc(12, 13, 40)),
]


@pytest.mark.parametrize("case, content, tags, expect, now_v2", CHANGES, ids=[c[0] for c in CHANGES])
def test_v21_changes(case, content, tags, expect, now_v2):
    assert keys(NEW, content, tags) == expect


@pytest.mark.parametrize("case, content, tags, expect, now_v2", CHANGES, ids=[c[0] for c in CHANGES])
def test_v2_path_is_unchanged_on_the_same_sentences(case, content, tags, expect, now_v2):
    assert keys("v2", content, tags) == now_v2


# --- 회귀 가드: v2 와 v2.1 이 같은 결과 --------------------------------------------------------------------

SAME = [
    ("no-explicit-repo-uses-project", "[진행] #9 남음", P, hc(9)),
    ("no-explicit-repo-no-project", "[진행] #9 남음", [], set()),
    ("unknown-word-prefix-is-not-a-repo", "[완료] foo #3·#4", P, hc(3, 4)),
    ("pr-number-still-excluded", "[완료] ar #3, PR #12 머지", P, ar(3)),
    ("pr-number-excluded-in-enum", "[완료] ar #3·PR #12 머지", P, ar(3)),
    ("html-entity-stops-enum", "[완료] ar #3·&#4; 표기", P, ar(3)),
    ("number-boundary-stops-enum", "[완료] ar #3·#2511x", P, ar(3)),
    ("url-number-masked", "[완료] ar #3, https://example.invalid/x#4", P, ar(3)),
    ("item-tag-only", "", ["item:ar-5"], ar(5)),
    ("range-expansion-kept", "[완료] ar #3~#5", P, ar(3, 4, 5)),
    ("explicit-only", "[완료] ar #3 와 hc #7", P, ar(3) | hc(7)),
]


@pytest.mark.parametrize("path", ("v2", NEW))
@pytest.mark.parametrize("case, content, tags, expect", SAME, ids=[c[0] for c in SAME])
def test_v21_keeps_v2_results(path, case, content, tags, expect):
    assert keys(path, content, tags) == expect


# --- 언급 출처·참조 고정 -----------------------------------------------------------------------------------

def hits(content, tags=P):
    fn = item_keys.key_hits_v2 if RED else item_keys.key_hits_v21
    return [(h.key, h.source, h.sent, h.ref_fixed) for h in fn(content, tags, ALIASES)]


def test_hit_sources_known_enum_sentence():
    assert hits("[진행] ar #3·#4 와 #9 남음") == [
        ((AR, 3), item_keys.SOURCE_KNOWN, 0, False),
        ((AR, 4), item_keys.SOURCE_ENUM, 0, False),
        ((AR, 9), item_keys.SOURCE_SENTENCE, 0, False),
    ]


def test_sentence_repo_number_in_upstream_context_is_ref_fixed_but_enum_is_not():
    # 문장 단일 리포로 붙인 번호는 접두 없는 번호라 v2 와 같이 참조 고정, 열거 전파 번호는 명시 접두가 붙은 것으로 본다.
    assert hits("[메모] upstream ar #3·#4 와 #9 참고") == [
        ((AR, 3), item_keys.SOURCE_KNOWN, 0, False),
        ((AR, 4), item_keys.SOURCE_ENUM, 0, False),
        ((AR, 9), item_keys.SOURCE_SENTENCE, 0, True),
    ]


def test_bare_number_without_explicit_repo_keeps_v2_source_and_fixing():
    assert hits("[메모] upstream #9 참고") == [((HC, 9), item_keys.SOURCE_BARE, 0, True)]


def test_constants():
    assert item_keys.RULE_VERSION == "item-keys-v1"
    assert item_keys.RULE_VERSION_V2 == "item-keys-v2"
    assert item_keys.RULE_VERSION_V21 == "item-keys-v2.1"
    assert item_roles.KEY_RULES_V21 == "v2.1"
    assert item_roles.KEY_RULES == ("v1", "v2", "v2.1")


# --- item_roles: 언급 목록 ---------------------------------------------------------------------------------

def test_memory_mentions_v21_flags():
    parsed = item_roles.memory_mentions("m1", "[인계] ar #3·#4 남음. upstream #9 참고.", P, ALIASES, rules=NEW)
    got = [(m["key"], m["sent"], m["role"], m["fixed"], m["bare"]) for m in parsed["mentions"]]
    assert got == [
        ("alpha-repo#3", 0, "open", False, False),
        ("alpha-repo#4", 0, "open", False, False),
        ("hindsight-crew#9", 1, "ref", True, True),
    ]
    assert parsed["keys"] == ["alpha-repo#3", "alpha-repo#4", "hindsight-crew#9"]


def test_memory_mentions_v21_sentence_repo_is_not_bare():
    parsed = item_roles.memory_mentions("m1", "[인계] ar #3 끝났고 #9 남음", P, ALIASES, rules=NEW)
    assert [(m["key"], m["bare"]) for m in parsed["mentions"]] == [("alpha-repo#3", False), ("alpha-repo#9", False)]


def test_memory_mentions_v2_unchanged_on_the_preflight_shape():
    text = "[인계] ar #11·#12(정리), #13 남음"
    parsed = item_roles.memory_mentions("m1", text, P, ALIASES, rules="v2")
    assert [(m["key"], m["bare"]) for m in parsed["mentions"]] == [
        ("alpha-repo#11", False), ("hindsight-crew#12", True), ("hindsight-crew#13", True)]


def test_key_hits_for_dispatches_by_store_rules():
    text = "[인계] ar #3·#4"
    assert item_roles.key_hits_for(text, P, ALIASES, "v2") == item_keys.key_hits_v2(text, P, ALIASES)
    assert item_roles.key_hits_for(text, P, ALIASES, NEW) == item_keys.key_hits_v21(text, P, ALIASES)
    with pytest.raises(ValueError):
        item_roles.key_hits_for(text, P, ALIASES, "v1")


@pytest.mark.parametrize("content", [c[1] for c in CHANGES] + [c[1] for c in SAME])
def test_v21_explicit_repos_are_a_subset_of_v2_repos(content):
    """게이트웨이 근거 읽기 범위(`github_read_scope`)는 v2 결과 repo 로 미리 정한다. v2.1 결과 repo(참조 고정 제외)가 그
    부분집합이라 v2.1 저장소에서도 범위가 모자라지 않는다."""
    v2 = {h.key[0] for h in item_keys.key_hits_v2(content, P, ALIASES) if not h.ref_fixed}
    v21 = {h.key[0] for h in item_keys.key_hits_v21(content, P, ALIASES) if not h.ref_fixed}
    assert v21 <= v2


# --- rework #410-1: 괄호 안에 다른 리포·업스트림 문맥이 있으면 열거 전파를 그 괄호에서 멈춘다 -----------------------

PAREN_STOP = [
    # (경우, 본문, 기대 hits 중 뒤 번호의 (키, source, ref_fixed))
    ("context-word-in-paren", "[메모] ar#5 (mem0 쪽 원인) #3913 참고", ((AR, 3913), item_keys.SOURCE_SENTENCE, True)),
    ("upstream-word-in-paren", "[메모] ar #5 (upstream 쪽) #77 참고", ((AR, 77), item_keys.SOURCE_SENTENCE, True)),
    ("known-repo-in-paren", "[메모] ar #5 (hc 쪽 일) #77 남음", None),                 # 명시 리포는 ar 하나, 괄호 안 hc 는 # 없음
    ("alias-in-paren", "[메모] ar #5 (alpha-repo 쪽 정리) #77 남음", None),
    ("owner-repo-in-paren", "[메모] ar #5 (org/other 참고) #77 남음", None),
]


@pytest.mark.parametrize("case, content, tail_hit", PAREN_STOP, ids=[c[0] for c in PAREN_STOP])
def test_paren_with_other_repo_or_context_stops_enumeration(case, content, tail_hit):
    got = hits(content)
    assert got[0][:2] == ((AR, 5), item_keys.SOURCE_KNOWN)
    tail = [h for h in got if h[0][1] != 5]
    assert all(h[1] != item_keys.SOURCE_ENUM for h in tail)              # 괄호 뒤 번호는 열거로 붙지 않는다
    if tail_hit is not None:
        assert [(h[0], h[1], h[3]) for h in tail] == [tail_hit]
    else:                                                                # 문장 단일 리포 규칙이 판정한다
        assert [(h[0], h[1]) for h in tail] == [((AR, 77), item_keys.SOURCE_SENTENCE)]


def test_plain_note_paren_still_passes_in_two_repo_sentence():
    # enum-paren-note 와 같은 모양: 설명만 있는 괄호는 통과(명시 리포가 둘이라 문장 단일 규칙은 못 쓴다)
    got = hits("[인계] ar #3·#4(설명), #5 와 hc #7 남음")
    assert ((AR, 5), item_keys.SOURCE_ENUM, 0, False) in got
