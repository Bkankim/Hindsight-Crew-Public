"""bench/r5_labels.py - 런 5 시간여행 라벨셋 규칙(issue #361). 가짜 입력만 쓴다(네트워크 0)."""
from __future__ import annotations

import ast
import json
import os
import re
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from bench import r5_labels as L

REPO = Path(__file__).resolve().parent.parent
UPPER = date(2026, 10, 26)          # 격자 09-01 + 3k: ... 10-13 10-16 10-19 10-22 10-25
KST = L.KST


def at(day: str, hh: int = 12) -> str:
    """KST 날짜의 hh 시를 UTC ISO 로."""
    d = datetime.combine(date.fromisoformat(day), datetime.min.time().replace(hour=hh), tzinfo=KST)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mem(mid, day, keys, completion=False, owner_refs=(), state="valid", hh=12):
    return {"id": mid, "state": state, "mentioned_at": at(day, hh), "keys": list(keys),
            "completion": completion, "owner_refs": [list(r) for r in owner_refs], "tags": []}


def issue(n, created, closed=None, reason=None, pr=False, state=None):
    return {"number": n, "created_at": at(created), "closed_at": at(closed) if closed else None,
            "state": state or ("closed" if closed else "open"), "state_reason": reason, "pull_request": pr}


def gh(repos):
    """닫힌 이슈는 재오픈 events 를 받은 것으로 둔다(reo 에 없으면 False). build 는 확인 안 된 닫힌 키 이슈를 거부한다."""
    return {"calls": 0, "repos": {r: {"owner": "Bkankim", "issues": iss,
                                      "reopened": dict({str(i["number"]): False for i in iss if i.get("closed_at")}, **reo)}
                                  for r, (iss, reo) in repos.items()}}


def test_build_refuses_closed_issue_without_reopen_check():
    ghdoc = gh({"app": ([issue(1, "2026-08-20", "2026-09-05", "completed")], {})})
    ghdoc["repos"]["app"]["reopened"] = {}
    with pytest.raises(L.Fail):
        L.build([mem("c", "2026-09-03", ["app#1"], completion=True), TAIL], ghdoc)


# 마지막 기억 = UPPER 라서 상한이 UPPER 로 잡힌다
TAIL = mem("tail", UPPER.isoformat(), [])


def labels_of(meta, ghdoc, key):
    res = L.build(meta + [TAIL], ghdoc)
    return {r["t"]: (r["label"], r["split"]) for r in res["rows"] if r["key"] == key}, res


# --- 분류 행렬 ---------------------------------------------------------------------------------

