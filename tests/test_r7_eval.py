"""R7-0 표 키 전체 GitHub 대조 평가 하네스(issue #394, `bench/r7_eval.py`). 네트워크 0, LLM 0.

고정하는 것:
  * 범주 판정과 t 시점 규칙(이슈 열림·닫힘 전후, 재오픈·재닫힘, 이력 없음, PR 머지·닫힘 전후, 없는 번호, t 이후 생성,
    없는 리포)을 parametrize 행렬로.
  * 표 재구성: 역할 저장소 -> 게이트웨이 공개 함수(`read_store`·`role_states`·`build_table`), 손잡이 scope all·
    recent_done_days 0·max_rows 1000, 표 키 = 열림 키 전부. 저장소는 읽기 전용으로 연다.
  * 회수율 분모(t 시점 열린 이슈, PR 제외)와 보조 분모(저장소에 키가 있는 열린 이슈).
  * 원인 표지(1 범위 표기, 2 동점, 3 끝냄 뒤 open, 4 번호만 규칙의 외부 문맥·상한 초과, 5 PR 표기, 데이터층, 기타)와
    주 원인 순서.
  * 봉인 저장물(이름에 `sealed`)·봉인 t(dev t 상한 10-13 뒤 ~ 10-28)를 exit 2 로 거부, 별칭 sha 불일치 exit 3.
  * 리포 코드: 런 5 코드 유지, 없는 리포는 이름순 x01.., 기존 r7 코드 파일이 있으면 이어 붙인다.
"""
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from bench import r7_eval as E
from gateway import item_open_table as OT
from gateway import item_roles as IR

KST = timezone(timedelta(hours=9))
T = datetime(2026, 10, 10, 23, 59, 59, tzinfo=KST)
ALIASES = {"hc": "hindsight-crew", "hindsight-crew": "hindsight-crew", "other": "other-repo"}


def at(day, hour=12, month=10):
    return datetime(2026, month, day, hour, tzinfo=KST).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def item(n, kind="issue", state="open", reason=None, created=at(1), closed=None, merged=None):
    return {"kind": kind, "state": state, "state_reason": reason, "created_at": created, "closed_at": closed,
            "merged_at": merged}


def repo_rec(items, events=None, status=200):
    return {"status": status, "items": {str(k): v for k, v in items.items()},
            "events": {str(k): v for k, v in (events or {}).items()}}


# --- 범주 판정 행렬 -------------------------------------------------------------------------------

CASES = [
    # (이름, 레코드 items, events, n, 기대 범주)
    ("open-never-closed", {1: item(1)}, {}, 1, "open_issue"),
    ("closed-after-t", {1: item(1, state="closed", reason="completed", closed=at(12))},
     {1: [["closed", at(12), None]]}, 1, "open_issue"),
    ("closed-before-t", {1: item(1, state="closed", reason="completed", closed=at(5))},
     {1: [["closed", at(5), None]]}, 1, "closed_completed"),
    ("closed-not-planned-before-t", {1: item(1, state="closed", reason="not_planned", closed=at(5))},
     {1: [["closed", at(5), None]]}, 1, "closed_not_planned"),
    ("closed-duplicate-before-t", {1: item(1, state="closed", reason="duplicate", closed=at(5))},
     {1: [["closed", at(5), None]]}, 1, "closed_not_planned"),
    ("closed-reopened-before-t", {1: item(1, state="open", reason="reopened")},
     {1: [["closed", at(3), None], ["reopened", at(6), None]]}, 1, "open_issue"),
    ("closed-reopened-after-t", {1: item(1, state="open", reason="reopened")},
     {1: [["closed", at(3), None], ["reopened", at(12), None]]}, 1, "closed_completed"),
    ("reclosed-before-t", {1: item(1, state="closed", reason="not_planned", closed=at(8))},
     {1: [["closed", at(3), "completed"], ["reopened", at(6), None], ["closed", at(8), None]]}, 1,
     "closed_not_planned"),
    ("reopened-closed-at-later", {1: item(1, state="closed", reason="completed", closed=at(12))},
     {1: [["closed", at(3), "completed"], ["reopened", at(6), None], ["closed", at(12), None]]}, 1, "open_issue"),
    ("first-close-reason-from-event", {1: item(1, state="closed", reason="completed", closed=at(12))},
     {1: [["closed", at(3), "not_planned"], ["reopened", at(11), None], ["closed", at(12), None]]}, 1,
     "closed_not_planned"),
    ("closed-no-history", {1: item(1, state="closed", reason="completed", closed=at(5))}, {}, 1, "reopened_unknown"),
    ("open-after-reopen-no-history", {1: item(1, state="open", reason="reopened")}, {}, 1, "reopened_unknown"),
    ("created-after-t", {1: item(1, created=at(11))}, {}, 1, "not_yet"),
    ("pr-created-after-t", {1: item(1, kind="pr", created=at(11))}, {}, 1, "not_yet"),
    ("pr-open", {1: item(1, kind="pr")}, {}, 1, "pr_open"),
    ("pr-merged-after-t", {1: item(1, kind="pr", state="closed", closed=at(12), merged=at(12))}, {}, 1, "pr_open"),
    ("pr-merged-before-t", {1: item(1, kind="pr", state="closed", closed=at(5), merged=at(5))}, {}, 1, "pr_merged"),
    ("pr-closed-before-t", {1: item(1, kind="pr", state="closed", closed=at(5))}, {}, 1, "pr_closed"),
    ("pr-closed-after-t", {1: item(1, kind="pr", state="closed", closed=at(12))}, {}, 1, "pr_open"),
    ("missing-number", {1: item(1)}, {}, 2, "missing_number"),
]


