"""issue #351 R4-1 - recall 저장, 판독 표·세트 D 고정, 주 통계량과 영분포(`bench/d_llm_stats.py`).

#349 "주 통계량과 영분포"·"T1-lite 판정"·"recall 인자"가 정본이다. 게이트웨이·LLM 호출은 가짜 주입으로만 한다(네트워크 0).
작업 표·목록 출력은 전부 합성이다(실제 항목 이름 없음).
"""
import json
import os
import random

import pytest

from bench import d_llm as D
from bench import d_llm_stats as S


def item(name, status="open", key=None, unverified=None):
    it = {"name": name, "status": status, "evidence_ids": [], "quote": ""}
    if key is not None:
        it["key"] = key
    if unverified is not None:
        it["unverified"] = unverified
    return it


def gen(runs):
    """runs: 반복마다 목록 항목 리스트, None 이면 invalid 반복."""
    return {"generations": [{"valid": False, "items": []} if r is None else {"valid": True, "items": r}
                            for r in runs]}


def setd(closed, controls, partial=()):
    def t(i):
        return {"id": i, "keywords": [f"{i.lower()}-kw"], "project_hint": None}
    return {"closed": [t(i) for i in closed], "controls": [t(i) for i in controls],
            "partial": [t(i) for i in partial], "unknown": []}


def openrun(*ids, unverified=()):
    return [item(f"{i.lower()}-kw", unverified=(i in unverified)) for i in ids]


# ---------------------------------------------------------------- 사전 등록 기본값(인자로 바꿀 수 있다)

def test_defaults_match_r4_0_proposal():
    assert (D.R41_K, D.R41_MAX_INPUT_CHARS, D.R41_MAX_TOKENS) == (7, 200000, 16384)
    assert D.R41_QUERY_TIMESTAMP == "2026-10-28T14:59:59Z"
    assert S.PERMS == 1000 and S.MIN_CLOSED == 20 and S.MIN_CONTROLS == 6 and S.NET_MIN == 3.0
    assert isinstance(S.SEED, int)


# ---------------------------------------------------------------- 열림 플래그와 열림율

@pytest.mark.parametrize("strict, want", [
    # 기본: unverified 는 표시일 뿐 open 으로 센다(#349)
    (False, {"A": [1, 1, 0, 0], "B": [1, 0, 0, 0]}),
    # T1-lite 보조: unverified=true 만으로 열린 반복은 not-open
    (True, {"A": [1, 0, 0, 0], "B": [1, 0, 0, 0]}),
])
def test_open_flags_unverified_modes_and_invalid_in_denominator(strict, want):
    g = gen([openrun("A", "B"), openrun("A", unverified=("A",)), None, [item("a-kw", status="done")]])
    tasks = S.tasks_of(setd(["A"], ["B"]))
    flags = S.open_flags(g, tasks, strict_unverified=strict)
    assert flags == want
    assert S.open_rates(flags) == {k: sum(v) / 4 for k, v in want.items()}   # invalid 반복도 분모 K 에 든다


def test_open_flag_strict_keeps_open_when_any_open_item_is_verified():
    g = gen([[item("a-kw", unverified=True), item("a-kw 후속", unverified=False)]])
    assert S.open_flags(g, S.tasks_of(setd(["A"], [])), strict_unverified=True)["A"] == [1]


# ---------------------------------------------------------------- 순이득(적격 이득·새 오답)

def test_net_gain_qualified_only_and_new_wrong_includes_offtable_increase():
    closed = ["A", "B", "C"]
    rb = {"A": 1.0, "B": 0.6, "C": 0.0}
    rt = {"A": 0.2, "B": 0.0, "C": 0.4}
    off = [{"id": "x1", "label": "done", "rate_b": 0.0, "rate_t": 0.5},
           {"id": "x2", "label": "unlabelable", "rate_b": 0.2, "rate_t": 0.3},   # 라벨 불가는 끝난 일로 본다
           {"id": "x3", "label": "open", "rate_b": 0.0, "rate_t": 1.0},          # 열린 일은 새 오답이 아니다
           {"id": "x4", "label": "done", "rate_b": 0.6, "rate_t": 0.1}]          # 감소분은 세지 않는다(증가분만)
    r = S.net_gain(rb, rt, closed, qualified={"A": "new_done_row", "B": "vanished"}, offtable=off)
    assert r["gain"] == pytest.approx(0.8)            # B 는 그냥 사라짐 -> 0
    assert r["new_wrong"] == pytest.approx(0.4 + 0.5 + 0.1)
    assert r["net"] == pytest.approx(0.8 - 1.0)


