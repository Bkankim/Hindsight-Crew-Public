"""작업 키 규칙 v2 (issue #395, R7-1): 범위 전개, PR 표기 제외, 업스트림 문맥 참조 고정. v1 은 그대로.

동작이 바뀌는 경우(CHANGE)는 v2 진입점으로 통과하고 v1 경로로는 단언이 실패해야 한다. 그 red 는
`HC_R7_RED=1` 로 이 파일을 돌리면 CHANGE 경우만 v1 경로로 바뀌어 AssertionError 로 재현된다(완료 기준의 red 기록용).
v1 과 같은 결과가 맞는 경우(상한 초과·역순 범위는 양 끝만, `issue #n` 유지, 업스트림 문맥에서도 키는 남음)는
두 경로 모두 통과하는 회귀 가드다.
"""
from __future__ import annotations

import os

import pytest

from gateway import item_keys
from gateway.item_keys import extract_keys

HC = "hindsight-crew"
ALIASES = {"hc": HC, HC: HC}
P = ["project:hindsight-crew"]
CHANGE = ("v1",) if os.environ.get("HC_R7_RED") == "1" else ("v2",)
BOTH = ("v1", "v2")


def keys(path, content, tags, aliases=ALIASES):
    if path == "v1":
        return extract_keys(content, tags, aliases)
    return item_keys.extract_keys_v2(content, tags, aliases)


def hc(*ns):
    return {(HC, n) for n in ns}


# --- 동작이 바뀌는 경우 -----------------------------------------------------------------------------

@pytest.mark.parametrize("path", CHANGE)
@pytest.mark.parametrize("case, content, tags, expect", [
    ("range-tilde", "[완료] #10~#12 머지", P, hc(10, 11, 12)),
    ("range-dash", "[완료] #10-#12 머지", P, hc(10, 11, 12)),
    ("range-dots", "[완료] #10..#12 머지", P, hc(10, 11, 12)),
    ("range-spaced-tilde", "[완료] #10 ~ #12 머지", P, hc(10, 11, 12)),
    ("range-known-repo-prefix", "[완료] hc#10~#12 머지", [], hc(10, 11, 12)),
    ("range-known-repo-space-prefix", "[완료] hc #10 ~ #12 머지", [], hc(10, 11, 12)),
    ("range-owner-prefix", "[완료] Bkankim/Hindsight-Crew#3~#5", [], hc(3, 4, 5)),
    ("range-word-space-prefix", "[완료] issue #10~#12", P, hc(10, 11, 12)),
    ("range-cap-exactly-20", "[완료] #1~#20", P, hc(*range(1, 21))),
    ("range-two-in-one-sentence", "[완료] #1~#3, #7~#8", P, hc(1, 2, 3, 7, 8)),
    ("pr-space", "[완료] PR #12 머지, #7 해결", P, hc(7)),
    ("pr-lowercase", "[완료] pr #12 머지", P, set()),
    ("pr-pull", "[완료] pull #12 머지", P, set()),
    ("pr-pull-request", "[완료] Pull Request #12 머지", P, set()),
    ("pr-korean-adjacent", "[완료] 이번PR #12 머지", P, set()),
    ("pr-range", "[완료] PR #12~#14 머지, #7 남음", P, hc(7)),
    ("pr-other-issue-number-kept", "[완료] #30 은 PR #31 로 닫음", P, hc(30)),
])
def test_v2_changes(path, case, content, tags, expect):
    assert keys(path, content, tags) == expect


# --- 회귀 가드: 두 경로 모두 같은 결과 ----------------------------------------------------------------