@pytest.mark.parametrize("name,items,events,n,want", CASES, ids=[c[0] for c in CASES])
def test_category_at_t(name, items, events, n, want):
    cat, _ = E.item_at(repo_rec(items, events), n, T)
    assert cat == want


@pytest.mark.parametrize("rec", [None, {"status": 404}], ids=["no-record", "404"])
def test_missing_repo(rec):
    assert E.item_at(rec, 1, T) == ("missing_repo", None)


@pytest.mark.parametrize("items,events,want_end", [
    ({1: item(1, state="closed", reason="completed", closed=at(5))}, {1: [["closed", at(5), None]]}, at(5)),
    ({1: item(1, kind="pr", state="closed", closed=at(4), merged=at(4))}, {}, at(4)),
    ({1: item(1, kind="pr", state="closed", closed=at(6))}, {}, at(6)),
    ({1: item(1)}, {}, None),
], ids=["issue-close", "pr-merge", "pr-close", "open"])
def test_end_instant(items, events, want_end):
    _, end = E.item_at(repo_rec(items, events), 1, T)
    assert (end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if end else None) == want_end


def test_pr_with_issue_record_pull_request_field_is_pr():
    raw = {"number": 5, "state": "open", "state_reason": None, "created_at": at(1), "closed_at": None,
           "pull_request": {"url": "x", "merged_at": None}, "title": "secret"}
    slim = E.slim_item(raw)
    assert slim["kind"] == "pr" and "title" not in slim
    assert E.item_at(repo_rec({5: slim}), 5, T)[0] == "pr_open"


def test_open_issues_at_excludes_prs_and_counts_unknown():
    rec = repo_rec({1: item(1), 2: item(2, kind="pr"), 3: item(3, state="closed", reason="completed", closed=at(12)),
                    4: item(4, state="closed", reason="completed", closed=at(5)), 5: item(5, created=at(11)),
                    6: item(6, state="closed", reason="completed", closed=at(12))},
                   {3: [["closed", at(12), None]], 4: [["closed", at(5), None]]})
    opened, unknown = E.open_issues_at(rec, T)
    assert opened == {1, 3} and unknown == {6}


# --- 이벤트 정리 -------------------------------------------------------------------------------------

def test_repo_events_keep_issue_close_reopen_only():
    raw = [{"event": "closed", "created_at": at(5), "state_reason": None, "issue": {"number": 1}},
           {"event": "labeled", "created_at": at(5), "issue": {"number": 1}},
           {"event": "reopened", "created_at": at(6), "issue": {"number": 1}},
           {"event": "closed", "created_at": at(7), "state_reason": "completed", "issue": {"number": 2,
                                                                                        "pull_request": {}}},
           {"event": "closed", "created_at": at(4), "issue": {"number": 3}}]
    ev = E.slim_events(raw)
    assert ev == {"1": [["closed", at(5), None], ["reopened", at(6), None]], "3": [["closed", at(4), None]]}


# --- 표 재구성 ------------------------------------------------------------------------------------------