CASES = {
    # 이름: (기억, 이슈, 재오픈 표, 기대 {t: label} 일부, 키가 아예 없어야 하나, 제외 사유)
    "끝남_양성": ([mem("c", "2026-09-03", ["app#1"], completion=True)],
                 issue(1, "2026-08-20", "2026-09-05", "completed"), {},
                 {"2026-09-04": "control", "2026-09-07": "positive"}, None),
    "끝남_데이터층": ([mem("o", "2026-09-02", ["app#1"])],
                    issue(1, "2026-08-20", "2026-09-05", "completed"), {},
                    {"2026-09-04": "control", "2026-09-07": "data_layer"}, None),
    "완료기억이_t뒤면_데이터층": ([mem("c", "2026-09-08", ["app#1"], completion=True)],
                          issue(1, "2026-08-20", "2026-09-05", "completed"), {},
                          {"2026-09-07": "data_layer", "2026-09-10": "positive"}, None),
    "기억전_끝남은_라벨아님": ([mem("o", "2026-09-12", ["app#1"])],
                     issue(1, "2026-08-20", "2026-09-05", "completed"), {},
                     {"2026-09-04": "control_no_memory", "2026-09-07": "data_layer", "2026-09-10": "data_layer",
                      "2026-09-13": "data_layer"}, None),
    "열림_대조": ([mem("o", "2026-09-02", ["app#1"])], issue(1, "2026-08-20"), {},
                {"2026-09-01": "control_no_memory", "2026-09-04": "control", "2026-10-25": "control"}, None),
    "생성전_라벨없음": ([mem("o", "2026-09-02", ["app#1"])], issue(1, "2026-09-09"), {},
                   {"2026-09-07": None, "2026-09-10": "control"}, None),
    "닫힘후_t_경계": ([mem("c", "2026-09-01", ["app#1"], completion=True)],
                  # 09-07 23:00 KST 닫힘 -> 09-07 끝 시각 이전이라 09-07 은 끝남
                  {"number": 1, "created_at": at("2026-08-20"), "closed_at": at("2026-09-07", 23),
                   "state": "closed", "state_reason": "completed", "pull_request": False}, {},
                  {"2026-09-04": "control", "2026-09-07": "positive"}, None),
    "재오픈_제외": ([mem("c", "2026-09-03", ["app#1"], completion=True)],
                 issue(1, "2026-08-20", "2026-09-05", "completed"), {"1": True}, {}, "reopened"),
    "지금_재오픈상태_제외": ([mem("o", "2026-09-03", ["app#1"])],
                     issue(1, "2026-08-20", None, "reopened"), {}, {}, "reopened"),
    "not_planned_제외": ([mem("o", "2026-09-03", ["app#1"])],
                        issue(1, "2026-08-20", "2026-09-05", "not_planned"), {}, {}, "not_planned"),
    "그밖의_닫힘_제외": ([mem("o", "2026-09-03", ["app#1"])],
                   issue(1, "2026-08-20", "2026-09-05", "duplicate"), {}, {}, "closed_other"),
    "PR_제외": ([mem("o", "2026-09-03", ["app#1"])],
              issue(1, "2026-08-20", "2026-09-05", "completed", pr=True), {}, {}, "pull_request"),
    "동명_repo_충돌_제외": ([mem("o", "2026-09-03", ["app#1"], owner_refs=[("bkankim", "app"), ("other", "app")])],
                       issue(1, "2026-08-20"), {}, {}, "collision"),
    "해석된_소유자와_다른_owner_충돌": ([mem("o", "2026-09-03", ["app#1"], owner_refs=[("other", "app")])],
                              issue(1, "2026-08-20"), {}, {}, "collision"),
    "같은_소유자_표기는_충돌아님": ([mem("o", "2026-09-03", ["app#1"], owner_refs=[("bkankim", "app")])],
                          issue(1, "2026-08-20"), {}, {"2026-09-04": "control"}, None),
    "무효화된_기억은_안셈": ([mem("c", "2026-09-03", ["app#1"], completion=True, state="invalidated"),
                     mem("o", "2026-09-02", ["app#1"])],
                    issue(1, "2026-08-20", "2026-09-05", "completed"), {}, {"2026-09-07": "data_layer"}, None),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_label_matrix(name):
    meta, iss, reo, want, excluded = CASES[name]
    got, res = labels_of(meta, gh({"app": ([iss], reo)}), "app#1")
    if excluded:
        assert got == {}
        assert res["excluded"].get(("app", 1)) == excluded
        return
    for day, label in want.items():
        assert (got.get(day) or (None,))[0] == label, (day, got.get(day))


def test_data_layer_sub_classes():
    # 09-05 닫힘, 완료 기억 없음, 첫 언급 09-12: 09-07·09-10 은 데이터층(언급도 없음), 09-13 부터 데이터층(완료 기억 없음)
    meta = [mem("o", "2026-09-12", ["app#1"]), TAIL]
    res = L.build(meta, gh({"app": ([issue(1, "2026-08-20", "2026-09-05", "completed")], {})}))
    sub = {r["t"]: (r["label"], r.get("data_sub")) for r in res["rows"] if r["t"] in ("2026-09-07", "2026-09-10", "2026-09-13")}
    assert sub == {"2026-09-07": ("data_layer", "no_mention"), "2026-09-10": ("data_layer", "no_mention"),
                   "2026-09-13": ("data_layer", "no_completion")}
    c = L.counts(res["rows"], "dev")
    assert c["data_layer"]["pairs"] == c["data_layer_no_mention"]["pairs"] + c["data_layer_no_completion"]["pairs"]
    assert c["data_layer_no_mention"]["pairs"] == 2


def test_agreement_uses_dev_keys_only():
    rows = [{"key": "a#1", "t": "2026-10-01", "label": "positive", "split": "dev"},
            {"key": "a#2", "t": "2026-10-22", "label": "positive", "split": "sealed"},
            {"key": "a#2", "t": "2026-10-01", "label": "positive", "split": "unused"},
            {"key": "a#3", "t": "2026-10-01", "label": "control_no_memory", "split": "dev"}]
    assert L.labeled_keys(rows, "dev") == {"a#1"}
    assert L.labeled_keys(rows) == {"a#1", "a#2"}


def test_issue_exclusion_order():
    # PR 이 재오픈·not_planned 보다 먼저
    assert L.issue_exclusion(issue(1, "2026-08-01", "2026-09-01", "not_planned", pr=True), True) == "pull_request"
    assert L.issue_exclusion(issue(1, "2026-08-01", "2026-09-01", "not_planned"), True) == "reopened"
    assert L.issue_exclusion(issue(1, "2026-08-01", "2026-09-01", "completed"), False) is None
    assert L.issue_exclusion(issue(1, "2026-08-01"), False) is None


def test_key_without_issue_or_repo_is_counted_not_labeled():
    meta = [mem("a", "2026-09-03", ["app#7", "ghost#1"]), TAIL]
    res = L.build(meta, gh({"app": ([issue(1, "2026-08-20")], {})}))
    assert res["rows"] == []
    assert res["key_reason"] == {"no_issue": 1, "repo_not_in_answer_list": 1}


# --- t 격자·상한 --------------------------------------------------------------------------------

@pytest.mark.parametrize("upper,last", [
    (date(2026, 10, 26), date(2026, 10, 25)),
    (date(2026, 10, 25), date(2026, 10, 25)),      # 상한 날짜가 격자점이면 포함
    (date(2026, 10, 24), date(2026, 10, 22)),
])
def test_t_grid_upper(upper, last):
    grid = L.t_grid(upper)
    assert grid[0] == date(2026, 9, 1) and grid[-1] == last
    assert all((b - a).days == 3 for a, b in zip(grid, grid[1:]))


def test_t_upper_is_last_mentioned_kst_date():
    # UTC 10-26 16:00 = KST 10-27 01:00 -> 상한 10-27
    meta = [mem("o", "2026-09-02", ["app#1"]),
            {"id": "z", "state": "valid", "mentioned_at": "2026-10-26T16:00:00Z", "keys": [], "completion": False}]
    res = L.build(meta, gh({"app": ([issue(1, "2026-08-20")], {})}))
    assert res["upper"] == date(2026, 10, 27)
    assert max(r["t"] for r in res["rows"]) <= "2026-10-27"


def test_t_end_is_kst_end_of_day():
    assert L.t_end(date(2026, 10, 28)).astimezone(timezone.utc) == datetime(2026, 10, 28, 14, 59, 59, 999999, tzinfo=timezone.utc)
    assert L.has_memory([(L.parse_instant("2026-10-28T14:59:59.400Z"), False)], L.t_end(date(2026, 10, 28)), completion=False)


@pytest.mark.parametrize("earliest,start", [
    (date(2026, 9, 5), date(2026, 9, 1)),
    (date(2026, 8, 29), date(2026, 8, 29)),
    (date(2026, 8, 27), date(2026, 8, 29)),       # 같은 격자로 earliest 이후 첫 점까지
])
def test_extended_start(earliest, start):
    assert L.extended_start(earliest) == start


# --- 분할(봉인 = 봉인 구간에 쌍이 있는 키의 층화 30% 홀드아웃, #360 20:13 개정) ----------------------

def R(key, t, label):
    return {"key": key, "t": t, "label": label}


def window_rows(n_pos, n_ctl, prefix="app"):
    """봉인 구간(UPPER=10-26 이면 [10-19, 10-26]) 10-22 에 양성 n_pos 키, 대조 n_ctl 키. 모두 dev 기간 쌍도 있다."""
    rows = []
    for i in range(n_pos):
        rows += [R("%s#%d" % (prefix, i + 1), "2026-10-01", "control"), R("%s#%d" % (prefix, i + 1), "2026-10-22", "positive")]
    for j in range(n_ctl):
        k = "%s#%d" % (prefix, 100 + j)
        rows += [R(k, "2026-10-01", "control"), R(k, "2026-10-16", "control"), R(k, "2026-10-22", "control")]
    return rows


def test_split_boundaries():
    parts = L.split(window_rows(0, 0) + [R("a#1", "2026-10-13", "control"), R("a#1", "2026-10-16", "control")], UPPER)
    assert parts["seal_start"] == UPPER - timedelta(days=7) == date(2026, 10, 19)
    assert parts["dev_last"] == date(2026, 10, 14)
    by = {(r["key"], r["t"]): r["split"] for r in parts["rows"]}
    assert by[("a#1", "2026-10-13")] == "dev"
    assert by[("a#1", "2026-10-16")] == "unused"             # 간격은 dev 에 넣지 않는다
    assert parts["sealed_keys"] == set()                     # 봉인 구간에 쌍이 없는 키는 봉인 후보가 아니다


@pytest.mark.parametrize("n_pos,n_ctl,want_pos,want_ctl", [
    (10, 10, 3, 3), (1, 1, 1, 1), (2, 5, 1, 2), (20, 0, 6, 0), (0, 3, 0, 1)])
def test_holdout_strata_sizes(n_pos, n_ctl, want_pos, want_ctl):
    parts = L.split(window_rows(n_pos, n_ctl), UPPER)
    pos = {k for k in parts["sealed_keys"] if int(k.split("#")[1]) < 100}
    assert (len(pos), len(parts["sealed_keys"]) - len(pos)) == (want_pos, want_ctl)
    assert parts["strata"] == {"positive": n_pos, "control": n_ctl}


def test_holdout_deterministic_by_seed():
    rows = window_rows(10, 10)
    a, b = L.split(rows, UPPER), L.split(list(reversed(rows)), UPPER)
    assert a["sealed_keys"] == b["sealed_keys"]              # 같은 씨앗 = 같은 봉인 키(입력 순서 무관)
    seeds = {frozenset(L.split(rows, UPPER, seed=s)["sealed_keys"]) for s in range(20)}
    assert len(seeds) > 1                                    # 씨앗이 실제로 고른다


def test_key_in_both_strata_goes_positive():
    rows = [R("a#1", "2026-10-19", "control"), R("a#1", "2026-10-25", "positive")]
    parts = L.split(rows, UPPER)
    assert parts["strata"] == {"positive": 1, "control": 0}
    assert parts["sealed_keys"] == {"a#1"}


def test_sealed_keys_out_of_dev_and_pairs_only_in_window():
    parts = L.split(window_rows(10, 10), UPPER)
    rows = parts["rows"]
    assert parts["sealed_keys"] and not (parts["dev_keys"] & parts["sealed_keys"])
    for r in rows:
        if r["key"] in parts["sealed_keys"]:
            assert r["split"] != "dev"
        if r["split"] == "sealed":
            assert r["key"] in parts["sealed_keys"] and "2026-10-19" <= r["t"] <= UPPER.isoformat()
    # 봉인되지 않은 봉인 구간 키는 dev 기간 쌍이 dev 이고 봉인 구간 쌍은 unused
    rest = [r for r in rows if r["key"] not in parts["sealed_keys"]]
    assert {r["split"] for r in rest if r["t"] == "2026-10-01"} == {"dev"}
    assert {r["split"] for r in rest if r["t"] == "2026-10-22"} == {"unused"}


def test_data_layer_follows_key_split():
    rows = [R("a#1", "2026-10-01", "data_layer"), R("a#1", "2026-10-22", "positive"),
            R("b#1", "2026-10-01", "data_layer"), R("b#1", "2026-10-22", "data_layer")]
    parts = L.split(rows, UPPER)
    by = {(r["key"], r["t"]): r["split"] for r in parts["rows"]}
    assert parts["sealed_keys"] == {"a#1"}                   # 양성 층 1키 -> 최소 1키
    assert by[("a#1", "2026-10-01")] == "unused" and by[("a#1", "2026-10-22")] == "sealed"
    # 봉인 구간에 데이터층만 있는 키는 층 밖이라 봉인 후보가 아니다(dev 키)
    assert by[("b#1", "2026-10-01")] == "dev" and by[("b#1", "2026-10-22")] == "unused"


def test_build_split_end_to_end():
    meta = [mem("a", "2026-09-02", ["app#1"]), mem("b", "2026-10-21", ["app#2"]), TAIL]
    ghdoc = gh({"app": ([issue(1, "2026-08-20"), issue(2, "2026-08-20", "2026-10-23", "completed")], {})})
    res = L.build(meta, ghdoc)
    parts = res["parts"]
    assert parts["strata"] == {"positive": 0, "control": 2}
    assert len(parts["sealed_keys"]) == 1 and not (parts["dev_keys"] & parts["sealed_keys"])


def test_fold_is_repo():
    meta = [mem("a", "2026-09-02", ["app#1", "lib#4"]), TAIL]
    ghdoc = gh({"app": ([issue(1, "2026-08-20")], {}), "lib": ([issue(4, "2026-08-20")], {})})
    rows = L.build(meta, ghdoc)["rows"]
    assert sum(L.counts(rows, p)["folds"] for p in ("dev", "sealed")) == 2   # 한 키는 봉인, 한 키는 dev
    assert {r["fold"] for r in rows} == {"app", "lib"}


# --- "기억 있음"·완료 계열 -----------------------------------------------------------------------

@pytest.mark.parametrize("tags,want", [
    (["kind:completion"], True), (["KIND:Done"], True), (["kind:task-done"], True),
    (["kind:task-completion"], True), (["kind:work-log"], False), (["kind:handoff"], False), ([], False),
    ("kind:completion", False),
])
def test_completion_family(tags, want):
    assert L.is_done_family(tags) is want


def test_has_memory_cutoff_inclusive():
    cut = L.t_end(date(2026, 9, 4))
    assert L.has_memory([(cut, True)], cut, completion=True)
    assert not L.has_memory([(cut + timedelta(seconds=1), True)], cut, completion=True)
    assert not L.has_memory([(cut, False)], cut, completion=True)
    assert L.has_memory([(cut, False)], cut, completion=False)


def test_memory_meta_has_no_body_and_extracts_keys():
    aliases = {"app": "app", "a": "app"}
    row = L.memory_meta({"id": "m", "text": "[완료] a#12 끝, Other/App#3 참고 https://x.io/o/app#9",
                         "tags": ["kind:completion", "project:app"], "state": "valid", "fact_type": "world",
                         "mentioned_at": "2026-09-01T00:00:00Z"}, aliases)
    assert row["keys"] == ["app#3", "app#12"]
    assert row["completion"] is True
    assert row["owner_refs"] == [["other", "app"]]
    assert "text" not in row and "content" not in row


# --- 세트 C·D 일치율 ---------------------------------------------------------------------------

def _tk(item):
    return set(item.get("keys") or [])


@pytest.mark.parametrize("states,want_rate,reason", [
    ({"a#1": "done", "a#2": "open"}, 1.0, None),
    ({"a#1": "open", "a#2": "open"}, 0.5, "done_vs_open"),
    ({"a#1": "done", "a#2": "excluded:not_planned"}, 1.0, None),     # 제외 키는 겹침 밖
])
def test_agreement(states, want_rate, reason):
    doc = {"closed": [{"id": "W1", "keys": ["a#1"]}], "controls": [{"id": "W2", "keys": ["a#2"]}],
           "partial": [{"id": "W3", "keys": ["a#3"]}]}
    labeled = {k for k, s in states.items() if s in ("done", "open")}
    got = L.agreement(doc, states.get, labeled, _tk)
    assert got["rate"] == want_rate
    if reason:
        assert got["reasons"] == {reason: 1}
    else:
        assert got["reasons"] == {}


def test_agreement_multi_key_mixed_and_no_key():
    doc = {"closed": [{"id": "W1", "keys": ["a#1", "a#2"]}, {"id": "W2", "keys": []}]}
    got = L.agreement(doc, {"a#1": "done", "a#2": "open"}.get, {"a#1", "a#2"}, _tk)
    assert got["reasons"] == {"multi_key_mixed": 1} and got["outside"] == {"no_key": 1}


# --- GitHub(가짜 응답) -------------------------------------------------------------------------

def test_cmd_github_resolves_owner_paginates_and_counts(tmp_path):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"app": "app", "lib": "lib", "gone": "gone"}))
    meta = tmp_path / "meta.jsonl"
    meta.write_text("".join(json.dumps(r) + "\n" for r in [
        mem("a", "2026-09-02", ["app#1", "lib#2"], owner_refs=[("someone", "lib")])]))
    page1 = [dict(issue(i, "2026-08-01"), extra="x") for i in range(1, 101)]
    page1[0] = issue(1, "2026-08-01", "2026-09-05", "completed")
    calls = []

    def fake(path):
        calls.append(path)
        if path == "repos/Bkankim/app":
            return 200, {}
        if path in ("repos/Bkankim/lib", "repos/Bkankim/gone"):
            return 404, None
        if path == "repos/someone/lib":
            return 200, {}
        if path.startswith("repos/Bkankim/app/issues?"):
            return 200, page1 if path.endswith("&page=1") else []
        if path.startswith("repos/someone/lib/issues?"):
            return 200, [issue(2, "2026-08-01")]
        if path.startswith("repos/Bkankim/app/issues/1/events"):
            return 200, [{"event": "closed"}]
        raise AssertionError(path)

    out = tmp_path / "gh.json"
    args = type("A", (), {"aliases": str(aliases), "policy": None, "meta": str(meta), "out": str(out)})
    assert L.cmd_github(args, get=fake) == 0
    doc = json.loads(out.read_text())
    assert doc["repos"]["app"]["owner"] == "Bkankim" and doc["repos"]["lib"]["owner"] == "someone"
    assert doc["repos"]["gone"].get("owner") is None
    assert doc["repos"]["app"]["reopened"] == {"1": False}
    assert doc["calls"] == len(calls) == 8
    assert "extra" not in json.dumps(doc) and "title" not in json.dumps(doc)
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    calls.clear()
    assert L.cmd_github(args, get=fake) == 0           # 캐시: 다시 부르면 새 호출 없음
    assert calls == []


