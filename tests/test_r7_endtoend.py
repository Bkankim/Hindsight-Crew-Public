"""R7-3a 원래 증상 새 채점식·항목 분해·저장소 준비·렌더 비교(issue #403, `bench/r7_endtoend.py`). 네트워크 0, LLM 0.

고정하는 것:
  * 열린 항목 범주(열린 이슈, 닫힌 이슈, 미계획, 열린 PR, 머지 PR, 없는 번호, t 뒤 생성, 이벤트 없는 재오픈, 리포 없음 x 뱅크
    열림/끝남/없음, 키 없음 x 근거 규칙 열림/끝남/보류/t 뒤) -> 판정과 분자·분모.
  * 옛 `symptom_score` 와 다르게 세는 경우: 대응 없음 항목을 분모에 넣고 범주로 판정한다(옛 채점은 오류로 안 셈).
  * 표 밖 끝남 키 유입(코디네이터 결정 2): 표 없음·2상태 표 열림·3상태 표 반증 줄 x 끝난/열린 키.
  * 반복 집계(무효 반복 제외 평균), 분해 표 코드.
  * add-github: 기억·언급 행 불변, 원본 sha 불변, 근거 테이블만 더함, 새 경로·작업 디렉터리 안만.
  * render: 수정 전 모듈 파일 로드, 게이트웨이 순서 재현, 반증 줄 없는 끝난 결과 키 수.
  * 봉인 경로 exit 2.
"""
import json
import os
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from bench import r5_endtoend as R5
from bench import r7_endtoend as S
from gateway import item_open_table as OT
from gateway import item_roles as IR

KST = timezone(timedelta(hours=9))
T = datetime(2026, 10, 28, 14, 59, 59, tzinfo=timezone.utc)
ALIASES = {"hc": "hindsight-crew", "hindsight-crew": "hindsight-crew"}
FETCHED = "2026-11-05T14:00:00Z"


def at(day, hour=12, month=10):
    return datetime(2026, month, day, hour, tzinfo=KST).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def it(kind="issue", state="open", reason=None, created=at(1), closed=None, merged=None):
    d = {"kind": kind, "state": state, "state_reason": reason, "created_at": created, "closed_at": closed,
         "merged_at": merged}
    if kind == "pr":
        d["merged_known"] = True
    return d


def cache():
    """R7-0 평가 캐시 모양."""
    return {"hindsight-crew": {"status": 200, "owner": "o", "calls": 2, "fetched_at": FETCHED,
                               "items": {"1": it(), "2": it(state="closed", reason="completed", closed=at(5)),
                                         "3": it(state="closed", reason="not_planned", closed=at(5)),
                                         "5": it(kind="pr"),
                                         "6": it(kind="pr", state="closed", closed=at(6), merged=at(6)),
                                         "8": it(created=at(1, month=11)),
                                         "9": it(state="closed", reason="completed", closed=at(7)),
                                         "10": it(state="closed", reason="completed", closed=at(2, month=11))},
                               "events": {"2": [["closed", at(5), None]], "3": [["closed", at(5), None]],
                                          "10": [["closed", at(2, month=11), None]]}},
            "gone": {"status": 404, "owner": "o", "calls": 1, "fetched_at": FETCHED}}


class St:
    def __init__(self, state):
        self.state = state


BANK = {"gone#1": St(OT.STATE_OPEN), "gone#2": St(OT.STATE_DONE), "hindsight-crew#2": St(OT.STATE_DONE)}
MEMS = {
    "m-open": {"id": "m-open", "text": "[진행] 대시보드 정리 작업이 남았다. 다른 문장.", "tags": [], "mentioned_at": at(10)},
    "m-done": {"id": "m-done", "text": "[완료] 대시보드 정리 작업을 마쳤다.", "tags": [], "mentioned_at": at(20)},
    "m-plain": {"id": "m-plain", "text": "회의 기록 한 줄.", "tags": [], "mentioned_at": at(11)},
    "m-comp": {"id": "m-comp", "text": "무관한 문장.", "tags": ["kind:completion"], "mentioned_at": at(12)},
    "m-late": {"id": "m-late", "text": "[진행] 대시보드 정리 작업이 남았다.", "tags": [], "mentioned_at": at(1, month=11)},
}
Q = "대시보드 정리 작업"


def item(key=None, ev=(), quote=Q, status="open", name="x"):
    return {"key": key, "evidence_ids": list(ev), "quote": quote, "status": status, "name": name, "unverified": False}


# 이름, 항목, 판정, 출처, 분해 GitHub 범주, 키 있음, 미연결 키의 보조 규칙 판정(rework #403-1)
U = "unconfirmed"        # GitHub 미연결 키: P_key 분모에만, 분자 아님(문자열로 둬 수정 전 코드에서도 단언으로 실패)
CASES = [
    ("open-issue", item("hindsight-crew#1"), S.OPEN, "github", "open_issue", True, None),
    ("alias-owner-form", item("someone/HC #1"), S.OPEN, "github", "open_issue", True, None),
    ("closed-issue", item("hindsight-crew#2"), S.FINISHED, "github", "closed_completed", True, None),
    ("not-planned", item("hindsight-crew#3"), S.FINISHED, "github", "closed_not_planned", True, None),
    ("pr-open", item("hindsight-crew#5"), S.WRONG, "github", "pr_open", True, None),
    ("pr-merged", item("hindsight-crew#6"), S.FINISHED, "github", "pr_merged", True, None),
    ("missing-number", item("hindsight-crew#99"), S.WRONG, "github", "missing_number", True, None),
    ("created-after-t", item("hindsight-crew#8"), S.WRONG, "github", "not_yet", True, None),
    ("closed-no-events", item("hindsight-crew#9"), S.HOLD, "github", "reopened_unknown", True, None),
    ("closed-after-t", item("hindsight-crew#10"), S.OPEN, "github", "open_issue", True, None),
    ("missing-repo-bank-open", item("gone#1"), U, "bank", "missing_repo", True, S.OPEN),
    ("missing-repo-bank-done", item("gone#2"), U, "bank", "missing_repo", True, S.FINISHED),
    ("missing-repo-no-bank-evidence", item("gone#3", ev=["m-open"]), U, "evidence", "missing_repo", True, S.OPEN),
    ("missing-repo-no-bank-hold", item("gone#3"), U, "evidence", "missing_repo", True, S.HOLD),
    ("repo-not-in-cache", item("elsewhere#4", ev=["m-open"]), U, "evidence", "missing_repo", True, S.OPEN),
    ("pr-notation-key", item("PR #12", ev=["m-open"]), U, "evidence", "missing_repo", True, S.OPEN),
    ("keyless-open", item(None, ev=["m-open"]), S.OPEN, "evidence", S.NO_KEY, False, None),
    ("keyless-done-later", item(None, ev=["m-open", "m-done"]), S.FINISHED, "evidence", S.NO_KEY, False, None),
    ("keyless-completion-tag", item(None, ev=["m-plain", "m-comp"]), S.FINISHED, "evidence", S.NO_KEY, False, None),
    ("keyless-no-signal", item(None, ev=["m-plain"]), S.HOLD, "evidence", S.NO_KEY, False, None),
    ("keyless-no-evidence", item(None), S.HOLD, "evidence", S.NO_KEY, False, None),
    ("keyless-after-t", item(None, ev=["m-late"]), S.HOLD, "evidence", S.NO_KEY, False, None),
    ("bad-key-is-keyless", item("not a key", ev=["m-open"]), S.OPEN, "evidence", S.NO_KEY, False, None),
    ("bare-number-is-keyless", item("#12", ev=["m-open"]), S.OPEN, "evidence", S.NO_KEY, False, None),
]


@pytest.mark.parametrize("name,itm,verdict,source,cat,has_key,rule", CASES, ids=[c[0] for c in CASES])
def test_category_to_numerator_denominator(name, itm, verdict, source, cat, has_key, rule):
    rec = S.classify_item(itm, aliases=ALIASES, gh=cache(), t=T, bank=BANK, rows=None, mems=MEMS)
    assert (rec["verdict"], rec["source"], rec["github"], rec["has_key"]) == (verdict, source, cat, has_key)
    assert rec.get("rule") == rule
    s = S.score_items([rec])
    # 분모는 언제나 1(대응 없음·PR·없는 번호·키 없음·보류·미확인도 분모에 든다), 분자는 open 일 때만 1
    assert s["open_items"] == 1
    assert s["P_all"] == (1.0 if verdict == S.OPEN else 0.0)
    assert s["verdicts"] == {v: int(v == verdict) for v in (S.OPEN, S.FINISHED, S.WRONG, S.HOLD, U)}
    if has_key:
        assert s["keyed"] == 1 and s["keyless"] == 0
        # P_key 분자는 GitHub 로 t 시점 열린 이슈가 확인된 항목만
        assert s["P_key"] == (1.0 if verdict == S.OPEN and source == "github" else 0.0)
        if cat == "missing_repo":
            assert s["linked"] == 0 and s["P_key_linked"] is None and s["P_all_github"] == 0.0
            assert s["unconfirmed"] == 1 and s["unconfirmed_rule"] == {v: int(v == rule)
                                                                      for v in (S.OPEN, S.FINISHED, S.HOLD)}
        else:
            assert s["linked"] == 1 and s["P_key_linked"] == s["P_key"] and s["unconfirmed"] == 0
    else:
        assert s["keyed"] == 0 and s["keyless"] == 1 and s["P_key"] is None
        assert s["keyless_verdicts"][verdict] == 1
        assert s["keyless_bare_number"] == int(name == "bare-number-is-keyless")


def test_llm_judge_interface_only_for_rule_holds():
    calls = []

    def judge(itm, mems):
        calls.append(len(mems))
        return S.FINISHED
    held = S.classify_item(item(None, ev=["m-plain"]), aliases=ALIASES, gh=cache(), t=T, bank={}, rows=None,
                           mems=MEMS, judge=judge)
    ruled = S.classify_item(item(None, ev=["m-open"]), aliases=ALIASES, gh=cache(), t=T, bank={}, rows=None,
                            mems=MEMS, judge=judge)
    assert (held["verdict"], held["reason"]) == (S.FINISHED, "llm-finished")
    assert ruled["reason"] == "rule-open" and calls == [1]


# --- 옛 채점과 다르게 세는 경우(단언으로 고정) ---------------------------------------------------------------

TASKS = [{"id": "s0:a", "category": "closed", "keys": ["hindsight-crew#2"], "keywords": []},
         {"id": "s0:b", "category": "controls", "keys": ["hindsight-crew#1"], "keywords": []}]