def make_store(path, mems, aliases=ALIASES):
    """mems: [(mid, mentioned_at, {key: role})]"""
    meta = {"model": "m", "prompt_sha256": "p", "aliases_sha256": OT.aliases_sha256(aliases), "updated_at": "u"}
    memories = {mid: {"document_id": "d-" + mid, "mentioned_at": when, "state": "valid", "text_sha256": "s",
                      "judged_at": "j"} for mid, when, _ in mems}
    roles = {mid: r for mid, _, r in mems}
    IR.publish(str(path), meta, memories, roles, mode=0o600)
    return str(path)


def test_store_view_and_table(tmp_path):
    store = make_store(tmp_path / "s.sqlite", [
        ("a", at(1), {"hindsight-crew#1": "open", "other-repo#2": "open", "hindsight-crew#3": "ref"}),
        ("b", at(3), {"hindsight-crew#1": "done", "hindsight-crew#4": "open"}),
        ("c", at(12), {"hindsight-crew#4": "done", "hindsight-crew#5": "open"}),
    ])
    view = E.store_view(store, T)
    assert view.meta["aliases_sha256"] == OT.aliases_sha256(ALIASES)
    keys, truncated = E.table_keys(view, T)
    assert keys == ["hindsight-crew#4", "other-repo#2"] and truncated == 0
    assert view.all_keys == {"hindsight-crew#1", "other-repo#2", "hindsight-crew#3", "hindsight-crew#4"}
    assert E.repo_of("hindsight-crew#4") == "hindsight-crew"


def test_table_knobs_are_bank_wide():
    assert E.KNOBS["scope"] == "all" and E.KNOBS["recent_done_days"] == 0 and E.KNOBS["max_rows"] == 1000


def test_store_opened_read_only(tmp_path, monkeypatch):
    store = make_store(tmp_path / "s.sqlite", [("a", at(1), {"hindsight-crew#1": "open"})])
    before = os.stat(store).st_mtime_ns
    seen = []
    real = OT.connect_ro
    monkeypatch.setattr(OT, "connect_ro", lambda p: seen.append(p) or real(p))
    E.store_view(store, T)
    assert seen and os.stat(store).st_mtime_ns == before
    assert not os.path.exists(store + "-journal") and not os.path.exists(store + "-wal")


# --- 원인 표지 ------------------------------------------------------------------------------------------

def listing_row(mid, when, text, tags=("project:hindsight-crew",)):
    return {"id": mid, "text": text, "tags": list(tags), "state": "valid", "mentioned_at": when,
            "document_id": "d-" + mid, "type": "world"}


@pytest.mark.parametrize("sentence,key,want", [
    ("[진행] #12~#15 작업 중", "hindsight-crew#12", {"bare", "range"}),
    ("[진행] #12 ~ #15 작업 중", "hindsight-crew#15", {"bare", "range"}),
    ("[진행] HC#12-HC#15 작업 중", "hindsight-crew#12", {"explicit", "range"}),
    ("[진행] #12..#15 작업 중", "hindsight-crew#12", {"bare", "range"}),
    ("[진행] #12~#15 와 #12 남음", "hindsight-crew#12", {"bare"}),
    ("[진행] PR #40 리뷰 대기", "hindsight-crew#40", {"bare", "pr"}),
    ("[진행] pull #40 리뷰 대기", "hindsight-crew#40", {"bare", "pr"}),
    ("[진행] PR #40 이 #41 을 닫음", "hindsight-crew#41", {"bare"}),
    ("[진행] upstream #900 확인 필요", "hindsight-crew#900", {"bare", "external"}),
    ("[진행] 업스트림 이슈 #900 대기", "hindsight-crew#900", {"bare", "external"}),
    ("[진행] hindsight-crew#7 남음", "hindsight-crew#7", {"explicit"}),
    ("[진행] #7 남음", "hindsight-crew#7", {"bare"}),
    ("[진행] #8 남음", "hindsight-crew#7", set()),
])
def test_key_sources(sentence, key, want):
    assert E.key_sources(sentence, ["project:hindsight-crew"], ALIASES, key) == want


def cause_case(tmp_path, mems, texts, key, cat, end=None, repo_max=None):
    store = make_store(tmp_path / "s.sqlite", mems)
    view = E.store_view(store, T)
    listing = {mid: listing_row(mid, when, texts.get(mid, "")) for mid, when, _ in mems}
    rm = E.repo_max_numbers(listing, ALIASES, T) if repo_max is None else repo_max
    end_at = E.parse_instant(end) if end else None
    return E.causes_for(key, cat, end_at, view, listing, ALIASES, rm, T)