def _gh_args(tmp_path, aliases, meta_rows):
    a = tmp_path / "aliases.json"
    a.write_text(json.dumps(aliases))
    m = tmp_path / "meta.jsonl"
    m.write_text("".join(json.dumps(r) + "\n" for r in meta_rows))
    return type("A", (), {"aliases": str(a), "policy": None, "meta": str(m), "out": str(tmp_path / "gh.json")})


def test_cmd_github_keeps_cache_and_calls_on_failure(tmp_path):
    args = _gh_args(tmp_path, {"app": "app", "lib": "lib"}, [mem("a", "2026-09-02", ["app#1"])])

    def fake(path):
        if path in ("repos/Bkankim/app", "repos/Bkankim/lib"):
            return 200, {}
        if path.startswith("repos/Bkankim/app/issues?"):
            return 200, [issue(1, "2026-08-01")]
        raise L.Fail("HTTP 403 rate limit")            # lib 이슈 목록에서 실패

    with pytest.raises(L.Fail):
        L.cmd_github(args, get=fake)
    doc = json.loads((tmp_path / "gh.json").read_text())
    assert doc["repos"]["app"]["issues"][0]["number"] == 1     # 실패 전 받은 것은 캐시에 남는다
    assert doc["calls"] == 3                                  # app 확인 + app 이슈 1쪽 + lib 확인