GEN = {"valid": True, "items": [
    item("hindsight-crew#1"),               # 대응(controls), GitHub 열림 -> 둘 다 맞음
    item("hindsight-crew#2"),               # 대응(closed), GitHub 닫힘 -> 둘 다 끝난 일
    item("hindsight-crew#6"),               # 대응 없음, 머지 PR -> 옛: 셈 밖, 새: 끝남
    item("hindsight-crew#99"),              # 대응 없음, 없는 번호 -> 옛: 셈 밖, 새: 틀림
    item(None, ev=["m-open", "m-done"]),    # 대응 없음, 키 없음, 근거상 끝남
    item(None, ev=["m-open"]),              # 대응 없음, 키 없음, 근거상 열림
    item("hindsight-crew#1", status="done"),  # 열린 항목 아님(둘 다 셈 밖)
]}


def test_old_symptom_score_counts_unmapped_differently():
    old = R5.symptom_score([GEN], TASKS)
    rep = old["reps"][0]
    assert (rep["open_items"], rep["closed"], rep["controls"], rep["unmapped"]) == (6, 1, 1, 4)
    new = S.score_arm([GEN], aliases=ALIASES, gh=cache(), t=T, bank={}, rows=None, mems=MEMS)
    p = new["pooled"]
    assert (p["open_items"], p["verdicts"][S.OPEN], p["keyed"], p["keyed_verdicts"][S.OPEN]) == (6, 2, 4, 1)
    assert p["keyless_verdicts"] == {S.OPEN: 1, S.FINISHED: 1, S.WRONG: 0, S.HOLD: 0, S.UNCONFIRMED: 0}
    side = S.old_vs_new([GEN], TASKS, new)
    # 옛 채점의 정밀도 꼴(1 - 끝난 일 비율)은 5/6, 새 P_all 은 2/6, 새 P_key 는 1/4
    assert side["old"]["precision_like_mean"] == pytest.approx(5 / 6)
    assert side["new"]["mean"]["P_all"] == pytest.approx(2 / 6)
    assert side["new"]["mean"]["P_key"] == pytest.approx(1 / 4)
    assert side["old"]["precision_like_mean"] != side["new"]["mean"]["P_all"]
    # 표 밖 끝남 키 유입(표 없음): 닫힌 이슈 #2, 머지 PR #6
    assert new["leak_per_rep"] == 2.0


# --- 표 줄·유입 -----------------------------------------------------------------------------------------

def tab2(keys):
    rows = "\n".join("%s | %s" % (k, OT.STATE_OPEN) for k in keys)
    return {"t": "2026-10-28", "fmt": "a", "keys": list(keys), "truncated": 0,
            "text": "<item_open_table>\n머리\n줄: 키 | 상태\n%s\n</item_open_table>" % rows}


def tab3(rows):
    body = "\n".join(" | ".join(r) for r in rows)
    return {"t": "2026-10-28", "fmt": "b", "keys": [r[0] for r in rows if r[1] == OT.ROW_CANDIDATE], "truncated": 0,
            "counts": {}, "fetched_at": FETCHED,
            "text": "<item_open_table>\n머리\n줄: ...\n상태: 설명 | 아님\n%s\n</item_open_table>" % body}


def doc(*tabs):
    return {"runs": [{"q": i, "open_table": t} for i, t in enumerate(tabs, 1)]}


LEAK = [
    # 이름, 응답 문서, 키, 유입, 분해 표 상태
    ("no-table-closed", None, "hindsight-crew#2", True, S.NO_TABLE),
    ("no-table-open", None, "hindsight-crew#1", False, S.NO_TABLE),
    ("2state-open-row-closed", doc(tab2(["hindsight-crew#2"])), "hindsight-crew#2", True, OT.STATE_OPEN),
    ("2state-out-closed", doc(tab2(["hindsight-crew#1"])), "hindsight-crew#2", True, S.OUT_OF_TABLE),
    ("3state-refuted", doc(tab3([["hindsight-crew#2", OT.ROW_CLOSED, "github", "2026-10-05"]])),
     "hindsight-crew#2", False, OT.ROW_CLOSED),
    ("3state-not-planned-refuted", doc(tab3([["hindsight-crew#3", OT.ROW_NOT_PLANNED, "github", "2026-10-05"]])),
     "hindsight-crew#3", False, OT.ROW_NOT_PLANNED),
    ("3state-unknown-not-refuted", doc(tab3([["hindsight-crew#6", OT.ROW_UNKNOWN, "pr-open"]])),
     "hindsight-crew#6", True, OT.ROW_UNKNOWN),
    ("3state-refuted-in-other-query", doc(tab2(["hindsight-crew#2"]),
                                          tab3([["hindsight-crew#2", OT.ROW_CLOSED, "github", "2026-10-05"]])),
     "hindsight-crew#2", False, OT.ROW_CLOSED),
    ("doc-without-tables", doc(None), "hindsight-crew#2", True, S.NO_TABLE),
]


@pytest.mark.parametrize("name,d,key,leak,tstate", LEAK, ids=[c[0] for c in LEAK])
def test_out_of_table_finished_key_leak(name, d, key, leak, tstate):
    rows = S.response_rows(d)
    rec = S.classify_item(item(key), aliases=ALIASES, gh=cache(), t=T, bank=BANK, rows=rows, mems=MEMS)
    assert rec["leak"] is leak
    assert rec["table"] == tstate


def test_bank_leak_and_decomposition_codes():
    rows = S.response_rows(doc(tab2(["hindsight-crew#1"])))
    recs = [S.classify_item(item(k), aliases=ALIASES, gh=cache(), t=T, bank=BANK, rows=rows, mems=MEMS)
            for k in ("hindsight-crew#1", "hindsight-crew#2", None)]
    assert [r["bank_leak"] for r in recs] == [False, True, False]
    assert S.decomposition(recs) == {"key|out|끝남|closed_completed": 1, "key|열림|-|open_issue": 1,
                                     "nokey|-|-|no_key": 1}


def test_table_rows_parses_text_and_keys():
    t3 = tab3([["hindsight-crew#1", OT.ROW_CANDIDATE, "2026-10-01"], ["hindsight-crew#2", OT.ROW_CLOSED, "github",
                                                                       "2026-10-05"]])
    assert S.table_rows(t3) == {"hindsight-crew#1": OT.ROW_CANDIDATE, "hindsight-crew#2": OT.ROW_CLOSED}
    assert S.table_rows(tab2(["hindsight-crew#1"])) == {"hindsight-crew#1": OT.STATE_OPEN}
    assert S.table_rows({"keys": ["a#1"], "text": ""}) == {"a#1": OT.STATE_OPEN}
    assert S.table_rows(None) == {}


def test_score_arm_means_skip_invalid_reps():
    g_ok = {"valid": True, "items": [item("hindsight-crew#1"), item("hindsight-crew#2")]}
    res = S.score_arm([g_ok, {"valid": False}, g_ok], aliases=ALIASES, gh=cache(), t=T, bank={}, rows=None, mems={})
    assert res["K"] == 3 and res["invalid"] == 1
    assert res["mean"]["P_all"] == pytest.approx(0.5)
    assert res["leak_per_rep"] == 1.0
    assert res["pooled"]["open_items"] == 4


def test_score_arm_per_rep_rows_and_count_check():
    g = {"valid": True, "items": [item("hindsight-crew#2")]}
    refuted = S.response_rows(doc(tab3([["hindsight-crew#2", OT.ROW_CLOSED, "github", "2026-10-05"]])))
    plain = S.response_rows(doc(tab2(["hindsight-crew#2"])))
    res = S.score_arm([g, g], aliases=ALIASES, gh=cache(), t=T, bank={}, rows=[refuted, plain], mems={})
    assert [r["leak"] for r in res["reps"]] == [0, 1]
    one = S.score_arm([g, g], aliases=ALIASES, gh=cache(), t=T, bank={}, rows=[refuted], mems={})
    assert [r["leak"] for r in one["reps"]] == [0, 0]
    with pytest.raises(S.Fail):
        S.score_arm([g, g, g], aliases=ALIASES, gh=cache(), t=T, bank={}, rows=[refuted, plain], mems={})


def _cache_files(tmp_path, ext_repos, base_sha=None):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"repos": cache()}))
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    if ext_repos is not None:
        (work / "x.json").write_text(json.dumps({"repos": ext_repos, "base_sha256": base_sha or S.V.file_sha256(
            str(base))}))
    return str(work), str(base)


def test_load_cache_rules(tmp_path):
    work, base = _cache_files(tmp_path, None)
    d, out = S.load_cache(work, base, "x.json")
    assert set(d["repos"]) == {"hindsight-crew", "gone"} and out.endswith("x.json")
    sup = dict(cache(), newrepo={"status": 404})
    work, base = _cache_files(tmp_path, sup)
    assert "newrepo" in S.load_cache(work, base, "x.json")[0]["repos"]
    for bad in ({"hindsight-crew": cache()["hindsight-crew"]},                        # 리포 빠짐
                dict(cache(), **{"hindsight-crew": dict(cache()["hindsight-crew"], items={"1": it()})})):  # 항목 빠짐
        work, base = _cache_files(tmp_path, bad)
        with pytest.raises(S.Fail):
            S.load_cache(work, base, "x.json")
    work, base = _cache_files(tmp_path, sup, base_sha="0" * 64)                     # 다른 기준 캐시에서 넓힘
    with pytest.raises(S.Fail):
        S.load_cache(work, base, "x.json")
    assert S.CACHE_SCORE != S.CACHE_RENDER


# --- GitHub 캐시 확장 -----------------------------------------------------------------------------------

def test_extend_cache_fetches_missing_repo_and_offline_refuses():
    doc_ = {"repos": cache()}
    got = []

    def fetch(owner, repo):
        got.append((owner, repo))
        return {"status": 404, "owner": owner, "calls": 1}

    def fill(rec, repo, wanted):
        return 0
    assert S.extend_cache(doc_, {"newrepo": {1}, "hindsight-crew": {1}}, False, fetch=fetch, fill=fill) == 1
    assert [r for _, r in got] == ["newrepo"]
    with pytest.raises(S.Fail):
        S.extend_cache({"repos": {}}, {"x": {1}}, True, fetch=fetch, fill=fill)


# --- add-github -----------------------------------------------------------------------------------------

def make_store(path, github=None):
    meta = {"model": "m", "prompt_sha256": "p", "aliases_sha256": OT.aliases_sha256(ALIASES), "updated_at": "u"}
    mems = {"a": {"document_id": "d", "mentioned_at": at(10), "state": "valid", "text_sha256": "s", "judged_at": "j"},
            "b": {"document_id": "d", "mentioned_at": at(12), "state": "valid", "text_sha256": "s", "judged_at": "j"}}
    roles = {"a": {"hindsight-crew#1": "open", "hindsight-crew#2": "open", "gone#4": "open"},
             "b": {"hindsight-crew#6": "open"}}
    IR.publish(str(path), meta, mems, roles, mode=0o600, github=github)
    return str(path)