def test_cause_tie_and_reopen(tmp_path):
    mems = [("a", at(2), {"hindsight-crew#1": "done"}), ("b", at(5), {"hindsight-crew#1": "open"}),
            ("c", at(5), {"hindsight-crew#1": "done"})]
    texts = {"a": "[완료] #1 끝", "b": "[진행] #1 진행", "c": "[완료] #1 끝"}
    got = cause_case(tmp_path, mems, texts, "hindsight-crew#1", "closed_completed", end=at(1))
    assert got["causes"] == {"2", "3"} and got["primary"] == "2"


def test_cause_data_layer(tmp_path):
    mems = [("a", at(2), {"hindsight-crew#1": "open"})]
    got = cause_case(tmp_path, mems, {"a": "[진행] #1 진행"}, "hindsight-crew#1", "closed_completed", end=at(4))
    assert got["causes"] == {"data"} and got["primary"] == "data"


def test_done_after_close_is_not_data_layer(tmp_path):
    mems = [("a", at(2), {"hindsight-crew#1": "done"}), ("b", at(6), {"hindsight-crew#1": "open"})]
    texts = {"a": "[완료] #1 끝", "b": "[진행] #1 재검토"}
    got = cause_case(tmp_path, mems, texts, "hindsight-crew#1", "closed_completed", end=at(1))
    assert got["causes"] == {"3"}


def test_cause_range_endpoint_and_covered(tmp_path):
    mems = [("a", at(2), {"hindsight-crew#12": "open", "hindsight-crew#15": "open"}),
            ("b", at(3), {"hindsight-crew#13": "open"}),
            ("c", at(6), {"hindsight-crew#12": "done", "hindsight-crew#15": "done"}),
            ("d", at(7), {"hindsight-crew#12": "open"})]
    texts = {"a": "[진행] #12~#15 진행", "b": "[진행] #13 진행", "c": "[완료] #12~#15 완료", "d": "[진행] #12~#15 다시 봄"}
    got13 = cause_case(tmp_path, mems, texts, "hindsight-crew#13", "closed_completed", end=at(1))
    assert "1" in got13["causes"] and got13["range"] == {"covered"}
    got12 = cause_case(tmp_path, mems, texts, "hindsight-crew#12", "closed_completed", end=at(1))
    assert "1" in got12["causes"] and "endpoint" in got12["range"] and got12["primary"] == "1"


def test_cause_pr_and_external_and_over_max(tmp_path):
    mems = [("a", at(2), {"hindsight-crew#40": "open"}), ("b", at(2), {"hindsight-crew#900": "open"}),
            ("c", at(2), {"hindsight-crew#500": "open"}), ("e", at(1), {"hindsight-crew#30": "done"})]
    texts = {"a": "[진행] PR #40 리뷰 대기", "b": "[진행] upstream #900 확인 필요", "c": "[진행] issue #500 남음",
             "e": "[완료] hindsight-crew#30 완료"}
    assert cause_case(tmp_path, mems, texts, "hindsight-crew#40", "pr_open")["primary"] == "5"
    assert cause_case(tmp_path, mems, texts, "hindsight-crew#900", "missing_number")["causes"] == {"4"}
    got = cause_case(tmp_path, mems, texts, "hindsight-crew#500", "missing_number")
    assert got["causes"] == {"4"} and got["over_max"] is True


def test_cause_other_and_no_body(tmp_path):
    mems = [("a", at(2), {"hindsight-crew#7": "open"})]
    assert cause_case(tmp_path, mems, {"a": "[진행] hindsight-crew#7 진행"}, "hindsight-crew#7",
                      "missing_number")["primary"] == "other"
    store = make_store(tmp_path / "t.sqlite", mems)
    got = E.causes_for("hindsight-crew#7", "missing_number", None, E.store_view(store, T), {}, ALIASES, {}, T)
    assert got["no_body"] is True and got["primary"] == "other"


def test_repo_max_uses_explicit_only_and_cutoff():
    listing = {"a": listing_row("a", at(2), "[완료] hindsight-crew#30 완료, #999 참고"),
               "b": listing_row("b", at(12), "[완료] hindsight-crew#80 완료"),
               "c": listing_row("c", at(3), "x", tags=["project:hindsight-crew", "item:hindsight-crew-45"])}
    assert E.repo_max_numbers(listing, ALIASES, T) == {"hindsight-crew": 45}


# --- 봉인 가드·별칭·리포 코드 -------------------------------------------------------------------------

@pytest.mark.parametrize("day,ok", [("2026-09-28", True), ("2026-10-13", True), ("2026-10-14", False),
                                    ("2026-10-28", False), ("2026-10-20", False)])