def test_net_gain_conservative_counts_all_decreases():
    r = S.net_gain({"A": 1.0, "B": 0.5}, {"A": 0.0, "B": 1.0}, ["A", "B"], qualified=None)
    assert (r["gain"], r["new_wrong"], r["net"]) == (pytest.approx(1.0), pytest.approx(0.5), pytest.approx(0.5))


def test_qualified_reason_must_be_known():
    with pytest.raises(ValueError):
        S.net_gain({"A": 1.0}, {"A": 0.0}, ["A"], qualified={"A": "looks-good"})


# ---------------------------------------------------------------- 대조군 가드

def test_control_drop_denominator_is_baseline_positive_controls():
    r = S.control_drop({"C1": 1.0, "C2": 0.0, "C3": 0.5}, {"C1": 0.5, "C2": 1.0, "C3": 0.5}, ["C1", "C2", "C3"])
    assert (r["denom"], r["zero"]) == (2, 1)
    assert r["drop"] == pytest.approx(0.5)            # C2(기준선 0)는 분모·합에서 빠진다


# ---------------------------------------------------------------- 순열 영분포

def test_percentile95_nearest_rank():
    assert S.percentile95(list(range(1, 101))) == 95
    assert S.percentile95([3.0]) == 3.0


@pytest.mark.parametrize("k, half, dropped", [(7, 3, 1), (6, 3, 0), (5, 2, 1)])
def test_perm_split_odd_k_drops_one(k, half, dropped):
    a, b = S.perm_split(list(range(k)), random.Random(1))
    assert len(a) == len(b) == half
    assert k - len(set(a) | set(b)) == dropped


def test_perm_null_is_seeded_and_reproducible():
    flags = {"A": [1, 0, 1, 1, 0, 1, 0], "B": [0, 0, 1, 0, 1, 1, 1], "C1": [1, 1, 0, 1, 1, 1, 1]}
    one = S.perm_null(flags, ["A", "B"], ["C1"], perms=200, seed=7)
    two = S.perm_null(flags, ["A", "B"], ["C1"], perms=200, seed=7)
    assert one == two
    assert one["perms"] == 200 and one["half"] == 3 and len(one["net_values"]) == 200
    assert one["p95_net"] == S.percentile95(one["net_values"])


def test_perm_null_identical_runs_is_zero():
    flags = {"A": [1] * 7, "C1": [1] * 7}
    r = S.perm_null(flags, ["A"], ["C1"], perms=50, seed=1)
    assert r["p95_net"] == 0 and r["p95_ctrl"] == 0


# ---------------------------------------------------------------- 복원 A/A 와 X2 반복 차

def test_aa_takes_max_of_both_directions():
    r1 = {"A": 1.0, "B": 0.0, "C1": 1.0}
    r2 = {"A": 0.0, "B": 0.4, "C1": 0.5}
    r = S.aa(r1, r2, ["A", "B"], ["C1"])
    assert r["net"] == pytest.approx(0.6)            # 1->2: 1.0-0.4, 2->1: 0.4-1.0
    assert r["ctrl"] == pytest.approx(0.5)


# ---------------------------------------------------------------- 평가(효과·판정 불가)

def _flags_gen(rows):
    """rows: {task: [0/1 ...]} -> gen."""
    k = len(next(iter(rows.values())))
    return gen([[item(f"{t.lower()}-kw") for t, v in rows.items() if v[r]] for r in range(k)])


def big_set(n_closed=20, n_ctrl=6):
    return setd([f"K{i}" for i in range(n_closed)], [f"C{i}" for i in range(n_ctrl)])


