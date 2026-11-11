"""R7-3 오프라인 측정 확장(issue #397, `bench/r7_eval.py`). 네트워크 0, LLM 0.

고정하는 것:
  * v2 저장소: meta `key_rules` 가 `role_states` 에 넘어간다(다른 기억 같은 시각 동점 = v2 끝남, v1 열림).
  * GitHub 근거 테이블 읽기와 `github_evidence` use/ignore: use + 테이블 = 3상태 표(R7-0 기준 분모 = `열림 후보` 줄,
    payload `keys`), ignore 또는 테이블 없음 = 지금 모양(열린 키 전부). 테이블 없는 저장소의 use 와 ignore 는 같은 표.
  * 3상태 줄 범주(줄 상태 x t 시점 GitHub 범주), 미확인·충돌 사유별 수.
  * 하위군: GitHub 연결 키(평가 캐시에 리포가 있음) / GitHub 없는 키(리포 없음·미확인).
  * 알려진 한계 수: (a) t 뒤 재오픈 - 표는 열림 후보인데 평가는 t 시점 닫힘, (b) 실명에 `_`·`.` 이 든 리포의 repo-missing.
  * `offline` 명령: 세 판본(켬·끔·수집 전) 요약, 저장소 감사(sha256, meta key_rules, GitHub 테이블 존재, 읽기 전후 sha
    같음, 작업 디렉터리 안), R7-0 캐시 원본 불변(넓힐 때는 새 파일), 봉인 거부 exit 2, 별칭 불일치 exit 3.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from bench import r7_eval as E
from gateway import item_open_table as OT
from gateway import item_roles as IR

KST = timezone(timedelta(hours=9))
T = datetime(2026, 11, 3, 3, 53, 28, tzinfo=KST)
ALIASES = {"hc": "hindsight-crew", "hindsight-crew": "hindsight-crew", "other": "other-repo"}
FETCHED = "2026-11-05T14:00:00Z"


def at(day, hour=12, month=10):
    return datetime(2026, month, day, hour, tzinfo=KST).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh_item(kind="issue", state="open", reason=None, created=at(1), closed=None, merged=None):
    return {"kind": kind, "state": state, "state_reason": reason, "created_at": created, "closed_at": closed,
            "merged_at": merged, "fetched_at": FETCHED}


def evidence():
    """수집 근거 테이블(현재 스냅샷 모양). #4 는 t 전에 닫혔다가 t 뒤에 재오픈돼 지금 열림."""
    return {"repos": {"hindsight-crew": {"owner": "o", "status": "ok", "fetched_at": FETCHED, "error_code": None},
                      "other-repo": {"owner": "o", "status": "missing", "fetched_at": FETCHED, "error_code": "http-404"},
                      "my_repo": {"owner": "o", "status": "missing", "fetched_at": FETCHED, "error_code": "http-404"}},
            "items": {("hindsight-crew", 1): gh_item(),
                      ("hindsight-crew", 2): gh_item(state="closed", reason="completed", closed=at(5)),
                      ("hindsight-crew", 3): gh_item(state="closed", reason="not_planned", closed=at(5)),
                      ("hindsight-crew", 4): gh_item(state="open", reason="reopened"),
                      ("hindsight-crew", 7): gh_item()}}


def eval_cache():
    """R7-0 평가 캐시 모양(리포 단위 events 로 t 시점 상태를 정한다)."""
    def it(**kw):
        d = gh_item(**kw)
        d.pop("fetched_at")
        return d
    return {"hindsight-crew": {"status": 200, "owner": "o", "calls": 2, "fetched_at": FETCHED,
                               "items": {"1": it(), "2": it(state="closed", reason="completed", closed=at(5)),
                                         "3": it(state="closed", reason="not_planned", closed=at(5)),
                                         "4": it(state="open", reason="reopened"), "7": it()},
                               "events": {"2": [["closed", at(5), None]], "3": [["closed", at(5), None]],
                                          "4": [["closed", at(5), None], ["reopened", at(4, month=11), None]]}},
            "other-repo": {"status": 404, "owner": "o", "calls": 1, "fetched_at": FETCHED},
            "my_repo": {"status": 404, "owner": "o", "calls": 1, "fetched_at": FETCHED}}