def fake_gh(args):
    path = args[-1]
    if "/gone/" in path:
        return 1, "", "gh: Not Found (HTTP 404)"
    if "/issues?" in path:
        page = [{"number": 1, "state": "open", "state_reason": None, "created_at": at(1), "closed_at": None},
                {"number": 2, "state": "closed", "state_reason": "completed", "created_at": at(1),
                 "closed_at": at(5)},
                {"number": 6, "state": "closed", "state_reason": None, "created_at": at(1), "closed_at": at(6),
                 "pull_request": {"merged_at": at(6)}}]
        return 0, json.dumps([page]), ""
    return 0, json.dumps([[{"number": 6, "merged_at": at(6)}]]), ""


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_add_github_adds_tables_only(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    src = make_store(tmp_path / "src.sqlite")
    sha = S.V.file_sha256(src)
    al = tmp_path / "al.json"
    al.write_text(json.dumps(ALIASES))
    out = str(work / "out.sqlite")
    rc = S.cmd_add_github(Args(pair=[(src, out)], aliases=str(al), owners=None, default_owner_file=None,
                               work=str(work)),
                          cfg_factory=lambda Sc: Sc.GithubConfig(fake_gh, owners={}, default_owner="o", min_interval=0),
                          now_text=FETCHED)
    assert rc == 0
    assert S.V.file_sha256(src) == sha
    rep = json.loads((work / "r7-s0-add-github.json").read_text())
    p = rep["pairs"][0]
    assert p["rows_same"] is True and p["before"]["github_tables"] is False and p["after"]["github_tables"] is True
    assert rep["repo_status"] == {"ok": 1, "missing": 1}
    assert rep["gh_calls"] == 3
    gh = IR.load_github(out)
    assert gh["repos"]["gone"]["status"] == "missing"
    assert set(gh["items"]) == {("hindsight-crew", 1), ("hindsight-crew", 2), ("hindsight-crew", 6)}
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("where", ["exists", "outside-work"])
def test_add_github_refuses_bad_output(tmp_path, where):
    work = tmp_path / "w"
    work.mkdir()
    src = make_store(tmp_path / "src.sqlite")
    out = str(work / "o.sqlite") if where == "exists" else str(tmp_path / "o.sqlite")
    if where == "exists":
        shutil.copyfile(src, out)
    with pytest.raises(S.Fail):
        S.cmd_add_github(Args(pair=[(src, out)], aliases=None, owners=None, default_owner_file=None, work=str(work)),
                         cfg_factory=lambda Sc: None)


# --- render -----------------------------------------------------------------------------------------------

def render_store(tmp_path):
    ev = {"repos": {"hindsight-crew": {"owner": "o", "status": "ok", "fetched_at": FETCHED, "error_code": None}},
          "items": {("hindsight-crew", 1): dict(it(), fetched_at=FETCHED),
                    ("hindsight-crew", 2): dict(it(state="closed", reason="completed", closed=at(5)),
                                                fetched_at=FETCHED)}}
    meta = {"model": "m", "prompt_sha256": "p", "aliases_sha256": OT.aliases_sha256(ALIASES), "updated_at": "u"}
    mems = {m: {"document_id": "d", "mentioned_at": when, "state": "valid", "text_sha256": "s", "judged_at": "j"}
            for m, when in (("a", at(3)), ("b", at(9)))}
    roles = {"a": {"hindsight-crew#1": "open", "hindsight-crew#2": "open", "hindsight-crew#6": "open"},
             "b": {"hindsight-crew#2": "done"}}           # #2 = 뱅크 끝남 + GitHub 닫힘(t 전)
    path = str(tmp_path / "s.sqlite")
    IR.publish(path, meta, mems, roles, mode=0o600, github=ev)
    return path


RROWS = [{"id": "r", "text": "hindsight-crew#1 진행, hindsight-crew#2 그리고 hindsight-crew#6 언급",
          "tags": ["project:hindsight-crew"]}]
RARGS = R5.query_args(None, "남은 할 일 목록", "2026-10-28T14:59:59Z", 0)
NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


class NoResultKeys:
    """#402 이전 모듈 흉내: result_keys 가 없어 build_table 에 결과 키·now 를 넘기지 않는다."""

    def __getattr__(self, name):
        if name == "result_keys":
            raise AttributeError(name)
        return getattr(OT, name)


@pytest.mark.parametrize("label,mod,knobs_extra,state2,missing", [
    ("before-like", NoResultKeys(), {"github_max_age_hours": 0}, None, ["hindsight-crew#2"]),
    ("after-age-off", OT, {"github_max_age_hours": 0}, OT.ROW_CLOSED, []),
    ("after-default-age-stale", OT, {}, OT.ROW_UNKNOWN, ["hindsight-crew#2"]),
    ("after-done-closed-off", OT, {"github_max_age_hours": 0, "done_closed_rows": "off"}, None, ["hindsight-crew#2"]),
], ids=lambda v: v if isinstance(v, str) else "")
def test_render_refutation_rows(tmp_path, label, mod, knobs_extra, state2, missing):
    store = render_store(tmp_path)
    knobs = dict(OT._defaults(), **knobs_extra)
    table, why = S.render_table(mod, RARGS, RROWS, store, ALIASES, knobs, now=NOW)
    assert why == "attached"
    states = {r[0]: r[1] for r in table["rows"]}
    assert states.get("hindsight-crew#2") == state2
    keys = S.result_keys(RROWS, ALIASES, "v1")
    assert keys == {"hindsight-crew#1", "hindsight-crew#2", "hindsight-crew#6"}
    m = S.missing_refutations(table, keys, cache(), T)
    # 끝난 결과 키 = #2(닫힌 이슈), #6(머지 PR: 저장소 근거에 항목 없음 -> 미확인 줄, 반증 아님)
    assert m["finished"] == 2
    assert m["missing_keys"] == sorted(missing + ["hindsight-crew#6"])


def test_load_module_file_and_not_attached(tmp_path):
    before_file = tmp_path / "before.py"
    before_file.write_text(open(OT.__file__, encoding="utf-8").read(), encoding="utf-8")
    before = S.load_module_file(str(before_file), "gateway._r7_test_before")
    assert before is not OT and before.FIELD == OT.FIELD
    assert S.render_table(before, R5.query_args(None, "무관한 질문", "2026-10-28T14:59:59Z", 0), RROWS,
                          render_store(tmp_path), ALIASES, OT._defaults()) == (None, OT.NOT_ATTACHED_NO_TERM)


def test_sealed_input_exit_2(tmp_path):
    rc = S.main(["score", "--gen-dir", str(tmp_path / "sealed-gen"), "--recalls", "on=" + str(tmp_path),
                 "--answer-key", "a", "--store", "s", "--aliases", "a", "--github-cache", "c", "--work", str(tmp_path)])
    assert rc == 2


# --- 단계 A(issue #397): 손잡이·감사 줄·저장소 확인·G3 표 토큰·생성 파일 -----------------------------------

@pytest.mark.parametrize("mode", ["use", "ignore"])
def test_stage_a_knobs_explicit_defaults(mode):
    v = S.knobs_stage_a(mode)
    parsed, warnings = OT.parse_knobs(json.dumps(v, ensure_ascii=False))
    assert warnings == [] and parsed == v
    assert v["github_evidence"] == mode and v["done_closed_rows"] == "result" and v["github_max_age_hours"] == 2
    assert v["header"] == OT.HEADER_DEFAULT and v["table_fmt"] == "a"
    assert {k: v[k] for k in OT.DEFAULTS} == OT._defaults()


def test_stage_a_knobs_arms_differ_and_refuse_other():
    assert OT._canon_sha256(S.knobs_stage_a("use")) != OT._canon_sha256(S.knobs_stage_a("ignore"))
    with pytest.raises(S.Fail):
        S.knobs_stage_a("off")


def _doc(texts, query_ts="2026-10-28T14:59:59Z"):
    return {"query_timestamp": query_ts, "stage": "symptom",
            "runs": [{"q": i, "results": [{"id": "r%d" % i, "text": t, "tags": ["project:hindsight-crew"]}],
                      "open_table": None} for i, t in enumerate(texts, 1)]}


@pytest.mark.parametrize("query,text,key_rules,want", [
    ("무관한 질문", "hindsight-crew#6 진행", "v2", OT.NOT_ATTACHED_NO_TERM),
    ("남은 할 일 목록", "무관", "v2", OT.NOT_ATTACHED_NO_KEYS),
    ("남은 할 일 목록", "hindsight-crew PR #6 머지", "v2", OT.NOT_ATTACHED_NO_KEYS_V2),
    ("남은 할 일 목록", "hindsight-crew PR #6 머지", "v1", S.ATTACHED),
    ("남은 할 일 목록", "hindsight-crew#6 진행", "v2", S.ATTACHED),
], ids=["no-term", "no-keys", "no-keys-v2", "v1-store-keeps-pr", "attached"])
def test_expected_attach_v2(query, text, key_rules, want):
    got = S.expected_attach_v2(_doc([text]), None, [{"text": query}], OT._defaults(), ALIASES, key_rules)
    assert got == {want: 1}


KSHA = "k" * 64


def _line(reason, ts="2026-11-06T01:00:00Z", **info):
    base = {"knobs_source": "file", "knobs_sha256": KSHA, "github_evidence": "use"}
    return json.dumps({"decision": OT.DECISION, "ts": ts, "reason": reason, "item_open_table": dict(base, **info)})


def _use_attached(**over):
    info = {"key_rules": "v2", "counts": {OT.ROW_CANDIDATE: 3}, "unknown_reasons": {}, "github_fetched_at": FETCHED}
    info.update(over)
    return _line("attached", **{k: v for k, v in info.items() if v is not None})


def _ignore_attached(**over):
    info = {"key_rules": "v2", "github_evidence": "ignore"}
    info.update(over)
    return _line("attached", **info)


# 이름, 팔 github 값, 감사 줄, recall 수, 기대(다시 한 판정), 저장 표, ok, 실패 문구 일부
AUDIT_CASES = [
    ("use-ok", "use", [_use_attached()], 1, {"attached": 1}, 1, True, None),
    ("use-with-no-keys-v2", "use", [_use_attached(), _line("not-attached", not_attached="no-result-keys-v2",
                                                             key_rules="v2")], 2,
     {"attached": 1, "no-result-keys-v2": 1}, 1, True, None),
    ("use-stale", "use", [_use_attached(unknown_reasons={"stale": 2})], 1, {"attached": 1}, 1, False, "stale"),
    ("use-no-fetched-at", "use", [_use_attached(github_fetched_at=None)], 1, {"attached": 1}, 1, False,
     "github_fetched_at"),
    ("use-no-counts", "use", [_use_attached(counts=None, unknown_reasons=None)], 1, {"attached": 1}, 1, False, "counts"),
    ("use-key-rules-v1", "use", [_use_attached(key_rules="v1")], 1, {"attached": 1}, 1, False, "key_rules"),
    ("use-wrong-knobs", "use", [_use_attached(knobs_sha256="x" * 64)], 1, {"attached": 1}, 1, False, "knobs_sha256"),
    ("use-evidence-ignore", "use", [_ignore_attached()], 1, {"attached": 1}, 1, False, "github_evidence"),
    ("use-expected-mismatch", "use", [_use_attached()], 1, {"no-status-term": 1}, 1, False, "expected"),
    ("use-saved-mismatch", "use", [_use_attached()], 1, {"attached": 1}, 0, False, "saved tables"),
    ("use-extra-line", "use", [_use_attached(), _use_attached()], 1, {"attached": 1}, 1, False, "lines"),
    ("use-fail-open", "use", [_line("timeout")], 1, {"attached": 1}, 0, False, "other reasons"),
    ("ignore-ok", "ignore", [_ignore_attached()], 1, {"attached": 1}, 1, True, None),
    ("ignore-has-counts", "ignore", [_ignore_attached(counts={OT.ROW_CANDIDATE: 1})], 1, {"attached": 1}, 1, False,
     "ignore arm"),
    ("ignore-evidence-use", "ignore", [_use_attached()], 1, {"attached": 1}, 1, False, "github_evidence"),
]


@pytest.mark.parametrize("name,mode,lines,recalls,expected,saved,ok,why", AUDIT_CASES, ids=[c[0] for c in AUDIT_CASES])
def test_audit_check_v2(name, mode, lines, recalls, expected, saved, ok, why):
    c = S.audit_counts_v2(lines, "2026-11-06T00:00:00Z", "2026-11-06T02:00:00Z")
    got = S.audit_check_v2(c, recalls, KSHA, mode, saved_tables=saved, expected=expected)
    assert got["ok"] is ok, got
    if why:
        assert any(why in f for f in got["fail"]), got


def test_audit_counts_v2_window_and_fields():
    lines = [_use_attached(), _use_attached(ts="2026-11-06T03:00:00Z"), "not json", "",
             _line("not-attached", not_attached={"no-status-term": 2, "no-result-keys": 1})]
    c = S.audit_counts_v2(lines, "2026-11-06T00:00:00Z", "2026-11-06T02:00:00Z")
    assert c["lines"] == 2 and c["reason"] == {"attached": 1, "not-attached": 1}
    assert c["not_attached"] == {"no-status-term": 2, "no-result-keys": 1}
    assert c["key_rules"] == {"v2": 1} and c["github_fetched_at"] == {FETCHED: 1}
    assert c["counts"] == {OT.ROW_CANDIDATE: 3} and c["lines_with_counts"] == 1


def gh_store(tmp_path, fetched=FETCHED, key_rules="v2"):
    ev = {"repos": {"hindsight-crew": {"owner": "o", "status": "ok", "fetched_at": fetched, "error_code": None},
                    "gone": {"owner": "o", "status": "missing", "fetched_at": fetched, "error_code": None}},
          "items": {("hindsight-crew", 1): dict(it(), fetched_at=fetched)}}
    meta = {"model": "m", "prompt_sha256": "p", "aliases_sha256": OT.aliases_sha256(ALIASES), "updated_at": "u"}
    if key_rules:
        meta["key_rules"] = key_rules
    mems = {"a": {"document_id": "d", "mentioned_at": at(3), "state": "valid", "text_sha256": "s", "judged_at": "j"}}
    path = str(tmp_path / "gh.sqlite")
    IR.publish(path, meta, mems, {"a": {"hindsight-crew#1": "open"}}, mode=0o600, github=ev)
    return path


@pytest.mark.parametrize("hours,stale", [(1.5, 0), (2.5, 2)])
def test_store_check_age_and_tables(tmp_path, hours, stale):
    path = gh_store(tmp_path)
    at_ = S.parse_instant(FETCHED) + timedelta(hours=hours)
    got = S.store_check(path, at_)
    assert got["key_rules"] == "v2" and got["github_tables"] is True and got["memories"]["rows"] == 1
    assert got["github"]["repos"] == 2 and got["github"]["repo_status"] == {"missing": 1, "ok": 1}
    assert got["github"]["items"] == 1 and got["github"]["fetched_at"] == FETCHED
    assert got["github"]["age_hours"] == hours and got["github"]["stale_repos"] == stale


def test_store_check_without_tables(tmp_path):
    got = S.store_check(make_store(tmp_path / "plain.sqlite"), NOW)
    assert got["github"] is None and got["github_tables"] is False and got["key_rules"] == "v1"


def test_cmd_audit_end_to_end(tmp_path):
    store = gh_store(tmp_path)
    rec = tmp_path / "rec-on"
    rec.mkdir()
    sym = "남은 할 일 목록"
    doc = dict(_doc(["hindsight-crew#1 진행"]), t="2026-10-28", symptom_sha256=R5.sha256_text(sym))
    doc["runs"][0]["open_table"] = {"keys": ["hindsight-crew#1"]}
    (rec / "t-2026-10-28.json").write_text(json.dumps(doc))
    symf = tmp_path / "q.txt"
    symf.write_text(sym)
    kn = tmp_path / "kn.json"
    kn.write_text(json.dumps(S.knobs_stage_a("use")))
    ksha = OT._canon_sha256(S.knobs_stage_a("use"))
    log = tmp_path / "gateway.log"
    fetched = "2026-11-06T00:30:00Z"
    log.write_text(_line("attached", key_rules="v2", counts={OT.ROW_CANDIDATE: 1}, unknown_reasons={},
                         github_fetched_at=fetched, knobs_sha256=ksha) + "\n")
    store = gh_store(tmp_path, fetched=fetched)
    al = tmp_path / "al.json"
    al.write_text(json.dumps(ALIASES))
    win = {"on": {"start_utc": "2026-11-06T00:00:00Z", "end_utc": "2026-11-06T02:00:00Z", "recalls": 1, "arm": "on",
                  "recalls_dir": str(rec), "symptom_file": str(symf), "knobs_file": str(kn), "store": store},
           "off": {"start_utc": "2026-11-06T02:00:01Z", "end_utc": "2026-11-06T03:00:00Z", "recalls": 1, "arm": "off"}}
    wf = tmp_path / "win.json"
    wf.write_text(json.dumps(win))
    out = tmp_path / "audit.json"
    assert S.main(["audit", "--log", str(log), "--windows", str(wf), "--aliases", str(al), "--out", str(out)]) == 0
    got = json.loads(out.read_text())["windows"]
    assert got["on"]["check"] == {"ok": True, "fail": []}, got["on"]["check"]
    assert got["on"]["expected"] == {"attached": 1} and got["on"]["saved_tables"] == 1
    assert got["on"]["store"]["github"]["age_hours"] == 1.5 and got["on"]["store"]["github"]["stale_repos"] == 0
    assert got["off"]["check"]["ok"] is True


def test_g3_tables_attached_queries_only():
    calls = [{"arm": a, "key": k, "phase": "measure", "round": r, "tokens": t}
             for k, b, c in (("q1", 100, 150), ("q2", 200, 200), ("q3", 50, 52))
             for r in range(3) for a, t in (("base", b), ("cand", c))]
    calls.append({"arm": "cand", "key": "w", "phase": "warmup", "round": -1, "tokens": 999})
    tab = {"t": "2026-10-28", "fmt": "a", "keys": ["hindsight-crew#1"], "truncated": 0, "text": "x",
           "rows": [["hindsight-crew#1", OT.ROW_CANDIDATE, "10-01"], ["hindsight-crew#2", OT.ROW_CLOSED, "10-05"]]}
    results = {("cand", "q1"): [{"payload_rest": {OT.FIELD: tab}}], ("cand", "q2"): [{"payload_rest": {}}],
               ("base", "q1"): [{"payload_rest": {}}]}
    got = S.g3_tables(calls, results, "base", "cand", counter=len)
    assert got["queries"] == 3 and got["base_total"] == 350 and got["cand_total"] == 402
    assert got["attached_queries"] == 1 and got["attached_delta_total"] == 50 and got["attached_delta_max"] == 50
    assert got["other_delta_total"] == 2 and got["other_queries_increased"] == 1
    assert got["attached_row_states"] == [{OT.ROW_CANDIDATE: 1, OT.ROW_CLOSED: 1}]
    assert got["attached_payload_tokens"][0] > 0
    payload = {k: v for k, v in tab.items() if k != "rows"}
    payload["counts"] = {OT.ROW_CANDIDATE: 1}
    got2 = S.g3_tables(calls, {("cand", "q1"): [{"payload_rest": {OT.FIELD: payload}}]}, "base", "cand")
    assert got2["attached_row_states"] == [{OT.ROW_CANDIDATE: 1}] and got2["attached_payload_tokens"] == [None]


def _gen_case(tmp_path, recall_sha=None, stage="symptom"):
    rec = tmp_path / "rec-ignore"
    rec.mkdir()
    (rec / "t-2026-10-28.json").write_text(json.dumps({"t": "2026-10-28"}))
    g = tmp_path / "gen-ignore.json"
    g.write_text(json.dumps({"stage": stage, "arm": "on",
                             "recall_sha256": recall_sha or S.V.file_sha256(str(rec / "t-2026-10-28.json"))}))
    return str(rec), str(g)


@pytest.mark.parametrize("case,ok", [("match", True), ("other-recall", False), ("stage", False), ("no-recalls", False)])
def test_load_symptom_gens_override(tmp_path, case, ok):
    rec, g = _gen_case(tmp_path, recall_sha="0" * 64 if case == "other-recall" else None,
                       stage="dev" if case == "stage" else "symptom")
    recalls = {"ignore": rec} if case != "no-recalls" else {"on": rec}
    if ok:
        assert S.load_symptom_gens(recalls, None, {"ignore": g})["ignore"]["arm"] == "on"
    else:
        with pytest.raises(S.Fail):
            S.load_symptom_gens(recalls, None, {"ignore": g})


def test_load_symptom_gens_default_needs_gen_dir(tmp_path):
    with pytest.raises(S.Fail):
        S.load_symptom_gens({"on": str(tmp_path)}, None, {})


# --- 단계 B: dev 짝 채점(issue #397 단계 B) ---------------------------------------------------------------

T_A, T_B = "2026-10-07", "2026-10-10"
TI = {T_A: datetime(2026, 10, 7, 14, 59, 59, tzinfo=timezone.utc), T_B: datetime(2026, 10, 10, 14, 59, 59,
                                                                                 tzinfo=timezone.utc)}


def dev_cache():
    """t_A(10-07) 시점: 1 열림, 2 닫힘(10-05), 4 열림, 5 PR, 8 아직 없음, 9 열림(10-07 뒤 닫힘), 11 열림.
    t_B(10-10) 시점: 9 닫힘(10-08). 리포 gone 은 404."""
    return {"hindsight-crew": {"status": 200, "owner": "o", "calls": 2, "fetched_at": FETCHED,
                               "items": {"1": it(), "2": it(state="closed", reason="completed", closed=at(5)),
                                         "4": it(), "5": it(kind="pr"), "8": it(created=at(1, month=11)),
                                         "9": it(state="closed", reason="completed", closed=at(8)),
                                         "11": it()},
                               "events": {"2": [["closed", at(5), None]], "9": [["closed", at(8), None]]}},
            "gone": {"status": 404, "owner": "o", "calls": 1, "fetched_at": FETCHED}}


def gen(*keysets, invalid=()):
    out = []
    for i, ks in enumerate(keysets):
        if i in invalid:
            out.append({"valid": False, "items": []})
        else:
            out.append({"valid": True, "items": [item(k) for k in ks]})
    return out


def test_recall_denominator_open_issues_only():
    den, unknown = S.recall_denominator(dev_cache(), TI[T_A], ["hindsight-crew", "gone", "absent"])
    assert den == {"hindsight-crew#1", "hindsight-crew#4", "hindsight-crew#9", "hindsight-crew#11"}
    assert unknown == 0
    den_b, _ = S.recall_denominator(dev_cache(), TI[T_B], ["hindsight-crew"])
    assert "hindsight-crew#9" not in den_b and len(den_b) == 3


def test_rep_open_keys_and_pair_states():
    g = gen(["hc#1", "hindsight-crew#4"], ["hindsight-crew#1"], [], invalid=(2,))
    g[1]["items"].append(item("hindsight-crew#4", status="done"))
    reps = S.rep_open_keys(g, ALIASES)
    assert reps == [{"hindsight-crew#1", "hindsight-crew#4"}, {"hindsight-crew#1"}, None]
    st = S.pair_states(reps, ["hindsight-crew#1", "hindsight-crew#4", "hindsight-crew#11"])
    # 과반 = 2/3: #1 열림 2 -> open, #4 열림 1 + 무효 1 -> invalid, #11 -> not_open
    assert st == {"hindsight-crew#1": "open", "hindsight-crew#4": "invalid", "hindsight-crew#11": "not_open"}


# 이름, 켬 반복 키(t_A, t_B), 끔 반복 키(t_A, t_B), 기대 R 켬, 기대 R 끔
RCASES = [
    ("same", [["hindsight-crew#1"]] * 3, [["hindsight-crew#1"]] * 3, [["hindsight-crew#1"]] * 3,
     [["hindsight-crew#1"]] * 3, 2 / 7, 2 / 7),
    ("on-better", [["hindsight-crew#1", "hindsight-crew#4"]] * 3, [["hindsight-crew#4", "hindsight-crew#11"]] * 3,
     [["hindsight-crew#1"]] * 3, [[]] * 3, 4 / 7, 1 / 7),
    ("closed-key-not-counted", [["hindsight-crew#2", "hindsight-crew#9"]] * 3, [["hindsight-crew#9"]] * 3,
     [[]] * 3, [[]] * 3, 1 / 7, 0.0),
]


@pytest.mark.parametrize("name,on_a,on_b,off_a,off_b,r_on,r_off", RCASES, ids=[c[0] for c in RCASES])
def test_dev_arms_recall_and_paired(name, on_a, on_b, off_a, off_b, r_on, r_off):
    gh = dev_cache()
    den = {t: S.recall_denominator(gh, TI[t], ["hindsight-crew"])[0] for t in (T_A, T_B)}
    arms = {}
    for arm, (a, b) in (("on", (on_a, on_b)), ("off", (off_a, off_b))):
        arms[arm] = S.dev_arm({T_A: gen(*a), T_B: gen(*b)}, {T_A: None, T_B: None}, aliases=ALIASES, gh=gh, ts=TI,
                              banks={T_A: {}, T_B: {}}, den=den, ctl={T_A: set(), T_B: set()}, mems={T_A: {}, T_B: {}})
    assert arms["on"]["R"]["value"] == pytest.approx(r_on)
    assert arms["off"]["R"]["value"] == pytest.approx(r_off)
    assert arms["on"]["R"]["den"] == 7
    pr = S.paired_recall(arms["on"]["_pairs"], arms["off"]["_pairs"])
    assert pr["diff"] == pytest.approx(r_on - r_off)
    assert pr["half_width"] == pytest.approx((pr["hi"] - pr["lo"]) / 2)
    assert pr["clusters"] == 4 and pr["pairs"] == 7


def test_dev_arm_subgroups_unknown_raised_and_p_key():
    gh = dev_cache()
    t3 = tab3([["hindsight-crew#11", OT.ROW_UNKNOWN, "done-but-open"], ["hindsight-crew#1", OT.ROW_CANDIDATE,
                                                                         "2026-10-01"]])
    d = doc(t3)
    keys = ["hindsight-crew#1", "hindsight-crew#11", "hindsight-crew#2", "gone#1", None]
    den = {T_A: S.recall_denominator(gh, TI[T_A], ["hindsight-crew"])[0]}
    ctl = {T_A: {"gone#1", "gone#7", "hindsight-crew#4"}}
    res = S.dev_arm({T_A: gen(keys, keys)}, {T_A: d}, aliases=ALIASES, gh=gh, ts={T_A: TI[T_A]},
                    banks={T_A: BANK}, den=den, ctl=ctl, mems={T_A: MEMS})
    pooled = res["pooled"]
    assert pooled["keyed"] == 8 and pooled["linked"] == 6 and pooled["linked_open"] == 4
    assert res["P_key"] == pytest.approx(4 / 8)
    assert res["unknown_raised"] == 2            # 미확인·충돌 줄 키(#11)를 열림으로 낸 항목 x 2반복
    sg = res["subgroups"]
    assert sg["linked"]["P_den"] == 6 and sg["linked"]["P_num"] == 4 and sg["linked"]["R_den"] == 4
    assert sg["unlinked"]["P_den"] == 2 and sg["unlinked"]["rule"]["open"] == 2
    assert sg["unlinked"]["R_label"] == {"den": 2, "num": 1, "value": 0.5}
    assert sg["no_key"]["P_den"] == 2 and sg["no_key"]["R_den"] is None
    assert res["leak_per_rep"] == pytest.approx(1.0)   # #2 닫힘, 표에 반증 줄 없음


def test_paired_recall_refuses_different_pairs():
    with pytest.raises(ValueError):
        S.paired_recall({("a#1", T_A): 1}, {("a#2", T_A): 1})


def _dev_files(tmp_path, arm, keys_by_t, bad_sha=False):
    rec = tmp_path / ("rec-" + arm)
    gdir = tmp_path / ("gen-" + arm)
    rec.mkdir()
    gdir.mkdir()
    for t, keys in keys_by_t.items():
        p = rec / ("t-%s.json" % t)
        p.write_text(json.dumps({"t": t, "stage": "dev", "query_timestamp": TI[t].strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "runs": [{"q": 1, "results": []}]}))
        g = {"stage": "dev", "t": t, "k": 3, "label": "dev-" + arm, "generations": gen(keys, keys, keys),
             "recall_sha256": "0" * 64 if bad_sha else S.V.file_sha256(str(p))}
        (gdir / ("dev-%s-%s-gen.json" % (arm, t))).write_text(json.dumps(g))
    return str(rec), str(gdir)


def _dev_args(tmp_path, bad_sha=False, sealed=False):
    rec_on, g_on = _dev_files(tmp_path, "on", {T_A: ["hindsight-crew#1", "hindsight-crew#4"], T_B: ["hindsight-crew#4"]},
                              bad_sha)
    rec_off, g_off = _dev_files(tmp_path, "off", {T_A: ["hindsight-crew#1"], T_B: []})
    store = make_store(tmp_path / "store.sqlite")
    al = tmp_path / "al.json"
    al.write_text(json.dumps(ALIASES))
    base = tmp_path / ("sealed-cache.json" if sealed else "cache.json")
    base.write_text(json.dumps({"repos": dev_cache(), "calls_total": 3}))
    work = tmp_path / "w"
    work.mkdir(mode=0o700)
    return ["dev-score", "--gen", "on=" + g_on, "--gen", "off=" + g_off, "--recalls", "on=" + rec_on,
            "--recalls", "off=" + rec_off, "--t", T_A, "--t", T_B, "--store", store, "--aliases", str(al),
            "--github-cache", str(base), "--work", str(work), "--offline"], work


def test_cmd_dev_score_end_to_end(tmp_path, capsys):
    argv, work = _dev_args(tmp_path)
    assert S.main(argv) == 0
    out = json.loads((work / "r7-b-dev-score.json").read_text())
    assert out["arms"]["on"]["R"] == {"den": 7, "num": 3, "value": pytest.approx(3 / 7)}
    assert out["arms"]["off"]["R"]["num"] == 1
    assert out["paired_on_minus_off"]["R"]["diff"] == pytest.approx(2 / 7)
    assert "half_width" in out["paired_on_minus_off"]["R"]
    assert oct(os.stat(work / "r7-b-dev-score.json").st_mode & 0o777) == "0o600"
    printed = capsys.readouterr().out
    assert "hindsight-crew#" not in printed            # 표준 출력에 키 원문 없음


def test_cmd_dev_score_refuses_foreign_gen(tmp_path):
    argv, _ = _dev_args(tmp_path, bad_sha=True)
    assert S.main(argv) == 1


def test_cmd_dev_score_sealed_path_exit_2(tmp_path):
    argv, _ = _dev_args(tmp_path, sealed=True)
    assert S.main(argv) == 2


def test_dev_arm_invalid_rep_and_unknown_row_hits():
    gh = dev_cache()
    den = {T_A: S.recall_denominator(gh, TI[T_A], ["hindsight-crew"])[0]}
    d = doc(tab3([["hindsight-crew#11", OT.ROW_UNKNOWN, "done-but-open"]]))
    g = gen(["hindsight-crew#11", "hindsight-crew#1"], ["hindsight-crew#11"], [], invalid=(2,))
    res = S.dev_arm({T_A: g}, {T_A: d}, aliases=ALIASES, gh=gh, ts={T_A: TI[T_A]}, banks={T_A: {}}, den=den,
                    ctl={T_A: set()}, mems={T_A: {}})
    assert res["valid_reps"] == 2
    assert res["R"] == {"den": 4, "num": 1, "value": 0.25}       # #11 열림 2/3 과반, #1 열림 1 + 무효 1 = invalid -> 0
    assert res["R_rep_mean"] == pytest.approx((2 / 4 + 1 / 4) / 2)
    assert res["R_hits_unknown_row"] == 1


@pytest.mark.parametrize("case", ["ts-mismatch", "k-mismatch", "ts-other-day", "ts-sealed"])
def test_cmd_dev_score_refuses_bad_inputs(tmp_path, case):
    argv, work = _dev_args(tmp_path)
    rec_off = tmp_path / "rec-off" / ("t-%s.json" % T_A)
    gfile = tmp_path / "gen-off" / ("dev-off-%s-gen.json" % T_A)
    d = json.loads(rec_off.read_text())
    if case == "k-mismatch":
        g = json.loads(gfile.read_text())
        g["k"], g["generations"] = 2, g["generations"][:2]
        gfile.write_text(json.dumps(g))
        assert S.main(argv) == 1
        return
    d["query_timestamp"] = {"ts-mismatch": "2026-10-07T10:00:00Z", "ts-other-day": "2026-10-08T14:59:59Z",
                            "ts-sealed": "2026-10-20T14:59:59Z"}[case]
    rec_off.write_text(json.dumps(d))
    g = json.loads(gfile.read_text())
    g["recall_sha256"] = S.V.file_sha256(str(rec_off))
    gfile.write_text(json.dumps(g))
    if case != "ts-mismatch":           # 두 팔 같은 시각으로 맞춰 시각 자체 검사만 본다
        rec_on = tmp_path / "rec-on" / ("t-%s.json" % T_A)
        d2 = json.loads(rec_on.read_text())
        d2["query_timestamp"] = d["query_timestamp"]
        rec_on.write_text(json.dumps(d2))
        g2f = tmp_path / "gen-on" / ("dev-on-%s-gen.json" % T_A)
        g2 = json.loads(g2f.read_text())
        g2["recall_sha256"] = S.V.file_sha256(str(rec_on))
        g2f.write_text(json.dumps(g2))
    assert S.main(argv) == (2 if case == "ts-sealed" else 1)


def test_cmd_dev_score_list_repo_range(tmp_path):
    argv, work = _dev_args(tmp_path)
    gfile = tmp_path / "gen-on" / ("dev-on-%s-gen.json" % T_A)
    g = json.loads(gfile.read_text())
    for gg in g["generations"]:
        gg["items"].append(item("gone#3"))
    gfile.write_text(json.dumps(g))
    assert S.main(argv) == 0
    out = json.loads((work / "r7-b-dev-score.json").read_text())
    assert out["github"]["repos_range"] == 2 and out["github"]["repos_linked"] == 1
    assert out["arms"]["on"]["R"]["den"] == 7                     # 404 리포는 분모에 없음
    assert out["arms"]["on"]["subgroups"]["unlinked"]["P_den"] == 3


# --- 운영 시점 연막 실행기(issue #408) ------------------------------------------------------------------------
# 고정하는 것: t0 불일치(요청·프롬프트·채점) 실패, 10-28 경로 재현(score 기본 = --t0 SYMPTOM_TS 바이트 동일 + 고정 요약
# sha256), 거짓 열림 범주별 셈(GitHub 종결·없는 번호·표 반증 x 표 안팎), 미부착·필드 누락·빈 분모·유효 생성 부족 실패,
# 새 GitHub 캐시(t0 뒤에 받은 것만), 표 키 전체 대조, 소유자 커버·포크 확인.

from bench import d_llm as DL  # noqa: E402

T0 = datetime(2026, 11, 6, 1, 0, 0, tzinfo=timezone.utc)          # KST 2026-11-06 10:00
T0_TEXT = "2026-11-06T01:00:00Z"
T0_DAY = "2026-11-06"
GH_AT = "2026-11-06T01:05:00Z"                                     # t0 뒤에 받은 평가 캐시


def at11(day, hour=12):
    return at(day, hour, month=11)


def smoke_cache(fetched=GH_AT):
    """t0 에 새로 받은 평가 캐시 모양(R7-0 캐시와 같은 필드)."""
    return {"hindsight-crew": {"status": 200, "owner": "o", "calls": 2, "fetched_at": fetched,
                               "items": {"1": it(), "2": it(state="closed", reason="completed", closed=at11(2)),
                                         "3": it(state="closed", reason="not_planned", closed=at11(2)),
                                         "5": it(kind="pr"),
                                         "6": it(kind="pr", state="closed", closed=at11(3), merged=at11(3)),
                                         "7": it()},
                               "events": {"2": [["closed", at11(2), None]], "3": [["closed", at11(2), "not_planned"]]}},
            "gone": {"status": 404, "owner": "o", "calls": 1, "fetched_at": fetched}}


def smoke_store(tmp_path, fetched="2026-11-06T00:30:00Z", name="smoke.sqlite"):
    """v2 저장소 + 근거 테이블. #1 열림(GitHub 열림), #2 열림(GitHub 닫힘 -> 확인된 종결), #7 열림(근거는 닫힘, t0 평가는
    열림 -> 표 반증 줄인데 GitHub 열림), #5 열림(열린 PR -> 미확인·충돌 pr-open)."""
    ev = {"repos": {"hindsight-crew": {"owner": "o", "status": "ok", "fetched_at": fetched, "error_code": None}},
          "items": {("hindsight-crew", 1): dict(it(), fetched_at=fetched),
                    ("hindsight-crew", 2): dict(it(state="closed", reason="completed", closed=at11(2)),
                                                fetched_at=fetched),
                    ("hindsight-crew", 5): dict(it(kind="pr"), fetched_at=fetched),
                    ("hindsight-crew", 7): dict(it(state="closed", reason="completed", closed=at11(1)),
                                                fetched_at=fetched)}}
    meta = {"model": "m", "prompt_sha256": "p", "aliases_sha256": OT.aliases_sha256(ALIASES), "updated_at": "u",
            "key_rules": "v2"}
    mems = {"a": {"document_id": "d", "mentioned_at": at(20), "state": "valid", "text_sha256": "s", "judged_at": "j"}}
    roles = {"a": {"hindsight-crew#1": "open", "hindsight-crew#2": "open", "hindsight-crew#5": "open",
                   "hindsight-crew#7": "open"}}
    path = str(tmp_path / name)
    IR.publish(path, meta, mems, roles, mode=0o600, github=ev)
    return path


SROWS = [{"id": "r1", "text": "hindsight-crew#1 진행, hindsight-crew#2 와 hindsight-crew#7 정리 남음",
          "tags": ["project:hindsight-crew"], "mentioned_at": at(20), "type": "world"},
         {"id": "r2", "text": "[진행] 대시보드 정리 작업이 남았다.", "tags": [], "mentioned_at": at(21), "type": "world"}]


def smoke_recall(query_ts=T0_TEXT, rows=SROWS, table=None):
    return {"stage": "smoke", "t0": query_ts, "query_timestamp": query_ts, "max_tokens": 16384,
            "runs": [{"q": 1, "qid": "symptom", "scope": "symptom", "query": "지금 남은 할 일 목록 알려줘",
                      "results": list(rows), "open_table": table,
                      "error": None, "rows": len(rows)}],
            "totals": {"queries": 1, "errors": 0, "rows": len(rows), "tables": int(table is not None)}}


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False))
    return str(path)