def test_evaluate_effect_when_both_pairs_beat_bound_and_guard_passes():
    sd = big_set()
    closed = [t["id"] for t in sd["closed"]]
    ctrls = [t["id"] for t in sd["controls"]]
    base = {**{c: [1] * 7 for c in closed[:5]}, **{c: [1] * 7 for c in ctrls}}
    treat = {**{c: [0] * 7 for c in closed[:5]}, **{c: [1] * 7 for c in ctrls}}
    q = [{c: "new_done_row" for c in closed[:5]}] * 2
    rep = S.evaluate(sd, [_flags_gen(base)] * 2, [_flags_gen(treat)] * 2, qualified=q, offtable=[[], []],
                     perms=100, seed=1)
    assert rep["sample"]["ok"] is True
    for p in rep["pairs"]:
        assert p["net"] == pytest.approx(5.0) and p["bound"] == 0 and p["guard"] == "pass"
    assert rep["effect"] == "effect"


def test_evaluate_undecidable_when_controls_below_six():
    sd = big_set(n_ctrl=5)
    closed = [t["id"] for t in sd["closed"]]
    base = {**{c: [1] * 7 for c in closed[:5]}, **{t["id"]: [1] * 7 for t in sd["controls"]}}
    treat = {**{c: [0] * 7 for c in closed[:5]}, **{t["id"]: [1] * 7 for t in sd["controls"]}}
    q = [{c: "new_done_row" for c in closed[:5]}] * 2
    rep = S.evaluate(sd, [_flags_gen(base)] * 2, [_flags_gen(treat)] * 2, qualified=q, perms=50, seed=1)
    assert rep["sample"]["ok"] is False
    assert rep["effect"] == "undecidable"
    assert all(p["guard"] == "undecidable" for p in rep["pairs"])


def test_evaluate_no_effect_below_net_min_and_pending_without_reading():
    sd = big_set()
    closed = [t["id"] for t in sd["closed"]]
    ctrls = [t["id"] for t in sd["controls"]]
    base = {**{c: [1] * 7 for c in closed[:2]}, **{c: [1] * 7 for c in ctrls}}
    treat = {**{c: [0] * 7 for c in closed[:2]}, **{c: [1] * 7 for c in ctrls}}
    q = [{c: "new_done_row" for c in closed[:2]}] * 2
    rep = S.evaluate(sd, [_flags_gen(base)] * 2, [_flags_gen(treat)] * 2, qualified=q, perms=50, seed=1)
    assert rep["effect"] == "no-effect"                # 순이득 2.0 < 3.0
    rep = S.evaluate(sd, [_flags_gen(base)] * 2, [_flags_gen(treat)] * 2, qualified=None, perms=50, seed=1)
    assert rep["effect"] == "pending-reading"          # 이득 판독 없이는 효과를 내지 않는다


def test_evaluate_guard_fails_when_controls_drop_beyond_bound():
    sd = big_set()
    closed = [t["id"] for t in sd["closed"]]
    ctrls = [t["id"] for t in sd["controls"]]
    base = {**{c: [1] * 7 for c in closed[:5]}, **{c: [1] * 7 for c in ctrls}}
    treat = {**{c: [0] * 7 for c in closed[:5]}, **{c: [0] * 7 for c in ctrls}}
    q = [{c: "stale_open_dropped" for c in closed[:5]}] * 2
    rep = S.evaluate(sd, [_flags_gen(base)] * 2, [_flags_gen(treat)] * 2, qualified=q, perms=50, seed=1)
    assert all(p["guard"] == "fail" for p in rep["pairs"])
    assert rep["effect"] == "no-effect"