MEMS = [
    ("a", at(10), {"hindsight-crew#1": "open", "hindsight-crew#2": "open", "hindsight-crew#3": "open",
                   "hindsight-crew#4": "open", "other-repo#5": "open", "my_repo#6": "open",
                   "hindsight-crew#7": "open", "hindsight-crew#8": "open"}),
    ("b", at(20), {"hindsight-crew#7": "done"}),
]


def make_store(path, mems=MEMS, key_rules=None, github=None, aliases=ALIASES):
    meta = {"model": "m", "prompt_sha256": "p", "aliases_sha256": OT.aliases_sha256(aliases), "updated_at": "u"}
    if key_rules:
        meta[IR.META_KEY_RULES] = key_rules
    memories = {mid: {"document_id": "d-" + mid, "mentioned_at": when, "state": "valid", "text_sha256": "s",
                      "judged_at": "j"} for mid, when, _ in mems}
    roles = {mid: r for mid, _, r in mems}
    IR.publish(str(path), meta, memories, roles, mode=0o600, github=github)
    return str(path)


# --- v2 meta key_rules -> role_states --------------------------------------------------------------------

TIE = [("a", at(10), {"hindsight-crew#1": "open"}), ("b", at(10), {"hindsight-crew#1": "done"})]


@pytest.mark.parametrize("key_rules,want_open", [(None, True), ("v1", True), ("v2", False)],
                         ids=["no-meta", "v1", "v2"])
def test_store_view_passes_key_rules(tmp_path, key_rules, want_open):
    view = E.store_view(make_store(tmp_path / "s.sqlite", TIE, key_rules=key_rules), T)
    assert view.key_rules == (key_rules or "v1")
    assert (view.states["hindsight-crew#1"].state == OT.STATE_OPEN) is want_open


# --- use/ignore x 테이블 유무 행렬 -----------------------------------------------------------------------

ALL_OPEN = ["hindsight-crew#1", "hindsight-crew#2", "hindsight-crew#3", "hindsight-crew#4", "hindsight-crew#8",
            "my_repo#6", "other-repo#5"]
ARMS = [
    # (이름, 테이블 있음, 손잡이, 3상태, 분모 키)
    ("use-with-table", True, "use", True, ["hindsight-crew#1", "hindsight-crew#4"]),
    ("ignore-with-table", True, "ignore", False, ALL_OPEN),
    ("use-no-table", False, "use", False, ALL_OPEN),
    ("ignore-no-table", False, "ignore", False, ALL_OPEN),
]


@pytest.mark.parametrize("name,has_table,mode,three,want_keys", ARMS, ids=[a[0] for a in ARMS])
def test_table3_arms(tmp_path, name, has_table, mode, three, want_keys):
    store = make_store(tmp_path / "s.sqlite", key_rules="v2", github=evidence() if has_table else None)
    view = E.store_view(store, T, github_mode=mode)
    assert (view.github is not None) is (has_table and mode == "use")
    tab = E.table3(view, T)
    assert tab["three_state"] is three
    assert sorted(tab["keys"]) == sorted(want_keys)
    assert tab["keys"] == OT.payload_table(tab["table"])["keys"]


def test_no_table_use_equals_ignore(tmp_path):
    with_t = make_store(tmp_path / "a.sqlite", key_rules="v2", github=evidence())
    without = make_store(tmp_path / "b.sqlite", key_rules="v2")
    a = E.table3(E.store_view(with_t, T, github_mode="ignore"), T)
    b = E.table3(E.store_view(without, T, github_mode="use"), T)
    assert a["table"]["text"] == b["table"]["text"] and a["table"]["rows"] == b["table"]["rows"]
    assert a["sha256"] == b["sha256"]


# --- 줄 범주·사유 ------------------------------------------------------------------------------------------

def test_row_categories_and_reasons(tmp_path):
    view = E.store_view(make_store(tmp_path / "s.sqlite", key_rules="v2", github=evidence()), T)
    tab = E.table3(view, T)
    rc = E.row_categories(tab["table"]["rows"], eval_cache(), T)
    st = rc["by_state"]
    assert st[OT.ROW_CANDIDATE] == {"rows": 2, "categories": {"open_issue": 1, "closed_completed": 1}}
    assert st[OT.ROW_CLOSED] == {"rows": 1, "categories": {"closed_completed": 1}}
    assert st[OT.ROW_NOT_PLANNED] == {"rows": 1, "categories": {"closed_not_planned": 1}}
    # #412: 최대 번호(#7)보다 큰 없는 번호 #8 은 게이트웨이 기본(over_max_rows drop)처럼 표에서 빠진다
    assert st[OT.ROW_UNKNOWN]["rows"] == 3
    assert rc["unknown_reasons"] == {"repo-missing": 2, "done-but-open": 1}
    assert rc["rows"] == 7 and tab["table"]["over_max"] == 1