def test_cmd_github_tries_new_memory_owner_on_rerun(tmp_path):
    args = _gh_args(tmp_path, {"lib": "lib"}, [mem("a", "2026-09-02", ["lib#2"], owner_refs=[("x", "lib")])])
    calls = []

    def fake(path):
        calls.append(path)
        if path in ("repos/Bkankim/lib", "repos/x/lib"):
            return 404, None
        if path == "repos/y/lib":
            return 200, {}
        if path.startswith("repos/y/lib/issues?"):
            return 200, [issue(2, "2026-08-01")]
        raise AssertionError(path)

    assert L.cmd_github(args, get=fake) == 0
    assert json.loads((tmp_path / "gh.json").read_text())["repos"]["lib"].get("owner") is None
    # 새 meta 에 owner y 가 생기면 다시 부를 때 y 만 더 본다(x 는 다시 보지 않는다)
    (tmp_path / "meta.jsonl").write_text(json.dumps(mem("a", "2026-09-02", ["lib#2"],
                                                        owner_refs=[("x", "lib"), ("y", "lib")])) + "\n")
    calls.clear()
    assert L.cmd_github(args, get=fake) == 0
    assert calls[0] == "repos/y/lib" and "repos/x/lib" not in calls
    assert json.loads((tmp_path / "gh.json").read_text())["repos"]["lib"]["owner"] == "y"