def test_evaluate_x2_adds_repeat_difference_to_bound():
    sd = big_set()
    closed = [t["id"] for t in sd["closed"]]
    ctrls = [t["id"] for t in sd["controls"]]
    base = {**{c: [1] * 7 for c in closed[:6]}, **{c: [1] * 7 for c in ctrls}}
    t1 = {**{c: [0] * 7 for c in closed[:6]}, **{c: [1] * 7 for c in ctrls}}
    t2 = {**{c: [0] * 7 for c in closed[:4]}, **{c: [1] * 7 for c in closed[4:6]}, **{c: [1] * 7 for c in ctrls}}
    q = [{c: "new_done_row" for c in closed[:6]}] * 2
    rep = S.evaluate(sd, [_flags_gen(base)] * 2, [_flags_gen(t1), _flags_gen(t2)], qualified=q, x2=True,
                     perms=50, seed=1)
    assert rep["x2_diff"]["net"] == pytest.approx(2.0)
    assert [p["bound"] for p in rep["pairs"]] == [pytest.approx(2.0)] * 2


def test_evaluate_baseline_only_reports_rates_null_and_aa():
    sd = big_set()
    b1 = _flags_gen({"K0": [1, 1, 0, 1, 0, 1, 1], "C0": [1] * 7})
    b2 = _flags_gen({"K0": [0, 1, 0, 1, 0, 0, 1], "C0": [1] * 7})
    rep = S.evaluate(sd, [b1, b2], [], perms=50, seed=3)
    assert rep["base"][0]["open_rate"]["K0"] == pytest.approx(5 / 7)
    assert rep["base"][1]["open_rate"]["K0"] == pytest.approx(3 / 7)
    assert rep["aa"]["net"] == pytest.approx(2 / 7)
    assert len(rep["base"][0]["null"]["net_values"]) == 50
    assert rep["stable_positive"] == []               # 모든 반복 open 인 끝난 항목(보조)
    assert "effect" not in rep


def test_t1_lite_aux_reports_strict_net_and_unverified_ratio_difference():
    sd = setd(["A", "B"], ["C"])
    base = gen([openrun("A", "B", "C")] * 4)
    treat = gen([openrun("A", "B", "C", unverified=("A", "B"))] * 4)
    rep = S.evaluate(sd, [base, base], [treat, treat], qualified=[{}, {}], perms=20, seed=1)
    p = rep["pairs"][0]
    assert p["net"] == 0                                # 주 통계량: unverified 도 open -> 이득 0("표시만 함")
    aux = p["t1_lite"]
    assert aux["strict_treat_net"] == pytest.approx(2.0)
    assert aux["unverified_ratio"]["closed"] == 1.0 and aux["unverified_ratio"]["controls"] == 0.0
    assert aux["unverified_ratio"]["diff"] == 1.0


# ---------------------------------------------------------------- stats 명령

def test_stats_cmd_writes_private_report(tmp_path, capsys):
    sd = big_set()
    sp = tmp_path / "setd.json"
    sp.write_text(json.dumps(sd))
    gp = []
    for n in range(2):
        p = tmp_path / f"b{n}.json"
        p.write_text(json.dumps(_flags_gen({"K0": [1, 0, 1, 1, 0, 1, 1], "C0": [1] * 7})))
        gp.append(str(p))
    out = tmp_path / "w" / "stats.json"
    rc = D.main(["stats", "--set", str(sp), "--base", gp[0], "--base", gp[1], "--perms", "30", "--out", str(out)])
    assert rc == 0
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    doc = json.loads(out.read_text())
    assert doc["seed"] == S.SEED and doc["perms"] == 30
    assert doc["inputs"]["set_sha256"] and len(doc["inputs"]["base_sha256"]) == 2
    printed = capsys.readouterr().out
    assert "-kw" not in printed                        # 표준 출력에는 id·수치만


# ---------------------------------------------------------------- recall 저장

class FakeGw:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def call(self, tool, args):
        self.calls.append((tool, dict(args)))
        if len(self.calls) in self.fail_on:
            raise RuntimeError("boom")
        n = len(self.calls)
        return {"results": [{"id": f"m{n}-{i}", "text": "x" * (i + 1), "fact_type": "world"} for i in range(n)]}