def test_row_categories_two_state(tmp_path):
    view = E.store_view(make_store(tmp_path / "s.sqlite", key_rules="v2"), T)
    rc = E.row_categories(E.table3(view, T)["table"]["rows"], eval_cache(), T)
    assert set(rc["by_state"]) == {OT.STATE_OPEN} and rc["by_state"][OT.STATE_OPEN]["rows"] == 7
    assert rc["unknown_reasons"] == {}


# --- 하위군 ----------------------------------------------------------------------------------------------

def test_subgroups_split_by_eval_repo():
    sg = E.subgroup_metrics(ALL_OPEN, eval_cache(), T)
    assert sg["linked"]["keys"] == 5 and sg["linked"]["open_issue"] == 1
    assert sg["linked"]["precision"] == pytest.approx(0.2)
    assert sg["no_github"]["keys"] == 2 and sg["no_github"]["open_issue"] == 0
    assert sg["no_github"]["categories"] == {"missing_repo": 2}
    assert sg["linked"]["categories"]["missing_number"] == 1


def test_subgroups_empty():
    sg = E.subgroup_metrics([], eval_cache(), T)
    assert sg["linked"] == {"keys": 0, "open_issue": 0, "precision": None, "categories": {}}


# --- 알려진 한계 -----------------------------------------------------------------------------------------

def test_reopen_after_t_limit(tmp_path):
    view = E.store_view(make_store(tmp_path / "s.sqlite", key_rules="v2", github=evidence()), T)
    lim = E.reopen_limit(E.table3(view, T)["table"]["rows"], eval_cache(), T)
    assert lim == {"candidate_closed_at_t": 1, "reopened_after_t": 1, "finished_row_open_at_t": 0}


def test_reopen_limit_counts_finished_row_open_at_t():
    rows = [["hindsight-crew#2", OT.ROW_CLOSED, "2026-10-05", "github"]]
    cache = eval_cache()
    cache["hindsight-crew"]["events"]["2"] = [["closed", at(5), None], ["reopened", at(6), None]]
    assert E.reopen_limit(rows, cache, T)["finished_row_open_at_t"] == 1


def test_slug_limit():
    rows = [["my_repo#6", OT.ROW_UNKNOWN, "d", "repo-missing"], ["other-repo#5", OT.ROW_UNKNOWN, "d", "repo-missing"],
            ["a.b#1", OT.ROW_UNKNOWN, "d", "repo-missing"], ["a.b#2", OT.ROW_UNKNOWN, "d", "repo-error"],
            ["x_y#1", OT.ROW_CANDIDATE, "d", ""]]
    assert E.slug_limit(rows) == {"repo_missing_rows": 3, "with_sep_rows": 2, "with_sep_repos": 2}


# --- offline 명령 ----------------------------------------------------------------------------------------

