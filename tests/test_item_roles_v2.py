"""S4 역할 저장소의 키 규칙 v2·의심 키·GitHub 근거 테이블과 근거 판정 (issue #395, R7-1).

동작이 바뀌는 경우(참조 고정 언급, 의심 키 수 집계)는 `HC_R7_RED=1` 이면 v1 경로로 돌아 단언이 실패한다(red 기록용).
v1 기본 불변: `memory_mentions` 기본 출력, 스키마 v1 필수 키·테이블, `load_store` 3-튜플, github 인자 없는 publish 는 그대로다.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from gateway import item_roles as IR

HC = "hindsight-crew"
ALIASES = {"hc": HC, HC: HC}
P = ["project:hindsight-crew"]
CHANGE = ("v1",) if os.environ.get("HC_R7_RED") == "1" else ("v2",)
BOTH = ("v1", "v2")


def mentions(path, mid, text, tags=P):
    if path == "v1":
        return IR.memory_mentions(mid, text, tags, ALIASES)
    return IR.memory_mentions(mid, text, tags, ALIASES, rules="v2")


def by_key(parsed):
    out = {}
    for m in parsed["mentions"]:
        out.setdefault(m["key"], []).append(m)
    return out


# --- 참조 고정 언급 -------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", CHANGE)
def test_upstream_context_mention_is_fixed_ref(path):
    p = mentions(path, "m", "[메모] upstream #5 는 아직 남음")
    (m,) = by_key(p)["hindsight-crew#5"]
    assert m.get("fixed") is True and m["role"] == "ref"
    assert IR.judge_mentions(p) == []                       # LLM 판정 대상이 아니다


@pytest.mark.parametrize("path", CHANGE)
def test_fixed_and_normal_mentions_of_one_key_are_kept_apart(path):
    p = mentions(path, "m", "[메모] upstream #5 근거. #5 남음.")
    ms = by_key(p)["hindsight-crew#5"]
    assert [(m["sent"], m.get("fixed")) for m in ms] == [(0, True), (1, False)]
    assert [m["sent"] for m in IR.judge_mentions(p)] == [1]


@pytest.mark.parametrize("path", BOTH)
def test_explicit_repo_in_context_is_judged_in_both_paths(path):
    p = mentions(path, "m", "[메모] upstream 비교: hc#6 남음")
    (m,) = by_key(p)["hindsight-crew#6"]
    assert not m.get("fixed") and m["role"] == "open"
    assert len(IR.judge_mentions(p)) == 1


def test_default_mentions_equal_explicit_v1_and_have_no_v2_fields():
    text = "[메모] upstream #5 남음. PR #7 머지. #1~#3"
    assert IR.memory_mentions("m", text, P, ALIASES) == IR.memory_mentions("m", text, P, ALIASES, rules="v1")
    assert all("fixed" not in m and "bare" not in m for m in IR.memory_mentions("m", text, P, ALIASES)["mentions"])


def test_v2_keys_follow_the_v2_extraction():
    p = IR.memory_mentions("m", "[완료] #1~#3 끝. PR #7 머지.", P + ["item:hc-9"], ALIASES, rules="v2")
    assert p["keys"] == ["hindsight-crew#1", "hindsight-crew#2", "hindsight-crew#3", "hindsight-crew#9"]
    tag = [m for m in p["mentions"] if m["sent"] is None]
    assert tag == [{"mid": "m", "key": "hindsight-crew#9", "sent": None, "role": "ref", "source": "item_tag",
                    "fixed": False, "bare": False}]


def test_unknown_rules_are_refused():
    with pytest.raises(ValueError):
        IR.memory_mentions("m", "#1", P, ALIASES, rules="v3")


# --- 의심 키(관측 최대 번호) ------------------------------------------------------------------------------

LISTING = [("[완료] hc#10 머지", []), ("[메모] owner/hindsight-crew#4 참고", []), ("[인계] #500 남음", P),
           ("[인계] #7 남음", P), ("[인계] #900 남음", ["project:other"])]


def test_observed_max_counts_only_explicit_repo_forms():
    table = IR.observed_max(LISTING, ALIASES)
    assert table == {HC: 10}                                 # 번호만 키(#500)·태그·관측 없는 repo 는 상한 표에 없다
    assert IR.observed_max_json(table) == '{"hindsight-crew":10}'
    assert IR.observed_max_sha256(table) == IR.sha256_text('{"hindsight-crew":10}')


@pytest.mark.parametrize("path", CHANGE)
def test_suspect_key_count(path):
    table = IR.observed_max(LISTING, ALIASES)
    ms = [m for i, (text, tags) in enumerate(LISTING) for m in mentions(path, "m%d" % i, text, tags)["mentions"]]
    assert IR.suspect_keys(ms, table) == {"hindsight-crew#500"}


@pytest.mark.parametrize("path", BOTH)
def test_suspect_keys_are_not_removed(path):
    p = mentions(path, "m", "[인계] #500 남음")
    assert p["keys"] == ["hindsight-crew#500"]


# --- 저장소: v1 불변과 선택 테이블 -------------------------------------------------------------------------

META = {"model": "claude-haiku-5-5", "prompt_sha256": "p" * 64, "aliases_sha256": "a" * 64,
        "updated_at": "2026-11-02T00:00:00Z"}
MEMS = {"m1": {"document_id": "d1", "mentioned_at": "2026-10-01T00:00:00+00:00", "state": "valid",
               "text_sha256": "t" * 64, "judged_at": "2026-11-02T00:00:00Z"}}
ROLES = {"m1": {"hindsight-crew#1": "done"}}
GH = {"repos": {HC: {"owner": "o", "status": "ok", "fetched_at": "2026-11-03T00:00:00Z", "error_code": None},
                "gone": {"owner": "o", "status": "missing", "fetched_at": "2026-11-03T00:00:00Z",
                         "error_code": "http-404"}},
      "items": {(HC, 1): {"kind": "issue", "state": "closed", "state_reason": "completed",
                          "created_at": "2026-10-01T00:00:00Z", "closed_at": "2026-10-02T00:00:00Z",
                          "merged_at": None, "fetched_at": "2026-11-03T00:00:00Z"}}}


def tables(db):
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_publish_without_github_keeps_the_v1_tables_and_bytes(tmp_path):
    a, b = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    IR.publish(str(a), META, MEMS, ROLES)
    IR.publish(str(b), META, MEMS, ROLES, github=None)
    assert tables(a) == {"meta", "memories", "mention_roles"}
    assert a.read_bytes() == b.read_bytes()
    assert IR.load_github(str(a)) is None
    assert len(IR.load_store(str(a))) == 3
    meta, mems, roles, gh = IR.load_store(str(a), with_github=True)
    assert gh is None and roles == ROLES


def test_publish_with_github_round_trips(tmp_path):
    db = tmp_path / "s.sqlite"
    IR.publish(str(db), dict(META, key_rules="v2"), MEMS, ROLES, github=GH)
    assert tables(db) == {"meta", "memories", "mention_roles", "github_items", "github_repos"}
    meta, mems, roles, gh = IR.load_store(str(db), with_github=True)
    assert meta["key_rules"] == "v2" and IR.key_rules_of(meta) == "v2"
    assert gh == GH and IR.load_github(str(db)) == GH
    assert IR.load_store(str(db))[2] == ROLES                # 3-튜플 호출자는 그대로


def test_key_rules_absent_means_v1():
    assert IR.key_rules_of(META) == "v1"


@pytest.mark.parametrize("bad", [
    {"items": {(HC, 2): dict(GH["items"][(HC, 1)], state="merged")}},
    {"items": {(HC, 2): dict(GH["items"][(HC, 1)], kind="discussion")}},
    {"repos": {"x": dict(GH["repos"][HC], status="closed")}},
])
def test_github_tables_check_their_values(tmp_path, bad):
    gh = {"repos": {**GH["repos"], **bad.get("repos", {})}, "items": {**GH["items"], **bad.get("items", {})}}
    with pytest.raises((sqlite3.IntegrityError, IR.StoreError)):
        IR.publish(str(tmp_path / "s.sqlite"), META, MEMS, ROLES, github=gh)
    assert not (tmp_path / "s.sqlite").exists()


# --- GitHub 근거 판정(순수 함수, S3 #396 이 import) -----------------------------------------------------------

def item(kind="issue", state="closed", reason=None, merged=None, closed="2026-10-02T00:00:00Z"):
    return {"kind": kind, "state": state, "state_reason": reason, "created_at": "2026-10-01T00:00:00Z",
            "closed_at": closed if state == "closed" or merged else None, "merged_at": merged,
            "fetched_at": "2026-11-03T00:00:00Z"}


OK = {"owner": "o", "status": "ok", "fetched_at": "2026-11-03T00:00:00Z", "error_code": None}


@pytest.mark.parametrize("case, repo_row, item_row, verdict, reason", [
    ("completed-issue", OK, item(reason="completed"), IR.GH_CLOSED, "completed"),
    ("merged-pr", OK, item(kind="pr", merged="2026-10-02T00:00:00Z"), IR.GH_CLOSED, "merged"),
    ("closed-unmerged-pr", OK, item(kind="pr"), IR.GH_NOT_PLANNED, "pr-unmerged"),
    ("not-planned", OK, item(reason="not_planned"), IR.GH_NOT_PLANNED, "not_planned"),
    ("duplicate", OK, item(reason="duplicate"), IR.GH_NOT_PLANNED, "duplicate"),
    ("closed-without-reason", OK, item(reason=None), IR.GH_CLOSED_NO_REASON, "no-reason"),
    ("unknown-reason", OK, item(reason="reopened"), IR.GH_UNKNOWN, "unknown-reason"),
    ("reopened", OK, item(state="open", reason="reopened"), IR.GH_OPEN, "reopened"),
    ("open-issue", OK, item(state="open"), IR.GH_OPEN, "open"),
    ("open-pr", OK, item(kind="pr", state="open"), IR.GH_OPEN, "open"),
    ("repo-missing", dict(OK, status="missing", error_code="http-404"), None, IR.GH_UNKNOWN, "repo-missing"),
    ("repo-error", dict(OK, status="error", error_code="http-401"), item(reason="completed"), IR.GH_UNKNOWN,
     "repo-error"),
    ("repo-not-fetched", None, None, IR.GH_UNKNOWN, "repo-not-fetched"),
    ("no-number", OK, None, IR.GH_UNKNOWN, "no-number"),
])
def test_github_evidence(case, repo_row, item_row, verdict, reason):
    ev = IR.github_evidence(repo_row, item_row)
    assert (ev.verdict, ev.reason) == (verdict, reason)
    assert IR.is_closed_verdict(ev.verdict) is (verdict in (IR.GH_CLOSED, IR.GH_CLOSED_NO_REASON))
    if verdict == IR.GH_UNKNOWN or verdict == IR.GH_OPEN:
        assert ev.closed_at is None


def test_evidence_for_key_reads_the_tables():
    assert IR.evidence_for_key("hindsight-crew#1", GH).verdict == IR.GH_CLOSED
    assert IR.evidence_for_key("hindsight-crew#1", GH).closed_at == "2026-10-02T00:00:00Z"
    assert IR.evidence_for_key("hindsight-crew#2", GH).reason == "no-number"
    assert IR.evidence_for_key("gone#1", GH).reason == "repo-missing"
    assert IR.evidence_for_key("hindsight-crew#1", None).reason == "repo-not-fetched"
    assert IR.evidence_for_key("not-a-key", GH).reason == "no-number"