def test_save_recalls_args_and_per_query_cost():
    gw = FakeGw(fail_on=(2,))
    qs = [{"qid": "F1", "text": "문안 하나"}, {"qid": "F2", "text": "문안 둘"}, {"qid": "F3", "text": "셋"}]
    out = D.save_recalls(gw.call, "지시문 한 문단", qs, max_tokens=16384, query_timestamp=D.R41_QUERY_TIMESTAMP,
                         counter=len)
    assert [c[0] for c in gw.calls] == ["recall"] * 3
    a = gw.calls[0][1]
    assert a == {"query": "지시문 한 문단\n문안 하나", "query_timestamp": "2026-10-28T14:59:59Z", "max_tokens": 16384}
    runs = out["runs"]
    assert [r["q"] for r in runs] == [1, 2, 3] and [r["qid"] for r in runs] == ["F1", "F2", "F3"]
    assert runs[1]["error"] and runs[1]["results"] == []
    assert runs[2]["rows"] == 3 and runs[2]["response_tokens"] == 1 + 2 + 3
    assert all(isinstance(r["seconds"], float) for r in runs)
    assert out["totals"] == {"queries": 3, "errors": 1, "rows": 4, "response_tokens": 1 + 1 + 2 + 3,
                             "seconds": pytest.approx(sum(r["seconds"] for r in runs))}
    # 저장 결과는 기존 run 명령 입력 조립과 맞는다(질의 순 -> 순위 순)
    asm = D.assemble(out)
    assert [r["id"] for r in asm["rows"]][:2] == ["m1-0", "m3-0"]


def test_name_queries_from_set_skip_unknown():
    sd = {"closed": [{"id": "D01", "name": "이름 하나", "keywords": ["k"]}],
          "controls": [{"id": "D02", "name": "이름 둘", "keywords": ["k2"]}],
          "partial": [{"id": "D03", "name": "이름 셋", "keywords": ["k3"]}],
          "unknown": [{"id": "D04", "name": "이름 넷"}]}
    qs = D.name_queries(sd)
    assert [(q["qid"], q["text"]) for q in qs] == [("D01", "이름 하나"), ("D02", "이름 둘"), ("D03", "이름 셋")]


def test_recall_cmd_refuses_production_target(tmp_path, monkeypatch):
    for k in ("HC_TARGET", "HC_GATEWAY_BIND", "HC_HINDSIGHT_CONTAINER", "HC_COMPOSE_PROJECT"):
        monkeypatch.delenv(k, raising=False)
    ins = tmp_path / "i.txt"
    ins.write_text("지시문")
    qf = tmp_path / "q.json"
    qf.write_text(json.dumps({"queries": [{"qid": "F1", "text": "t"}]}))
    rc = D.main(["recall", "--instruction", str(ins), "--queries", str(qf), "--out", str(tmp_path / "o.json")])
    assert rc == 2
    assert not (tmp_path / "o.json").exists()


# ---------------------------------------------------------------- 판독 표와 세트 D 고정

def _gen_for_table():
    return gen([[item("작업 알파 정리", key="repo#12"), item("베타 남은 일")],
                [item("작업 알파", key="repo#12", status="done"), item("감마")]])


def test_work_table_groups_by_reader_work(tmp_path):
    g = _gen_for_table()
    blind, mp = D.blind_file(g, seed=1)
    rid = {(m["gen"], m["item"]): m["rid"] for m in mp}
    reader = {"items": [{"rid": rid[(0, 0)], "work": "repo#12", "claims_open": True},
                        {"rid": rid[(1, 0)], "work": "Repo #12", "claims_open": False},
                        {"rid": rid[(0, 1)], "work": "label:베타", "claims_open": True},
                        {"rid": rid[(1, 1)], "work": "multi", "claims_open": True}]}
    t = D.work_table(g, {"map": mp}, reader)
    works = {w["work"]: w for w in t["works"]}
    assert set(works) == {"repo#12", "label:베타"}
    w = works["repo#12"]
    assert w["n_items"] == 2 and w["runs"] == 2 and sorted(w["names"]) == ["작업 알파", "작업 알파 정리"]
    assert w["keys"] == ["repo#12"] and w["statuses"] == {"open": 1, "done": 1}
    assert w["truth"] is None and w["keywords"] == [] and w["wid"].startswith("W")
    assert t["skipped"] == {"multi": 1, "unclear": 0}