def setup_offline(tmp_path):
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    on = make_store(work / "v2-gh.sqlite", key_rules="v2", github=evidence())
    pre = make_store(work / "v2.sqlite", key_rules="v2")
    v1 = make_store(work / "v1.sqlite")
    (tmp_path / "copy.json").write_text(json.dumps({"copy_end_kst": "2026-11-03T03:53:28+09:00", "sha256": "x"}))
    rows = [{"id": "a", "text": "[진행] #1 #2 남음", "tags": ["project:hindsight-crew"], "state": "valid",
             "mentioned_at": at(10), "document_id": "d-a", "fact_type": "world"},
            {"id": "b", "text": "[완료] #7 끝", "tags": ["project:hindsight-crew"], "state": "valid",
             "mentioned_at": at(20), "document_id": "d-b", "fact_type": "world"}]
    (tmp_path / "listing.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (tmp_path / "aliases.json").write_text(json.dumps(ALIASES))
    (tmp_path / "codes.json").write_text(json.dumps({"hindsight-crew": "r01"}))
    cache = tmp_path / "r7-0-github.json"
    cache.write_text(json.dumps({"calls_total": 4, "repos": eval_cache()}))
    base = {"results": {"ops": {"bank": {"metrics": {"total": {"keys": 9, "open_issue": 1, "precision": 1 / 9}}},
                                "hindsight_crew": {"metrics": {"keys": 5, "open_issue": 1, "precision": 0.2}}}}}
    (tmp_path / "base.json").write_text(json.dumps(base))
    argv = ["offline", "--arm", "on", on, "use", "--arm", "off", on, "ignore", "--arm", "pre", pre, "use",
            "--arm", "v1", v1, "ignore",
            "--ops-copy", str(tmp_path / "copy.json"), "--ops-listing", str(tmp_path / "listing.jsonl"),
            "--aliases", str(tmp_path / "aliases.json"), "--codes", str(tmp_path / "codes.json"),
            "--github-cache", str(cache), "--work", str(work), "--baseline", str(tmp_path / "base.json"), "--offline"]
    return argv, work, cache


def test_offline_command(tmp_path, capsys):
    argv, work, cache = setup_offline(tmp_path)
    before = E.file_sha256(str(cache))
    assert E.main(argv) == 0
    assert E.file_sha256(str(cache)) == before
    s = json.loads((work / "r7-3-summary.json").read_text())
    assert set(s["arms"]) == {"on", "off", "pre", "v1"}
    on, off, pre = s["arms"]["on"], s["arms"]["off"], s["arms"]["pre"]
    assert on["three_state"] is True and off["three_state"] is False and pre["three_state"] is False
    assert on["store"]["key_rules"] == "v2" and on["store"]["github_tables"] is True
    assert pre["store"]["github_tables"] is False and s["arms"]["v1"]["store"]["key_rules"] == "v1"
    assert on["store"]["sha256_before"] == on["store"]["sha256_after"] and on["store"]["in_work_dir"] is True
    assert on["bank"]["metrics"]["total"]["keys"] == 2 and on["bank"]["metrics"]["total"]["open_issue"] == 1
    assert on["bank"]["subgroups"]["linked"]["precision"] == pytest.approx(0.5)
    assert on["bank"]["rows"]["unknown_reasons"] == {"repo-missing": 2, "done-but-open": 1}     # #412: #8 over-max 제외
    assert on["limits"]["reopen"]["reopened_after_t"] == 1 and on["limits"]["slug"]["with_sep_rows"] == 1
    assert off["bank"]["metrics"]["total"]["keys"] == 7
    assert off["table_sha256"] == pre["table_sha256"] and on["table_sha256"] != off["table_sha256"]
    assert ["off", "pre"] in s["equal_tables"]
    assert s["baseline_v1"]["bank"]["keys"] == 9
    assert s["github"]["calls_this_run"] == 0
    assert os.path.exists(work / "r7-3-details.json")
    assert "#" not in capsys.readouterr().out          # 표준 출력에 키 없음


def test_offline_refuses_sealed_and_aliases(tmp_path):
    argv, work, _ = setup_offline(tmp_path)
    sealed = list(argv)
    sealed[sealed.index("--baseline") + 1] = str(tmp_path / "r5-sealed-x.json")
    assert E.main(sealed) == 2
    (tmp_path / "aliases.json").write_text(json.dumps({"zz": "zz-repo"}))
    assert E.main(argv) == 3


def test_offline_missing_repo_in_cache_fails_offline(tmp_path):
    argv, work, cache = setup_offline(tmp_path)
    doc = json.loads(cache.read_text())
    del doc["repos"]["other-repo"]
    cache.write_text(json.dumps(doc))
    assert E.main(argv) == 1


def test_offline_extends_cache_into_new_file(tmp_path, monkeypatch):
    argv, work, cache = setup_offline(tmp_path)
    doc = json.loads(cache.read_text())
    del doc["repos"]["other-repo"]
    cache.write_text(json.dumps(doc))
    before = E.file_sha256(str(cache))
    seen = []

    def fake_fetch(owner, repo, get=None):
        seen.append(repo)
        return {"owner": owner, "status": 404, "calls": 1, "fetched_at": FETCHED}
    monkeypatch.setattr(E, "fetch_repo", fake_fetch)
    monkeypatch.setattr(E, "fill_gaps", lambda rec, repo, wanted, get=None: 0)
    argv = [a for a in argv if a != "--offline"]
    assert E.main(argv) == 0
    assert seen == ["other-repo"] and E.file_sha256(str(cache)) == before
    s = json.loads((work / "r7-3-summary.json").read_text())
    assert s["github"]["calls_this_run"] == 1
    assert json.loads((work / "r7-3-github.json").read_text())["repos"]["other-repo"]["status"] == 404