def attached_recall(tmp_path, store=None, query_ts=T0_TEXT):
    """오프라인 렌더로 표를 붙인 recall 파일(smoke-attach 결과)."""
    store = store or smoke_store(tmp_path)
    src = write_json(tmp_path / "rec-raw.json", smoke_recall(query_ts))
    al = write_json(tmp_path / "al.json", ALIASES)
    out = tmp_path / "rec-attached.json"
    rc = S.main(["smoke-attach", "--recall", src, "--store", store, "--aliases", al, "--out", str(out)])
    assert rc == 0
    return str(out), store, al


def gens_of(*item_lists, invalid=0):
    out = [{"valid": True, "items": list(items), "calls": [{"cost_usd": 0.01}]} for items in item_lists]
    out += [{"valid": False, "items": [], "calls": [{"cost_usd": 0.01}]} for _ in range(invalid)]
    return out


def smoke_gen(tmp_path, recall_path, gens, t0=T0_TEXT, day=T0_DAY, drop=(), name="smoke-gen.json"):
    """smoke-dllm 이 쓰는 머리 + 생성(가짜). 프롬프트 sha 는 실제 조립(기준일 day)으로 만든다."""
    doc_ = json.loads(open(recall_path, encoding="utf-8").read())
    b = R5.build_input(DL, DL.load_prompt(DL.BASELINE_PROMPT), doc_, day, 200000)
    head = {"label": "smoke", "stage": "smoke", "arm": "on", "t": day, "t0": t0, "k": len(gens),
            "recall_sha256": S.V.file_sha256(recall_path), "prompt_sha256": S.V.file_sha256(DL.BASELINE_PROMPT),
            "prompt_text_sha256": R5.sha256_text(b["prompt"]), "table_sha256": b["table"]["sha256"],
            "max_input_chars": 200000, "finished_kst": "x"}
    for k in drop:
        head.pop(k, None)
    return write_json(tmp_path / name, dict(head, generations=gens))