def _filled():
    return {"works": [
        {"wid": "W01", "work": "repo#12", "names": ["작업 알파 정리"], "keys": ["repo#12"], "truth": "done",
         "keywords": ["repo#12", "알파 정리"], "project_hint": "repo"},
        {"wid": "W02", "work": "label:베타", "names": ["베타 남은 일"], "keys": [], "truth": "open",
         "keywords": ["베타"], "project_hint": None},
        {"wid": "W03", "work": "label:감마", "names": ["감마"], "keys": [], "truth": "partial",
         "keywords": ["감마"], "project_hint": None},
        {"wid": "W04", "work": "label:델타", "names": ["델타"], "keys": [], "truth": "unknown", "keywords": []},
    ]}


def test_fix_set_categories_and_counts():
    sd = D.fix_set(_filled())
    assert [t["id"] for t in sd["closed"]] == ["W01"]
    assert [t["id"] for t in sd["controls"]] == ["W02"]
    assert [t["id"] for t in sd["partial"]] == ["W03"]
    assert [t["id"] for t in sd["unknown"]] == ["W04"]
    assert sd["counts"] == {"closed": 1, "controls": 1, "partial": 1, "unknown": 1,
                            "closed_keyed": 1, "closed_unkeyed": 0, "controls_keyed": 0, "controls_unkeyed": 1}
    assert sd["closed"][0]["name"] == "작업 알파 정리"
    # 세트 D 는 기존 judge 작업 표 모양과 맞는다
    tasks = D.tasks_from_answer_key(sd)
    assert {t["id"] for t in tasks} == {"W01", "W02", "W03"}


@pytest.mark.parametrize("patch, msg", [
    ({"keywords": ["목록에 없는 말"]}, "keyword"),         # keyword 는 목록 항목 이름·이슈 번호에서만
    ({"truth": "maybe"}, "truth"),
    ({"truth": None}, "truth"),
    ({"keywords": []}, "keyword"),
])
def test_fix_set_rejects_bad_rows(patch, msg):
    doc = _filled()
    doc["works"][0].update(patch)
    with pytest.raises(ValueError, match=msg):
        D.fix_set(doc)


def test_fix_set_accepts_bare_number_from_key():
    doc = _filled()
    doc["works"][0]["keywords"] = ["#12"]
    assert D.fix_set(doc)["closed"][0]["keywords"] == ["#12"]


# ---------------------------------------------------------------- final-review #351-1: 입력 검증(계산식 불변)

def test_tasks_of_passes_keys_so_key_only_task_matches():
    sd = {"closed": [{"id": "A", "keywords": ["이름 말"], "project_hint": None, "keys": ["repo#12"]}],
          "controls": [], "partial": []}
    tasks = S.tasks_of(sd)
    assert tasks[0]["keys"] == ["repo#12"]
    g = gen([[item("전혀 다른 표현", key="Owner/Repo #12")]])
    assert S.open_flags(g, tasks)["A"] == [1]


def _effect_inputs():
    sd = big_set()
    closed = [t["id"] for t in sd["closed"]]
    ctrls = [t["id"] for t in sd["controls"]]
    base = _flags_gen({**{c: [1] * 7 for c in closed[:5]}, **{c: [1] * 7 for c in ctrls}})
    treat = _flags_gen({**{c: [0] * 7 for c in closed[:5]}, **{c: [1] * 7 for c in ctrls}})
    return sd, base, treat, {c: "new_done_row" for c in closed[:5]}


def test_effect_needs_exactly_two_pairs():
    sd, base, treat, q = _effect_inputs()
    rep = S.evaluate(sd, [base], [treat], qualified=[q], offtable=[[]], perms=20, seed=1)
    assert rep["effect"] == "invalid-pairs"
    assert "pairs" not in rep                       # A/A 없이 상한을 만들지 않는다


def test_missing_offtable_is_pending_not_zero():
    sd, base, treat, q = _effect_inputs()
    rep = S.evaluate(sd, [base] * 2, [treat] * 2, qualified=[q, q], offtable=None, perms=20, seed=1)
    assert rep["effect"] == "pending-reading"
    rep = S.evaluate(sd, [base] * 2, [treat] * 2, qualified=[q, q], offtable=[[], []], perms=20, seed=1)
    assert rep["effect"] == "effect"