@pytest.mark.parametrize("path", BOTH)
@pytest.mark.parametrize("case, content, tags, expect", [
    ("range-over-cap-endpoints-only", "[완료] #1~#30", P, hc(1, 30)),
    ("range-21-endpoints-only", "[완료] #1~#21", P, hc(1, 21)),
    ("range-reverse-endpoints-only", "[완료] #12~#10", P, hc(10, 12)),
    ("range-equal", "[완료] #5~#5", P, hc(5)),
    ("range-unknown-attached-prefix-left-as-v1", "[완료] xhc#10~#12", P, hc(12)),
    ("range-unknown-word-space-prefix", "[완료] done #10~#40", P, hc(10, 40)),
    ("issue-word-kept", "[완료] issue #251 머지", P, hc(251)),
    ("issue-korean-word-kept", "[완료] 이슈 #251 머지", P, hc(251)),
    ("pr-no-space-already-excluded-in-v1", "[완료] PR#12 머지, #7 해결", P, hc(7)),
    ("pr-inside-word-is-not-pr", "[완료] XPR #5 머지", P, hc(5)),
    ("explicit-repo", "[완료] hc#251 머지", [], hc(251)),
    ("upstream-context-keeps-the-key", "[메모] upstream #5 참고", P, hc(5)),
    ("url-masked", "[완료] https://github.com/o/r/pull/12 #7", P, hc(7)),
    ("fence-masked", "[완료]\n```\n#1~#3\n```\n#9", P, hc(9)),
    ("tag", "", ["item:hc-251"], hc(251)),
    ("no-hash", "[완료] 번호 없음", P, set()),
])
def test_same_in_both_paths(path, case, content, tags, expect):
    assert keys(path, content, tags) == expect


def test_v1_rule_version_is_unchanged_and_v2_has_its_own():
    assert item_keys.RULE_VERSION == "item-keys-v1"
    assert item_keys.RULE_VERSION_V2 == "item-keys-v2"
    assert item_keys.RANGE_CAP == 20


# --- 참조 고정 표시(키 언급 단위) ------------------------------------------------------------------------

def hits(content, tags=P):
    return {(h.key, h.source, h.ref_fixed) for h in item_keys.key_hits_v2(content, tags, ALIASES)}


@pytest.mark.parametrize("word", ["upstream", "UPSTREAM", "업스트림", "mem0", "Mem0", "외부 리포", "외부 저장소",
                                  "다른 리포", "타 리포"])
def test_context_word_fixes_bare_keys_as_ref(word):
    assert hits("[메모] %s 이슈 #5 참고" % word) == {((HC, 5), "bare", True)}


def test_explicit_repo_forms_are_not_fixed_in_context():
    got = hits("[메모] upstream 비교: hc#6, vectorize-io/hindsight#12, 그리고 #5")
    assert got == {((HC, 6), "known", False), (("hindsight", 12), "owner", False), ((HC, 5), "bare", True)}


def test_context_is_per_sentence():
    got = hits("[메모] upstream 쪽 이야기다. #5 남음.")
    assert got == {((HC, 5), "bare", False)}


def test_word_number_without_context_is_not_fixed():
    assert hits("[메모] issue #5 남음") == {((HC, 5), "bare", False)}


def test_tag_hits_are_not_fixed_and_have_no_sentence():
    hs = item_keys.key_hits_v2("[메모] upstream", ["item:hc-9"] + P, ALIASES)
    assert [(h.key, h.source, h.ref_fixed, h.sent) for h in hs] == [((HC, 9), "tag", False, None)]


def test_hits_keys_equal_extract_keys_v2():
    text = "[완료] hc#1~#3 끝. upstream #9 참고. PR #4 머지.\n#7 남음"
    assert {h.key for h in item_keys.key_hits_v2(text, P, ALIASES)} == item_keys.extract_keys_v2(text, P, ALIASES)
    assert item_keys.extract_keys_v2(text, P, ALIASES) == hc(1, 2, 3, 7, 9)


def test_v2_does_not_change_v1_results_on_the_v1_matrix_inputs():
    # v2 가 바꾸는 표기(범위·PR·문맥)가 없는 입력에서 두 규칙의 키는 같다
    for content, tags in [("[완료] HC#251, hindsight-crew #300", []), ("이슈#251 완료, (#300)", ["project:hc"]),
                          ("[완료] &#251; ##251 #ff0000", ["project:hc"]), ("owner/hindsight-crew#77。 #8", P)]:
        assert item_keys.extract_keys_v2(content, tags, ALIASES) == extract_keys(content, tags, ALIASES)