def test_policy_repos_reads_only_aliases(tmp_path):
    pol = tmp_path / "p.json"
    pol.write_text(json.dumps({"banks": {"x": {"item_repo_aliases": {"hc": "hindsight-crew"}, "tag_aliases": {}},
                                         "y": {"tag_aliases": {}}}}))
    assert L.policy_repos(str(pol)) == ({"hc": "hindsight-crew"}, 1)
    table, conflicts = L.merged_aliases({"hc": "a"}, {"hc": "b", "x": "x"})
    assert table == {"hc": "a", "x": "x"} and conflicts == 1


# --- 모듈 경계 -------------------------------------------------------------------------------

def test_imports_only_extract_keys_from_gateway():
    tree = ast.parse((REPO / "bench" / "r5_labels.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("gateway"):
            assert node.module == "gateway.item_keys" and [a.name for a in node.names] == ["extract_keys"]
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("gateway") for a in node.names)
    # 완료 기준 grep 과 같은 검사: 줄마다 금지 낱말이 있으면 그 줄에 extract_keys 가 있어야 한다
    bad = re.compile(r"item_ledger|item_status|is_completion|import gateway|from gateway")
    lines = (REPO / "bench" / "r5_labels.py").read_text(encoding="utf-8").splitlines()
    assert [ln for ln in lines if bad.search(ln) and "extract_keys" not in ln] == []