# 이름, 항목, 거짓 열림 사유(없으면 None), 표 안(True)/밖(False)/해당 없음(None)
FALSE_OPEN = [
    ("open-issue-in-table", item("hindsight-crew#1"), None, None),
    ("closed-issue-in-table", item("hindsight-crew#2"), "github-finished", True),
    ("not-planned-out-of-table", item("hindsight-crew#3"), "github-finished", False),
    ("merged-pr-out-of-table", item("hindsight-crew#6"), "github-finished", False),
    ("missing-number", item("hindsight-crew#99"), "missing-number", False),
    ("refuted-row-github-open", item("hindsight-crew#7"), "table-refuted", True),
    ("open-pr", item("hindsight-crew#5"), None, None),
    ("unlinked-repo", item("gone#1"), None, None),
    ("keyless", item(None, ev=["r2"]), None, None),
]


def smoke_rows():
    """smoke_store 로 렌더한 표 줄(텍스트 파싱) - 응답 표 하나."""
    rec, _, _ = attached_recall_cached["v"]
    return S.response_rows(json.loads(open(rec, encoding="utf-8").read()))


attached_recall_cached = {}


@pytest.fixture
def attached(tmp_path):
    attached_recall_cached["v"] = attached_recall(tmp_path)
    return attached_recall_cached["v"]