@pytest.mark.parametrize("kw", [
    {"qualified": [{}]},                            # 짝 2개인데 판독 1개
    {"qualified": [{}, {}], "offtable": [[]]},      # 표 밖 판독 1개
])
def test_reading_length_must_match_pairs(kw):
    sd, base, treat, _ = _effect_inputs()
    with pytest.raises(ValueError):
        S.evaluate(sd, [base] * 2, [treat] * 2, perms=5, seed=1, **kw)


def test_x2_needs_two_treatments():
    sd, base, treat, _ = _effect_inputs()
    with pytest.raises(ValueError):
        S.evaluate(sd, [base], [treat], x2=True, perms=5, seed=1)


def _stats_files(tmp_path, n_treat=2, qualified=None):
    sd, base, treat, q = _effect_inputs()
    (tmp_path / "sd.json").write_text(json.dumps(sd))
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "t.json").write_text(json.dumps(treat))
    args = ["stats", "--set", str(tmp_path / "sd.json"), "--base", str(tmp_path / "b.json"),
            "--base", str(tmp_path / "b.json"), "--perms", "5", "--out", str(tmp_path / "o.json")]
    for _ in range(n_treat):
        args += ["--treat", str(tmp_path / "t.json")]
    if qualified is not None:
        (tmp_path / "q.json").write_text(json.dumps({"pairs": qualified}))
        args += ["--qualified", str(tmp_path / "q.json")]
    return args


def test_stats_cmd_exit1_on_reading_length_mismatch(tmp_path):
    assert D.main(_stats_files(tmp_path, qualified=[{}])) == 1
    assert not (tmp_path / "o.json").exists()


def test_stats_cmd_exit1_on_x2_without_two_treatments(tmp_path):
    args = _stats_files(tmp_path, n_treat=0) + ["--x2"]
    assert D.main(args) == 1


def test_recall_cmd_exit1_when_any_recall_failed(tmp_path, monkeypatch):
    from bench import arch_v2_regression as A
    monkeypatch.setattr(A, "resolve_target", lambda env, p, r: A.Target("next", "x:18888", "hc-next-h", "hc-next",
                                                                        True, True))
    monkeypatch.setattr(A, "resolve_bank", lambda ns, env: "b")
    monkeypatch.setattr(A, "build_clients", lambda t, ns, env: (FakeGw(fail_on=(1,)), None))
    ins = tmp_path / "i.txt"
    ins.write_text("지시문")
    qf = tmp_path / "q.json"
    qf.write_text(json.dumps({"queries": [{"qid": "F1", "text": "a"}, {"qid": "F2", "text": "b"}]}))
    out = tmp_path / "o.json"
    assert D.main(["recall", "--instruction", str(ins), "--queries", str(qf), "--out", str(out)]) == 1
    assert json.loads(out.read_text())["totals"]["errors"] == 1     # 증거는 남기고 실패로 끝낸다


def test_table_cmd_refuses_existing_out(tmp_path):
    g = _gen_for_table()
    blind, mp = D.blind_file(g, seed=1)
    gp = tmp_path / "g.json"
    gp.write_text(json.dumps(g))
    mpath = tmp_path / "m.json"
    mpath.write_text(json.dumps({"gen_sha256": D.sha256_file(str(gp)), "map": mp}))
    rp = tmp_path / "r.json"
    rp.write_text(json.dumps({"items": [{"rid": m["rid"], "work": "label:x", "claims_open": True} for m in mp]}))
    out = tmp_path / "t.json"
    out.write_text('{"filled": true}')
    assert D.main(["table", "--gen", str(gp), "--map", str(mpath), "--reader", str(rp), "--out", str(out)]) == 1
    assert json.loads(out.read_text()) == {"filled": True}
    tb = tmp_path / "tb.json"
    tb.write_text("{}")
    out2 = tmp_path / "t2.json"
    assert D.main(["table", "--gen", str(gp), "--map", str(mpath), "--reader", str(rp), "--out", str(out2),
                   "--truth-blind", str(tb)]) == 1
    assert not out2.exists()