def test_seal_t_guard(day, ok):
    d = datetime.strptime(day, "%Y-%m-%d").date()
    if ok:
        E.guard_dev_t(d)
    else:
        with pytest.raises(E.SealGuard):
            E.guard_dev_t(d)


def run_args(tmp_path, **over):
    a = {"--ops-store": str(tmp_path / "ops.sqlite"), "--ops-copy": str(tmp_path / "copy.json"),
         "--ops-listing": str(tmp_path / "ops-listing.jsonl"), "--dev-store": str(tmp_path / "dev.sqlite"),
         "--plan": str(tmp_path / "plan.json"), "--bodies": str(tmp_path / "bodies.jsonl"),
         "--meta": str(tmp_path / "meta.jsonl"), "--aliases": str(tmp_path / "aliases.json"),
         "--owners": str(tmp_path / "owners.json"), "--codes": str(tmp_path / "codes.json"),
         "--work": str(tmp_path / "work"), "--offline": None}
    a.update(over)
    out = ["run"]
    for k, v in a.items():
        out += [k] if v is None else [k, v]
    return out


@pytest.mark.parametrize("flag", ["--ops-store", "--dev-store", "--plan", "--bodies", "--meta", "--ops-listing"])
def test_sealed_input_exit_2(tmp_path, flag):
    argv = run_args(tmp_path, **{flag: str(tmp_path / "r5-sealed-x")})
    assert E.main(argv) == 2


def test_sealed_t_exit_2(tmp_path):
    argv = run_args(tmp_path) + ["--dev-t", "2026-10-28"]
    assert E.main(argv) == 2


def test_ops_t_in_seal_window_exit_2(tmp_path):
    (tmp_path / "copy.json").write_text(json.dumps({"copy_end_kst": "2026-10-20T10:00:00+09:00", "sha256": "x"}))
    assert E.main(run_args(tmp_path)) == 2


def test_alias_mismatch_exit_3(tmp_path):
    (tmp_path / "aliases.json").write_text(json.dumps({"zz": "zz-repo"}))
    make_store(tmp_path / "ops.sqlite", [("a", at(1), {"hindsight-crew#1": "open"})])
    assert E.check_aliases({"aliases_sha256": OT.aliases_sha256(ALIASES)}, {"zz": "zz-repo"}) is False
    assert E.check_aliases({"aliases_sha256": OT.aliases_sha256(ALIASES)}, ALIASES) is True


def test_repo_codes_keep_r5_and_extend(tmp_path):
    r5 = {"hindsight-crew": "r01", "b-repo": "r02"}
    codes = E.repo_codes(r5, {}, ["zeta", "hindsight-crew", "alpha"])
    assert codes == {"hindsight-crew": "r01", "b-repo": "r02", "alpha": "x01", "zeta": "x02"}
    again = E.repo_codes(r5, codes, ["beta", "alpha"])
    assert again["alpha"] == "x01" and again["zeta"] == "x02" and again["beta"] == "x03"


def test_write_private_mode(tmp_path):
    p = tmp_path / "d" / "f.json"
    E.write_private(str(p), "{}")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700


# --- 표 지표 --------------------------------------------------------------------------------------------

def test_table_metrics_precision_and_recall():
    gh = {"hindsight-crew": repo_rec({1: item(1), 2: item(2, kind="pr"), 3: item(3), 4: item(4)}),
          "other-repo": {"status": 404}}
    keys = ["hindsight-crew#1", "hindsight-crew#2", "hindsight-crew#9", "other-repo#5"]
    m = E.table_metrics(keys, gh, T, recall_repos=["hindsight-crew", "other-repo"],
                        store_keys={"hindsight-crew#1", "hindsight-crew#3"}, codes={"hindsight-crew": "r01",
                                                                                     "other-repo": "x01"})
    assert m["total"]["keys"] == 4 and m["total"]["open_issue"] == 1
    assert m["total"]["precision"] == pytest.approx(0.25)
    assert m["total"]["categories"] == {"open_issue": 1, "pr_open": 1, "missing_number": 1, "missing_repo": 1}
    assert m["total"]["recall"] == {"den": 3, "num": 1, "value": pytest.approx(1 / 3)}
    assert m["total"]["recall_store"] == {"den": 2, "num": 1, "value": pytest.approx(0.5)}
    assert set(m["repos"]) == {"r01", "x01"} and m["repos"]["r01"]["keys"] == 3