def test_smoke_attach_renders_three_state_table(attached):
    rec, store, _ = attached
    d = json.loads(open(rec, encoding="utf-8").read())
    tab = d["runs"][0]["open_table"]
    assert d["runs"][0]["attach_reason"] == "attached" and d["attached_offline"]["store_sha256"] == S.V.file_sha256(store)
    assert set(S.SMOKE_TABLE_FIELDS) <= set(tab)
    states = {r[0]: r[1] for r in S.table_lines(tab)}
    assert states["hindsight-crew#1"] == OT.ROW_CANDIDATE and states["hindsight-crew#2"] == OT.ROW_CLOSED
    assert states["hindsight-crew#7"] == OT.ROW_CLOSED and states["hindsight-crew#5"] == OT.ROW_UNKNOWN


def test_smoke_attach_refuses_already_attached(tmp_path):
    store = smoke_store(tmp_path)
    src = write_json(tmp_path / "rec.json", smoke_recall(table={"text": "x"}))
    al = write_json(tmp_path / "al.json", ALIASES)
    assert S.main(["smoke-attach", "--recall", src, "--store", store, "--aliases", al,
                   "--out", str(tmp_path / "o.json")]) == 1


@pytest.mark.parametrize("name,itm,why,in_table", FALSE_OPEN, ids=[c[0] for c in FALSE_OPEN])
def test_false_open_categories(attached, name, itm, why, in_table):
    rows = smoke_rows()
    rec = S.classify_item(itm, aliases=ALIASES, gh=smoke_cache(), t=T0, bank={}, rows=rows, mems={})
    assert S.false_open_reason(rec, rows) == why
    filler = item("hindsight-crew#1")             # #412: 빈 분모(실행기 실패 = 롤백 범주)와 섞이지 않게
    res = S.smoke_score(gens_of([itm, filler]), aliases=ALIASES, gh=smoke_cache(), t0=T0, bank={}, rows=rows, mems={},
                        min_valid=1)
    assert res["fail"] == []
    fo = res["gate"]["false_open"]
    rollback = why in S.ROLLBACK_REASONS          # #412 관문 재정의: 없는 번호는 공개만
    assert fo["total"] == int(rollback)
    if rollback:
        assert fo["by_reason"] == {why: 1}
        assert (fo["in_table"], fo["out_of_table"]) == ((1, 0) if in_table else (0, 1))
    if why == S.FALSE_MISSING:
        assert sum(res["disclosure"]["missing_number_open"].values()) == 1
    assert res["reference"]["open_pr_items"] == int(name == "open-pr")
    assert res["gate"]["verdict"] == ("rollback" if rollback else "pass")


def test_smoke_score_residual_keyless_and_reference(attached):
    rows = smoke_rows()
    items = [item("hindsight-crew#1"), item("hindsight-crew#2", status="residual"),
             item("hindsight-crew#1", status="residual"), item(None, ev=["r2"]), item("hindsight-crew#5")]
    res = S.smoke_score(gens_of(items, items), aliases=ALIASES, gh=smoke_cache(), t0=T0, bank={}, rows=rows,
                        mems={"r2": SROWS[1]}, min_valid=2)
    ref = res["reference"]
    assert ref["residual_finished"] == {"items": 2, "unique_keys": 1}
    assert ref["keyless"] == {"items": 2, "finished": 0, "open": 2, "hold": 0}
    assert ref["open_pr_items"] == 2 and ref["valid_reps"] == 2
    assert ref["P_key"] == pytest.approx(0.5) and ref["P_all"] == pytest.approx(4 / 6)
    assert res["fail"] == [] and res["gate"]["false_open"]["total"] == 0
    assert res["_residual_keys"] == ["hindsight-crew#2"]


# 이름, 생성, 최소 유효, 실패 코드
DENOM = [
    ("ok", gens_of([item("hindsight-crew#1")]), 1, []),
    ("no-open-items", gens_of([item("hindsight-crew#1", status="done")]), 1,
     ["empty:open-items", "empty:keyed", "empty:linked"]),
    ("keyless-only", gens_of([item(None)]), 1, ["empty:keyed", "empty:linked"]),
    ("unlinked-only", gens_of([item("gone#1")]), 1, ["empty:linked"]),
    ("valid-short", gens_of([item("hindsight-crew#1")], invalid=2), 3, ["valid-reps"]),
]


@pytest.mark.parametrize("name,gens,min_valid,fail", DENOM, ids=[c[0] for c in DENOM])
def test_smoke_score_empty_denominator_and_valid_reps(attached, name, gens, min_valid, fail):
    res = S.smoke_score(gens, aliases=ALIASES, gh=smoke_cache(), t0=T0, bank={}, rows=smoke_rows(), mems={},
                        min_valid=min_valid)
    assert res["fail"] == fail


# 요청(recall query_timestamp)·프롬프트(생성 머리 t = 프롬프트 기준일)·채점(--t0) 중 하나만 어긋남
T0_CASES = [
    ("match", T0_TEXT, T0_DAY, T0_TEXT, []),
    ("request", "2026-11-06T00:59:00Z", T0_DAY, T0_TEXT, ["t0-request"]),
    ("prompt", T0_TEXT, "2026-11-05", T0_TEXT, ["t0-prompt"]),
    ("score", T0_TEXT, T0_DAY, "2026-11-06T02:00:00Z", ["t0-score"]),
]


@pytest.mark.parametrize("name,req,day,gen_t0,want", T0_CASES, ids=[c[0] for c in T0_CASES])
def test_t0_check(name, req, day, gen_t0, want):
    got = S.t0_check(T0, {"query_timestamp": req}, {"t": day, "t0": gen_t0})
    assert got == want


def _smoke_argv(tmp_path, rec, store, al, gen, extra=()):
    owners = write_json(tmp_path / "owners.json", {"hindsight-crew": "o"})
    work = tmp_path / "w"
    work.mkdir(mode=0o700, exist_ok=True)
    bodies, meta = tmp_path / "bodies.jsonl", tmp_path / "meta.jsonl"      # #412 교차 충돌용 저장 나열
    bodies.write_text(json.dumps({"id": "a", "text": SROWS[0]["text"]}, ensure_ascii=False) + "\n")
    meta.write_text(json.dumps({"id": "a", "tags": ["project:hindsight-crew"], "state": "valid", "document_id": "d",
                                "mentioned_at": at(20), "fact_type": "world"}) + "\n")
    return ["smoke", "--t0", T0_TEXT, "--gen", gen, "--recall", rec, "--store", store, "--aliases", al,
            "--github-owners", owners, "--github-default-owner", "o", "--work", str(work),
            "--listing", str(bodies), str(meta), *extra], work


def _fetch_factory(fetched=GH_AT, log=None):
    def fetch(owner, repo):
        if log is not None:
            log.append((owner, repo))
        rec = smoke_cache(fetched).get(repo) or {"status": 404, "calls": 1, "fetched_at": fetched}
        return dict(rec, owner=owner)

    def fill(rec, repo, wanted):
        return 0
    return fetch, fill


# 이름, 생성 항목(반복마다 같음), 반복 수, 추가 인자, 머리에서 뺄 칸, 종료 코드, 실패 코드 일부
SMOKE_CASES = [
    ("pass", [item("hindsight-crew#1"), item(None, ev=["r2"])], 3, (), (), 0, []),
    ("rollback", [item("hindsight-crew#1"), item("hindsight-crew#2")], 3, (), (), 4, []),
    ("valid-short", [item("hindsight-crew#1")], 2, ("--min-valid", "3"), (), 1, ["valid-reps"]),
    ("empty-denominator", [item(None, ev=["r2"])], 3, (), (), 1, ["empty:keyed"]),
    ("field-missing", [item("hindsight-crew#1")], 3, (), ("t0",), 1, ["field:gen.t0"]),
    ("score-t0", [item("hindsight-crew#1")], 3, ("--t0", "2026-11-06T02:00:00Z"), (), 1, ["t0-score"]),
]


@pytest.mark.parametrize("name,items,k,extra,drop,rc,fail", SMOKE_CASES, ids=[c[0] for c in SMOKE_CASES])
def test_cmd_smoke_end_to_end(tmp_path, name, items, k, extra, drop, rc, fail):
    rec, store, al = attached_recall(tmp_path)
    gen = smoke_gen(tmp_path, rec, gens_of(*([items] * k)), drop=drop)
    argv, work = _smoke_argv(tmp_path, rec, store, al, gen, extra)
    fetch, fill = _fetch_factory()
    assert S.cmd_smoke(S.parse_args(argv), fetch=fetch, fill=fill) == rc
    out = json.loads((work / "r7-4-smoke.json").read_text())
    for code in fail:
        assert code in out["fail"]
    if rc == 0:
        assert out["fail"] == [] and out["gate"]["verdict"] == "pass"
        assert out["inputs"]["t0"] == T0_TEXT and out["github"]["fetched_after_t0"] is True
        assert out["tables"]["symptom"]["candidate"]["rows"] == 1
    if rc == 4:
        assert out["gate"]["false_open"]["total"] == 3 and out["gate"]["false_open"]["in_table"] == 3


@pytest.mark.parametrize("case", ["no-table", "table-field", "request-t0", "prompt-day"])
def test_cmd_smoke_input_failures(tmp_path, case):
    rec, store, al = attached_recall(tmp_path)
    d = json.loads(open(rec, encoding="utf-8").read())
    if case == "no-table":
        d["runs"][0]["open_table"] = None
    elif case == "table-field":
        del d["runs"][0]["open_table"]["counts"]
    elif case == "request-t0":
        d["query_timestamp"] = "2026-11-06T00:00:00Z"
    write_json(tmp_path / "rec-attached.json", d)
    gen = smoke_gen(tmp_path, rec, gens_of(*([[item("hindsight-crew#1")]] * 3)),
                    day="2026-11-05" if case == "prompt-day" else T0_DAY)
    argv, work = _smoke_argv(tmp_path, rec, store, al, gen)
    fetch, fill = _fetch_factory()
    assert S.cmd_smoke(S.parse_args(argv), fetch=fetch, fill=fill) == 1
    out = json.loads((work / "r7-4-smoke.json").read_text())
    want = {"no-table": "no-table", "table-field": "field:table.counts", "request-t0": "t0-request",
            "prompt-day": "t0-prompt"}[case]
    assert want in out["fail"]


def test_smoke_cache_fresh_at_t0_only(tmp_path):
    rec, store, al = attached_recall(tmp_path)
    gen = smoke_gen(tmp_path, rec, gens_of(*([[item("hindsight-crew#1")]] * 3)))
    argv, work = _smoke_argv(tmp_path, rec, store, al, gen)
    log = []
    fetch, fill = _fetch_factory(log=log)
    assert S.cmd_smoke(S.parse_args(argv), fetch=fetch, fill=fill) == 0
    assert sorted(r for _, r in log) == ["hindsight-crew"]          # 소유자 표 값으로, 새로 받는다
    # 같은 t0 캐시는 재사용(호출 0), 다른 t0 로 받은 캐시나 t0 전에 받은 캐시는 거부
    log.clear()
    assert S.cmd_smoke(S.parse_args(argv + ["--offline"]), fetch=fetch, fill=fill) == 0 and log == []
    os.remove(work / "r7-4-github.json")
    early, fill2 = _fetch_factory(fetched="2026-11-06T00:00:00Z")
    assert S.cmd_smoke(S.parse_args(argv), fetch=early, fill=fill2) == 1
    c = json.loads((work / "r7-4-github.json").read_text())
    c["t0"] = "2026-11-05T00:00:00Z"
    (work / "r7-4-github.json").write_text(json.dumps(c))
    assert S.cmd_smoke(S.parse_args(argv), fetch=fetch, fill=fill) == 1
    assert "--github-cache" not in open(S.__file__, encoding="utf-8").read().split("def parse_args")[1].split(
        'add_parser("smoke")')[1].split("add_parser")[0]


def test_table_check_all_keys_against_github(attached):
    rec, _, _ = attached
    tab = json.loads(open(rec, encoding="utf-8").read())["runs"][0]["open_table"]
    got = S.table_check(tab, smoke_cache(), T0)
    assert got["rows"] == 4 and got["parsed_matches_counts"] is True
    assert got["candidate"] == {"rows": 1, "open_issue": 1, "precision": 1.0, "linked_rows": 1, "linked_open_issue": 1,
                                "linked_precision": 1.0}
    assert got["closed_errors"] == 1 and got["not_planned_errors"] == 0           # #7: 표는 종결, t0 GitHub 열림
    assert got["unknown_reasons"] == {"pr-open": 1}


def test_smoke_dllm_head_and_refusals(tmp_path):
    rec, _, _ = attached_recall(tmp_path)
    spent = tmp_path / "spent"
    spent.mkdir()
    out = tmp_path / "g" / "any-name.json"

    def gen_one(prompt, caller):
        assert "기준일 %s" % T0_DAY in prompt
        return {"valid": True, "attempts": 1, "error": None, "items": [item("hindsight-crew#1")],
                "calls": [{"cost_usd": 0.01}]}
    argv = ["smoke-dllm", "--recall", rec, "--t0", T0_TEXT, "--k", "3", "--gen-out", str(out),
            "--spent-dir", str(spent), "--max-usd", "1", "--soft-usd", "0.8"]
    assert S.cmd_smoke_dllm(S.parse_args(argv), caller=lambda p: {}, generate=gen_one) == 0
    g = json.loads(out.read_text())
    assert (g["t"], g["t0"], g["stage"], g["k"], len(g["generations"])) == (T0_DAY, T0_TEXT, "smoke", 3, 3)
    assert g["recall_sha256"] == S.V.file_sha256(rec) and S.check_prompt(g, json.loads(open(rec).read())) == []
    # 이미 있는 출력, 요청 t0 불일치, 표 없는 recall 은 지출 전에 거부
    assert S.cmd_smoke_dllm(S.parse_args(argv), caller=lambda p: {}, generate=gen_one) == 1
    bad = argv[:]
    bad[bad.index("--t0") + 1] = "2026-11-06T00:00:00Z"
    bad[bad.index("--gen-out") + 1] = str(tmp_path / "g" / "b.json")
    assert S.cmd_smoke_dllm(S.parse_args(bad), caller=lambda p: {}, generate=gen_one) == 1
    raw = write_json(tmp_path / "raw.json", smoke_recall())
    nt = argv[:]
    nt[nt.index("--recall") + 1] = raw
    nt[nt.index("--gen-out") + 1] = str(tmp_path / "g" / "c.json")
    assert S.cmd_smoke_dllm(S.parse_args(nt), caller=lambda p: {}, generate=gen_one) == 1
    assert not os.path.exists(tmp_path / "g" / "b.json") and not os.path.exists(tmp_path / "g" / "c.json")


def _legacy_score_files(tmp_path):
    """10-28 원래 증상 경로(score) 입력: 저장 recall·생성·정답표·저장소·별칭·확장 캐시."""
    rec = tmp_path / "rec-on"
    rec.mkdir()
    tab = tab3([["hindsight-crew#1", OT.ROW_CANDIDATE, "2026-10-20"], ["hindsight-crew#2", OT.ROW_CLOSED, "2026-10-05"]])
    rows = [dict(r, mentioned_at=at(20)) for r in SROWS]
    d = {"stage": "symptom", "arm": "on", "t": "2026-10-28", "query_timestamp": "2026-10-28T14:59:59Z",
         "runs": [{"q": 1, "results": rows, "open_table": tab}], "totals": {"errors": 0}}
    rp = write_json(rec / "t-2026-10-28.json", d)
    gdir = tmp_path / "gen"
    gdir.mkdir()
    b = R5.build_input(DL, DL.load_prompt(DL.BASELINE_PROMPT), d, "2026-10-28", 200000)
    items = [item("hindsight-crew#1"), item("hindsight-crew#2"), item(None, ev=["r2"]), item("gone#1")]
    g = {"stage": "symptom", "arm": "on", "t": "2026-10-28", "k": 2, "recall_sha256": S.V.file_sha256(rp),
         "prompt_text_sha256": R5.sha256_text(b["prompt"]), "table_sha256": b["table"]["sha256"],
         "max_input_chars": 200000, "generations": gens_of(items, items[:2])}
    write_json(gdir / "symptom-on-2026-10-28-gen.json", g)
    ak = write_json(tmp_path / "ak.json", {"closed": [{"id": "c1", "name": "n1", "keywords": ["hindsight-crew#2"]}],
                                           "controls": [{"id": "k1", "name": "n2", "keywords": ["hindsight-crew#1"]}],
                                           "partial": []})
    al = write_json(tmp_path / "al.json", ALIASES)
    base = write_json(tmp_path / "base-cache.json", {"repos": cache(), "calls_total": 3})
    return {"rec": str(rec), "gen": str(gdir), "ak": ak, "al": al, "base": base, "recall": rp, "g": g, "d": d}


def _legacy_argv(f, store, work, extra=()):
    return ["score", "--gen-dir", f["gen"], "--recalls", "on=" + f["rec"], "--answer-key", f["ak"], "--store", store,
            "--aliases", f["al"], "--github-cache", f["base"], "--work", str(work), "--offline", *extra]


# 10-28 경로 요약 고정 sha256(저장소 파일 sha256 칸은 sqlite 판에 따라 바이트가 달라 자리표시로 바꾼 뒤). 수정 전 코드로 잰 값.
LEGACY_SUMMARY_SHA = "b2fa8d14033c0f90344848d4ff0cd130188ae1451077d17dd2dd9aff7a1dc695"


def _legacy_normalized(path):
    doc_ = json.loads(open(path, encoding="utf-8").read())
    doc_["inputs"]["store_sha256"] = "STORE"
    return R5.sha256_text(json.dumps(doc_, sort_keys=True, ensure_ascii=False, indent=1))


def test_legacy_symptom_path_reproduces_bytes(tmp_path):
    f = _legacy_score_files(tmp_path)
    store = make_store(tmp_path / "store.sqlite")
    outs = []
    for i, extra in enumerate(((), ("--t0", "2026-10-28T14:59:59Z"))):
        work = tmp_path / ("w%d" % i)
        work.mkdir(mode=0o700)
        assert S.main(_legacy_argv(f, store, work, extra)) == 0
        outs.append((work / "r7-s0-score.json").read_bytes())
    assert outs[0] == outs[1]
    assert _legacy_normalized(str(tmp_path / "w0" / "r7-s0-score.json")) == LEGACY_SUMMARY_SHA


@pytest.mark.parametrize("case", ["request", "prompt-day", "prompt-text"])
def test_legacy_symptom_path_refuses_t0_mismatch(tmp_path, case):
    f = _legacy_score_files(tmp_path)
    store = make_store(tmp_path / "store.sqlite")
    gpath = os.path.join(f["gen"], "symptom-on-2026-10-28-gen.json")
    if case == "request":
        d = dict(f["d"], query_timestamp="2026-10-28T10:00:00Z")
        write_json(tmp_path / "rec-on" / "t-2026-10-28.json", d)
    elif case == "prompt-day":
        write_json(tmp_path / "gen" / "symptom-on-2026-10-28-gen.json", dict(f["g"], t="2026-10-27"))
    else:
        write_json(tmp_path / "gen" / "symptom-on-2026-10-28-gen.json", dict(f["g"], prompt_text_sha256="0" * 64))
    assert os.path.exists(gpath)
    work = tmp_path / "w"
    work.mkdir(mode=0o700)
    assert S.main(_legacy_argv(f, store, work)) == 1


def test_owners_check_cover_and_forks(tmp_path):
    store = smoke_store(tmp_path)
    ev = IR.load_github(store)
    ev["repos"]["gone"] = {"owner": "o", "status": "missing", "fetched_at": GH_AT, "error_code": "http-404"}
    roles = {"a": {"hindsight-crew#1": "open", "gone#2": "open", "other#3": "open"}}
    meta = {"model": "m", "prompt_sha256": "p", "aliases_sha256": OT.aliases_sha256(ALIASES), "updated_at": "u",
            "key_rules": "v2"}
    mems = {"a": {"document_id": "d", "mentioned_at": at(20), "state": "valid", "text_sha256": "s", "judged_at": "j"}}
    path = str(tmp_path / "own.sqlite")
    IR.publish(path, meta, mems, roles, mode=0o600, github=ev)
    calls = []

    def get(p):
        calls.append(p)
        if p == "repos/o/hindsight-crew":
            return 200, [{"fork": True}], 1
        if p.startswith("repos/o/hindsight-crew/pulls"):
            return 200, [{"number": 1, "user": {"login": "o"}}], 1
        if p == "repos/o/extra":
            return 200, [{"fork": False}], 1
        return 404, [], 1
    got = S.owners_check(path, {"hindsight-crew": "o", "extra": "o"}, "o", ALIASES, get=get)
    assert got["store_repos"] == 3 and got["covered"] == 1 and got["not_covered"] == 2
    assert got["not_covered_status"] == {"missing": 1, "absent": 1}
    assert got["owner_entries"] == 2 and got["extra_owner_entries"] == 1
    assert got["forks"] == 1 and got["forks_with_closed_pr"] == 1 and got["fork_closed_prs"] == [1]
    assert got["gh_calls"] == len(calls) == 3
