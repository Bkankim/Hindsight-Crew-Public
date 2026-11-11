"""bench/r5_metrics.py · bench/r5_oracle.py - 런 5 지표 2단·노이즈 바닥·오라클(issue #362, 상위 #360 "지표(R5-A2)").

가짜 입력만 쓴다(네트워크 0, LLM 호출은 가짜 출력 주입). recall 행·키·목록 출력은 전부 합성이다(실제 항목 이름 없음).
"""
from __future__ import annotations

import ast
import json
import os
import stat
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from bench import r5_metrics as R
from bench import r5_oracle as O

REPO = Path(__file__).resolve().parent.parent
ALIASES = {"hc": "app"}
T = date(2026, 10, 1)
T_ISO = T.isoformat()


def at(day: str, hh: int = 12, mm: int = 0, ss: int = 0, us: int = 0) -> str:
    d = datetime(*map(int, day.split("-")), hh, mm, ss, us, tzinfo=R.KST)
    return d.astimezone(timezone.utc).isoformat()


def row(rid, text, tags=(), day="2026-09-20", hh=12, ledger=None, fact_type="world", **extra):
    r = {"id": rid, "text": text, "tags": list(tags), "fact_type": fact_type,
         "mentioned_at": at(day, hh) if day else None}
    if ledger is not None:
        r[R.LEDGER_FIELD] = ledger
    r.update(extra)
    return r


def todo(rid, key_text, **kw):
    return row(rid, f"[todo] {key_text} 진행 중", tags=["project:app", "kind:todo"], **kw)


def done(rid, key_text, **kw):
    return row(rid, f"[완료] {key_text} 끝냄", tags=["project:app", "kind:completion"], **kw)


def stored(*query_rows, max_tokens=16384):
    """질의마다 결과 행 목록 -> d_llm.save_recalls 출력 모양."""
    return {"query_timestamp": R.query_timestamp(T), "max_tokens": max_tokens,
            "runs": [{"q": n, "qid": f"Q{n}", "max_tokens": max_tokens, "results": list(rs)}
                     for n, rs in enumerate(query_rows, 1)]}


# --- 시간여행 필터 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("mentioned_at, kept", [
    (at("2026-10-01", 23, 59, 59, 999999), True),        # t 끝 시각 그 자체(라벨과 같은 경계)
    (at("2026-10-01", 9), True),
    (at("2026-10-02", 0, 0, 0), False),                   # t 다음날 0시 KST
    (at("2026-10-05"), False),
    (None, False),                                        # mentioned_at 없음 = 시각을 모름 = 뺀다
    ("", False),
    ("not-a-time", False),
])
def test_time_filter_drops_after_t_and_missing(mentioned_at, kept):
    r = {"id": "x", "text": "t", "mentioned_at": mentioned_at}
    rows, stat_ = R.time_filter([r], T)
    assert (rows == [r]) is kept
    assert stat_["dropped"] == (0 if kept else 1)
    assert stat_["dropped_no_time"] == (1 if not kept and not R.L.parse_instant(mentioned_at) else 0)


def test_time_filter_observation_rows_too():
    obs = row("o1", "[관찰] app#1 진행", fact_type="observation", day="2026-10-03")
    keep = row("o2", "[관찰] app#1 진행", fact_type="observation", day="2026-09-03")
    rows, st = R.time_filter([obs, keep], T)
    assert [r["id"] for r in rows] == ["o2"] and st["dropped"] == 1 and st["rows"] == 2


def test_query_timestamp_is_t_end_kst_in_utc():
    assert R.query_timestamp(date(2026, 10, 13)) == "2026-10-13T14:59:59Z"


# --- 합집합 집계 -----------------------------------------------------------------------------------

def test_union_query_order_then_rank_dedupe_by_id():
    a, b, c, d = todo("a", "app#1"), todo("b", "app#2"), todo("c", "app#3"), todo("d", "app#4")
    noid = {"text": "x", "mentioned_at": at("2026-09-01")}
    doc = stored([b, a, noid], [a, c], [d, b])
    rows, st = R.union_rows(doc)
    assert [r["id"] for r in rows] == ["b", "a", "c", "d"]
    assert st == {"source_rows": 7, "no_id_rows": 1, "union_rows": 4}


def test_union_orders_runs_by_q_not_file_order():
    a, b = todo("a", "app#1"), todo("b", "app#2")
    doc = stored([a], [b])
    doc["runs"].reverse()
    assert [r["id"] for r in R.union_rows(doc)[0]] == ["a", "b"]


def test_t_input_filters_after_union():
    late = todo("late", "app#1", day="2026-10-09")
    early = todo("early", "app#1", day="2026-09-09")
    rows, st = R.t_input(stored([late], [early, late]), T)
    assert [r["id"] for r in rows] == ["early"]
    assert st["dropped"] == 1 and st["union_rows"] == 2


# --- 행 신호: 열림 / absent / 완료 근거 / S1 주석 ---------------------------------------------------

@pytest.mark.parametrize("rows, want", [
    # 낡은 열림 행만 -> 열림
    ([todo("a", "app#1")], {"present": True, "completion_row": False, "stale_open": True, "s1_note": False,
                            "state": "open"}),
    # 완료 근거 행이 있으면 열림 아님
    ([todo("a", "app#1"), done("b", "app#1")], {"present": True, "completion_row": True, "stale_open": True,
                                                 "s1_note": False, "state": "closed"}),
    # 완료 행만 -> 열림 아님(낡은 열림 행 없음)
    ([done("b", "app#1")], {"present": True, "completion_row": True, "stale_open": False, "s1_note": False,
                            "state": "closed"}),
    # 키가 결과에 없음 -> absent(열림 아님)
    ([todo("a", "app#2")], {"present": False, "completion_row": False, "stale_open": False, "s1_note": False,
                            "state": "absent"}),
    ([], {"present": False, "completion_row": False, "stale_open": False, "s1_note": False, "state": "absent"}),
    # S1 구조 필드(done)가 그 키에 달렸으면 완료 근거
    ([todo("a", "app#1", ledger=[["app#1", "done", "2026-09-25", "ev1"]])],
     {"present": True, "completion_row": False, "stale_open": True, "s1_note": True, "state": "closed"}),
    # 다른 키의 S1 필드는 이 키의 근거가 아니다
    ([todo("a", "app#1 app#2", ledger=[["app#2", "done", "2026-09-25", "ev1"]])],
     {"present": True, "completion_row": False, "stale_open": True, "s1_note": False, "state": "open"}),
    # 본문에 주석 문안만 있고 구조 필드가 없으면 S1 주석이 아니다(필드로만 인식)
    ([row("a", "[app#1 끝남 2026-09-25] [todo] app#1 진행 중", tags=["project:app", "kind:todo"])],
     {"present": True, "completion_row": False, "stale_open": True, "s1_note": False, "state": "open"}),
    ([row("a", "[항목 상태: 끝남 2026-09-25 근거 ev1] [todo] app#1", tags=["project:app", "kind:todo"],
          gateway_item_status=True)],
     {"present": True, "completion_row": False, "stale_open": True, "s1_note": False, "state": "open"}),
    # 필드의 상태가 done 이 아니면 근거가 아니다
    ([todo("a", "app#1", ledger=[["app#1", "open", None, "ev1"]])],
     {"present": True, "completion_row": False, "stale_open": True, "s1_note": False, "state": "open"}),
    # 필드 키는 별칭 표로 정규화해 맞춘다
    ([todo("a", "app#1", ledger=[["hc#1", "done", "2026-09-25", "ev1"]])],
     {"present": True, "completion_row": False, "stale_open": True, "s1_note": True, "state": "closed"}),
    # 망가진 필드는 무시
    ([todo("a", "app#1", ledger="garbage")],
     {"present": True, "completion_row": False, "stale_open": True, "s1_note": False, "state": "open"}),
    ([todo("a", "app#1", ledger=[["app#1"], 5, ["bad", "done", None, "x"]])],
     {"present": True, "completion_row": False, "stale_open": True, "s1_note": False, "state": "open"}),
    # 별칭 표기(hc#1)도 같은 키
    ([row("a", "[todo] hc#1 남음", tags=["kind:todo"])],
     {"present": True, "completion_row": False, "stale_open": True, "s1_note": False, "state": "open"}),
    # 완료 계열은 라벨 쪽 규칙(태그)만: 본문 라벨 '[완료]' 는 보지 않는다
    ([row("a", "[완료] app#1 끝", tags=["project:app", "kind:decision"])],
     {"present": True, "completion_row": False, "stale_open": True, "s1_note": False, "state": "open"}),
    ([row("a", "app#1 끝", tags=["project:app", "KIND:Done"])],
     {"present": True, "completion_row": True, "stale_open": False, "s1_note": False, "state": "closed"}),
])
def test_pair_signal_matrix(rows, want):
    sig = R.t_signals(rows, ["app#1"], ALIASES)["app#1"]
    assert sig == want


def test_signals_only_for_requested_keys():
    sig = R.t_signals([todo("a", "app#1 app#2")], ["app#2"], ALIASES)
    assert set(sig) == {"app#2"} and sig["app#2"]["state"] == "open"


def _imports(mod):
    tree = ast.parse((REPO / mod).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {(node.module, a.name) for a in node.names}
        if isinstance(node, ast.Import):
            names |= {(a.name, None) for a in node.names}
    return names


def test_metrics_module_does_not_import_gateway_completion_rules():
    """지표(검색·d-llm)는 처치(S1·X1) 완료 판정 쪽으로 기울지 않게 gateway 에서 extract_keys 만 import 한다."""
    gw = {n for n in _imports("bench/r5_metrics.py") if (n[0] or "").startswith("gateway")}
    assert gw <= {("gateway.item_keys", "extract_keys")}, gw


def test_oracle_o3_uses_s1_completion_not_label_rules():
    """o3 표(#362 범위 확장)는 S1 쪽 완료 판정만 쓰고 라벨 쪽 모듈(bench/r5_labels.py)을 직접 import 하지 않는다."""
    names = _imports("bench/r5_oracle.py")
    assert {n for n in names if (n[0] or "").startswith("gateway")} <= {("gateway.item_status", "is_completion_family")}
    assert not any("r5_labels" in (m or "") or a == "r5_labels" for m, a in names), names


# --- G1 / G2 recall 경로 ----------------------------------------------------------------------------

def sig(state):
    return {"present": state != "absent", "completion_row": state == "closed", "stale_open": state == "open",
            "s1_note": False, "state": state}


def test_g1_recall_denominator_is_positive_present_in_base():
    pos = {("k#1", "t"), ("k#2", "t"), ("k#3", "t")}
    base = {("k#1", "t"): sig("open"), ("k#2", "t"): sig("closed"), ("k#3", "t"): sig("absent")}
    treat = {("k#1", "t"): sig("closed"), ("k#2", "t"): sig("open"), ("k#3", "t"): sig("open")}
    v = R.g1_values(base, base, pos)
    assert v == {("k#1", "t"): 1, ("k#2", "t"): 0}                # absent 는 분모 밖
    assert R.g1_values(base, treat, pos) == {("k#1", "t"): 0, ("k#2", "t"): 1}
    assert R.rate(v) == pytest.approx(0.5)


def test_g2_recall_kept_and_unmarked():
    ctl = {("c#1", "t"), ("c#2", "t"), ("c#3", "t"), ("c#4", "t")}
    base = {("c#1", "t"): sig("open"), ("c#2", "t"): sig("open"), ("c#3", "t"): sig("absent"),
            ("c#4", "t"): sig("closed")}
    treat = {("c#1", "t"): sig("open"), ("c#2", "t"): sig("absent"), ("c#3", "t"): sig("open"),
             ("c#4", "t"): sig("closed")}
    # 분모 = 기준선에 나온 대조 쌍(c#1, c#2, c#4). 처치 뒤에도 나오고 완료 표시가 없어야 1
    assert R.g2_values(base, treat, ctl) == {("c#1", "t"): 1, ("c#2", "t"): 0, ("c#4", "t"): 0}
    s1 = dict(sig("open"), s1_note=True)
    assert R.g2_values(base, {("c#1", "t"): s1}, {("c#1", "t")}) == {("c#1", "t"): 0}


def test_rate_empty_is_none():
    assert R.rate({}) is None


# --- 키 군집 재표집 -----------------------------------------------------------------------------------

def test_cluster_bootstrap_deterministic_and_key_clustered():
    a = {("k#1", "t1"): 1, ("k#1", "t2"): 1, ("k#2", "t1"): 0, ("k#3", "t1"): 1, ("k#4", "t1"): 0}
    b = {("k#1", "t1"): 0, ("k#1", "t2"): 0, ("k#2", "t1"): 0, ("k#3", "t1"): 1, ("k#4", "t1"): 0}
    r1 = R.cluster_bootstrap(a, b, seed=7, n=300)
    r2 = R.cluster_bootstrap(dict(reversed(list(a.items()))), dict(reversed(list(b.items()))), seed=7, n=300)
    assert r1 == r2                                       # 입력 순서와 무관, 같은 씨앗 같은 결과
    assert r1 != R.cluster_bootstrap(a, b, seed=8, n=300)
    assert r1["diff"] == pytest.approx(0.4) and r1["clusters"] == 4 and r1["pairs"] == 5
    assert 0 <= r1["abs_p95"] <= 1 and r1["lo"] <= r1["diff"] <= r1["hi"]


def test_cluster_bootstrap_resamples_keys_not_pairs(monkeypatch):
    """같은 키의 t 들은 함께 뽑힌다: 표본마다 한 키의 쌍이 전부 들어가거나 전부 빠진다."""
    a = {("k#1", "t1"): 1, ("k#1", "t2"): 1, ("k#1", "t3"): 1, ("k#2", "t1"): 0}
    b = {p: 0 for p in a}
    seen = set()
    for s in range(30):
        r = R.cluster_bootstrap(a, b, seed=s, n=50, keep_samples=True)
        seen |= {round(x, 6) for x in r["samples"]}
    # 쌍 단위였다면 1/4, 2/4 같은 값이 나온다. 키 단위면 diff 는 (k#1 뽑힌 수 x 3)/(쌍 수) 모양뿐
    assert seen <= {0.0, 0.75, 1.0} and len(seen) > 1


def test_cluster_bootstrap_identical_runs_zero():
    a = {("k#1", "t1"): 1, ("k#2", "t1"): 0}
    r = R.cluster_bootstrap(a, dict(a), seed=1, n=100)
    assert r["diff"] == 0 and r["abs_p95"] == 0


def test_cluster_bootstrap_requires_same_pairs():
    with pytest.raises(ValueError):
        R.cluster_bootstrap({("k#1", "t"): 1}, {("k#2", "t"): 1}, seed=1, n=10)


def test_noise_floor_max_over_rep_pairs():
    base = {("k#1", "t"): 1, ("k#2", "t"): 0, ("k#3", "t"): 0}
    reps = [base, dict(base), {**base, ("k#2", "t"): 1}]
    nf = R.noise_floor(reps, seed=3, n=200)
    assert nf["observed_max_abs"] == pytest.approx(1 / 3)
    assert len(nf["pairs"]) == 3
    assert nf["floor"] == max(p["abs_p95"] for p in nf["pairs"])
    assert R.noise_floor(reps, seed=3, n=200) == nf


# --- d-llm 쌍 상태와 G1 분모·invalid 칸 ---------------------------------------------------------------

@pytest.mark.parametrize("states, want", [
    (["open", "open", "done"], "open"),
    (["open", "absent", "absent"], "not_open"),
    (["open", "invalid", "absent"], "invalid"),          # invalid 반복이 과반을 바꿀 수 있으면 별도 칸
    (["invalid", "invalid", "absent"], "invalid"),
    (["invalid", "absent", "absent"], "not_open"),
    (["open", "open", "invalid"], "open"),
    (["done", "done", "done"], "not_open"),
])
def test_dllm_pair_state(states, want):
    assert R.dllm_pair_state(states) == want


def test_dllm_g1_invalid_column_stays_in_denominator():
    base_present = {("k#1", "t"), ("k#2", "t"), ("k#3", "t")}
    pos = {("k#1", "t"), ("k#2", "t"), ("k#3", "t"), ("k#4", "t")}
    st = {("k#1", "t"): "open", ("k#2", "t"): "invalid", ("k#3", "t"): "not_open", ("k#4", "t"): "open"}
    g = R.dllm_rates(st, pos, base_present)
    assert g == {"den": 3, "open": 1, "invalid": 1, "not_open": 1, "rate": pytest.approx(1 / 3),
                 "invalid_rate": pytest.approx(1 / 3)}


def test_dllm_tasks_key_and_alias_forms():
    tasks = R.dllm_tasks(["app#1"], ALIASES)
    from bench import d_llm_match as M
    assert M.match_item({"key": "hc#1", "name": "x", "status": "open"}, tasks)["task"] == "app#1"
    assert M.match_item({"key": "Owner/app #1", "name": "x", "status": "open"}, tasks)["task"] == "app#1"
    assert M.match_item({"name": "app#1 정리", "status": "open"}, tasks)["task"] == "app#1"
    assert M.match_item({"key": "app#2", "name": "y", "status": "open"}, tasks)["task"] is None


def test_judge_t_maps_states_per_key_with_fake_generations():
    gens = [
        {"valid": True, "items": [{"key": "app#1", "name": "a", "status": "open", "evidence_ids": [], "quote": ""}]},
        {"valid": False, "items": []},
        {"valid": True, "items": [{"key": "app#1", "name": "a", "status": "done", "evidence_ids": [], "quote": ""},
                                  {"key": "app#2", "name": "b", "status": "open", "evidence_ids": [], "quote": ""}]},
    ]
    res = R.judge_t(gens, ["app#1", "app#2", "app#3"], ALIASES)
    assert res["states"]["app#1"] == ["open", "invalid", "done"]
    assert res["states"]["app#2"] == ["absent", "invalid", "open"]
    assert res["invalid_reps"] == 1 and res["empty_reps"] == 0


def test_judge_t_empty_list_rep_is_invalid():
    gens = [{"valid": True, "items": []}, {"valid": True, "items": []}, {"valid": True, "items": []}]
    res = R.judge_t(gens, ["app#1"], ALIASES)
    assert res["states"]["app#1"] == ["invalid"] * 3 and res["empty_reps"] == 3


def test_oracle_reduction():
    base = {("k#1", "t"): "open", ("k#2", "t"): "open", ("k#3", "t"): "not_open", ("k#4", "t"): "open"}
    orc = {("k#1", "t"): "not_open", ("k#2", "t"): "open", ("k#3", "t"): "open", ("k#4", "t"): "invalid"}
    red = R.oracle_reduction(base, orc, {("k#1", "t"), ("k#2", "t"), ("k#3", "t"), ("k#4", "t")})
    assert red == {"wrong": 3, "fixed": 1, "still_open": 1, "invalid": 1, "pct": pytest.approx(100 / 3),
                   "new_open": 1}


@pytest.mark.parametrize("o1, o2, want", [
    (60.0, 10.0, "통과"), (10.0, 50.0, "통과"), (49.9, 49.9, "멈춤"), (None, None, "멈춤"), (0.0, 0.0, "멈춤"),
])
def test_gate_line(o1, o2, want):
    line = R.gate_line(o1, o2)
    assert line.startswith("오라클 관문: o1 ") and line.endswith(f"-> {want}" if want == "통과" else "-> 멈춤(둘 다 50% 미만)")


# --- 오라클 o1·o2 변환 -------------------------------------------------------------------------------

META = [
    {"id": "ev-old", "state": "valid", "mentioned_at": at("2026-09-21"), "keys": ["app#1"], "completion": True},
    {"id": "ev-new", "state": "valid", "mentioned_at": at("2026-09-29"), "keys": ["app#1"], "completion": True},
    {"id": "ev-late", "state": "valid", "mentioned_at": at("2026-10-02", 1), "keys": ["app#1"], "completion": True},
    {"id": "ev-inv", "state": "invalidated", "mentioned_at": at("2026-09-30"), "keys": ["app#1"], "completion": True},
    {"id": "not-done", "state": "valid", "mentioned_at": at("2026-09-30"), "keys": ["app#1"], "completion": False},
    {"id": "ev2", "state": "valid", "mentioned_at": at("2026-09-22"), "keys": ["app#2"], "completion": True},
]


def test_completion_evidence_latest_valid_before_t():
    ev = O.completion_evidence(META, ["app#1", "app#2", "app#3"], T)
    assert ev == {"app#1": {"id": "ev-new", "date": "2026-09-29"}, "app#2": {"id": "ev2", "date": "2026-09-22"}}


def test_o1_annotates_stale_rows_of_positive_keys_only():
    rows = [todo("a", "app#1"), done("b", "app#1"), todo("c", "app#2"), todo("d", "app#3"),
            todo("e", "app#1 app#3")]
    ev = {"app#1": {"id": "ev-new", "date": "2026-09-29"}, "app#2": {"id": "ev2", "date": "2026-09-22"}}
    out, st = O.o1(rows, positives={"app#1", "app#3"}, evidence=ev, aliases=ALIASES)
    by = {r["id"]: r for r in out}
    assert [r["id"] for r in out] == ["a", "b", "c", "d", "e"]          # 순서·행 수 불변
    assert by["a"]["text"].startswith(O.note("app#1", ev["app#1"]) + " ")
    assert by["b"]["text"] == rows[1]["text"]                             # 완료 계열 행은 그대로
    assert by["c"]["text"] == rows[2]["text"]                             # 양성이 아닌 키(대조 등)는 주석 없음
    assert by["d"]["text"] == rows[3]["text"]                             # 근거 없는 양성 키는 주석 없음
    assert by["e"]["text"].startswith(O.note("app#1", ev["app#1"]) + " ")
    assert rows[0]["text"] == "[todo] app#1 진행 중"                      # 입력은 바꾸지 않는다
    assert R.LEDGER_FIELD not in by["a"]                                  # S1 구조 필드를 흉내 내지 않는다
    assert st == {"rows": 5, "annotated_rows": 2, "annotated_keys": 1}


def test_o2_drops_stale_rows_of_positive_keys_keeps_mixed_with_control():
    rows = [todo("a", "app#1"), done("b", "app#1"), todo("c", "app#2"), todo("e", "app#1 app#2"),
            todo("f", "app#1 app#9"), row("g", "무관")]
    out, st = O.o2(rows, positives={"app#1"}, controls={"app#2"}, aliases=ALIASES)
    assert [r["id"] for r in out] == ["b", "c", "e", "g"]
    assert st == {"rows": 6, "dropped_rows": 2, "kept_mixed_control": 1}


def test_oracle_then_signals_close_positive_in_recall_metric_only_by_s1_field_absence():
    """o1 주석은 본문 문안이라 recall 지표의 S1 인식(구조 필드만)에 걸리지 않는다(지표 불변)."""
    rows = [todo("a", "app#1")]
    out, _ = O.o1(rows, {"app#1"}, {"app#1": {"id": "e", "date": "2026-09-29"}}, ALIASES)
    assert R.t_signals(out, ["app#1"], ALIASES)["app#1"]["state"] == "open"
    out2, _ = O.o2(rows, {"app#1"}, set(), ALIASES)
    assert R.t_signals(out2, ["app#1"], ALIASES)["app#1"]["state"] == "absent"


# --- 질의 계획과 봉인 가드 ----------------------------------------------------------------------------

LABELSET = [
    {"key": "app#1", "t": "2026-09-28", "label": "positive", "split": "dev", "fold": "r01"},
    {"key": "app#2", "t": "2026-09-28", "label": "control", "split": "dev", "fold": "r01"},
    {"key": "lib#3", "t": "2026-10-01", "label": "control", "split": "dev", "fold": "r02"},
    {"key": "lib#4", "t": "2026-10-01", "label": "data_layer", "split": "dev", "fold": "r02"},
    {"key": "app#5", "t": "2026-10-22", "label": "positive", "split": "sealed", "fold": "r01"},
    {"key": "app#5", "t": "2026-10-25", "label": "positive", "split": "sealed", "fold": "r01"},
    {"key": "app#6", "t": "2026-10-16", "label": "positive", "split": "unused", "fold": "r01"},
    {"key": "app#7", "t": "2026-10-01", "label": "control_no_memory", "split": "dev", "fold": "r01"},
]

FAMILIES = {"queries": [{"qid": "F1", "scope": "all", "text": "fam one"}, {"qid": "F2", "scope": "all", "text": "fam two"}]}
INSTR = "기준 시각은 2026-10-28 23:59:59 KST(2026-10-28T14:59:59Z)다. 끝."
PTPL = "{project} 상태"


def test_dev_pairs_only_dev_positive_control():
    pos, ctl = R.dev_pairs(LABELSET)
    assert pos == {("app#1", "2026-09-28")} and ctl == {("app#2", "2026-09-28"), ("lib#3", "2026-10-01")}


def test_plan_dev_t_only_with_projects_and_instruction_dates():
    plan = R.make_plan(LABELSET, FAMILIES, INSTR, PTPL)
    assert sorted(plan["per_t"]) == ["2026-09-28", "2026-10-01"]
    q = plan["per_t"]["2026-09-28"]
    assert [x["qid"] for x in q["queries"]] == ["F1", "F2", "P:app"]
    assert q["queries"][2]["text"] == "app 상태"
    assert q["instruction"] == "기준 시각은 2026-09-28 23:59:59 KST(2026-09-28T14:59:59Z)다. 끝."
    assert q["query_timestamp"] == "2026-09-28T14:59:59Z"
    assert [x["qid"] for x in plan["per_t"]["2026-10-01"]["queries"]] == ["F1", "F2", "P:lib"]


def test_plan_extra_t_families_only():
    plan = R.make_plan(LABELSET, FAMILIES, INSTR, PTPL, extra_t=["2026-10-28"])
    assert [x["qid"] for x in plan["per_t"]["2026-10-28"]["queries"]] == ["F1", "F2"]


def test_plan_refuses_instruction_without_reference_date():
    with pytest.raises(R.Fail):
        R.make_plan(LABELSET, FAMILIES, "날짜 없는 지시", PTPL)


@pytest.mark.parametrize("t", ["2026-10-20", "2026-10-22", "2026-10-27"])
def test_seal_guard_refuses_sealed_window_t(t):
    with pytest.raises(R.Fail):
        R.check_not_sealed(LABELSET, [t])


def test_seal_guard_allows_dev_t_and_post_label_keyless_t():
    R.check_not_sealed(LABELSET, ["2026-09-28", "2026-10-28"])


def test_search_pairs_exclude_sealed_and_unused():
    recalls = {"2026-09-28": [todo("a", "app#1 app#2 app#5 app#6")], "2026-10-01": [todo("b", "lib#3")]}
    res = R.search_signals(recalls, LABELSET, ALIASES)
    assert set(res) == {("app#1", "2026-09-28"), ("app#2", "2026-09-28"), ("lib#3", "2026-10-01")}


# --- 파일 권한 ----------------------------------------------------------------------------------------

def test_write_private_0600(tmp_path):
    p = tmp_path / "d" / "x.json"
    R.write_private(str(p), {"a": 1})
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert json.loads(p.read_text()) == {"a": 1}


# --- 주 지표 P·R (#362 범위 확장) ----------------------------------------------------------------------

POS = {("p#1", "t"), ("p#2", "t"), ("p#3", "t")}
CTL = {("c#1", "t"), ("c#2", "t"), ("c#3", "t"), ("c#4", "t")}


def test_pr_summary_precision_recall_and_present_denominator():
    st = {("p#1", "t"): "open", ("p#2", "t"): "not_open", ("p#3", "t"): "invalid",
          ("c#1", "t"): "open", ("c#2", "t"): "open", ("c#3", "t"): "not_open", ("c#4", "t"): "invalid"}
    present = {("c#1", "t"), ("c#3", "t"), ("c#4", "t"), ("p#1", "t")}
    s = R.pr_summary(st, POS, CTL, present)
    # P: open 으로 나온 라벨 쌍 = p#1, c#1, c#2 -> 대조 2/3. invalid 는 open 이 아니다
    assert (s["P_num"], s["P_den"], s["open_positive"]) == (2, 3, 1) and s["P"] == pytest.approx(2 / 3)
    # R(= G2): 기준선에 나온 대조 c#1·c#3·c#4 중 open 1. invalid 는 분모 안
    assert (s["R_num"], s["R_den"]) == (1, 3) and s["R"] == pytest.approx(1 / 3)
    # R_all: 대조 전부 4 중 open 2
    assert (s["R_all_num"], s["R_all_den"]) == (2, 4)


def test_pr_summary_no_open_is_none():
    s = R.pr_summary({p: "not_open" for p in POS | CTL}, POS, CTL, POS | CTL)
    assert s["P"] is None and s["P_den"] == 0 and s["R"] == 0


def test_cluster_bootstrap_ratio_values_deterministic():
    a = {("k#1", "t"): (1, 1), ("k#2", "t"): (0, 1), ("k#3", "t"): (0, 0), ("k#4", "t"): (1, 1)}
    b = {("k#1", "t"): (1, 1), ("k#2", "t"): (0, 0), ("k#3", "t"): (0, 1), ("k#4", "t"): (0, 1)}
    r = R.cluster_bootstrap(a, b, seed=5, n=200)
    assert r["diff"] == pytest.approx(2 / 3 - 1 / 3)
    assert r == R.cluster_bootstrap(dict(reversed(list(a.items()))), b, seed=5, n=200)
    assert R.ratio(a) == pytest.approx(2 / 3) and R.ratio({}) is None


def test_noise_floor_accepts_ratio_values():
    a = {("k#1", "t"): (1, 1), ("k#2", "t"): (0, 1)}
    nf = R.noise_floor([a, dict(a)], seed=1, n=50)
    assert nf["floor"] == 0 and nf["rates"] == [0.5, 0.5]


def test_r_causes_open_row_completion_only_absent():
    sig_ = {("c#1", "t"): {"present": True, "stale_open": True},
            ("c#2", "t"): {"present": True, "stale_open": True},
            ("c#3", "t"): {"present": True, "stale_open": False, "completion_row": True},
            ("c#4", "t"): {"present": False}}
    st = {("c#1", "t"): "open", ("c#2", "t"): "not_open", ("c#3", "t"): "not_open", ("c#4", "t"): "invalid"}
    c = R.r_causes(sig_, st, CTL)
    assert (c["b_open_row"], c["b_completion_only"], c["b_absent"]) == (2, 1, 1)
    assert c["a_open_row_missed"] == 1 and c["open_row_dllm_open"] == 1 and c["missed"] == 3


def test_judge_t_counts_open_list_items_and_unmapped():
    gens = [{"valid": True, "items": [
        {"key": "app#1", "name": "a", "status": "open", "evidence_ids": [], "quote": ""},
        {"key": "zzz#9", "name": "z", "status": "open", "evidence_ids": [], "quote": ""},
        {"key": "app#2", "name": "b", "status": "done", "evidence_ids": [], "quote": ""}]},
        {"valid": False, "items": []}]
    res = R.judge_t(gens, ["app#1", "app#2"], ALIASES)
    assert res["open_items"] == [2, None] and res["open_unmapped"] == [1, None]


# --- 오라클 o3: 뱅크 상태 표 --------------------------------------------------------------------------

TAG_ALIASES = {"kind:work-log": "kind:completion", "kind:done": "kind:completion"}


def mm(mid, day, keys, tags, state="valid", completion=None, hh=12):
    m = {"id": mid, "state": state, "mentioned_at": at(day, hh), "keys": list(keys), "tags": list(tags)}
    if completion is not None:
        m["completion"] = completion
    return m


O3_META = [
    mm("a1", "2026-09-10", ["app#1"], ["kind:todo"]),
    mm("a2", "2026-09-20", ["app#1"], ["kind:work-log"]),          # 템플릿 별칭으로 완료 계열(라벨 쪽 규칙은 아님)
    mm("a3", "2026-09-25", ["app#1"], ["kind:todo"]),               # 완료 뒤 언급 - 상태는 최신 완료로 끝남
    mm("b1", "2026-09-12", ["app#2"], ["kind:handoff"], completion=True),   # 라벨 쪽 completion 칸은 무시
    mm("b2", "2026-10-03", ["app#2"], ["kind:completion"]),         # t 뒤
    mm("c1", "2026-09-15", ["app#3"], ["kind:completion"], state="invalidated"),
    mm("c2", "2026-09-14", ["app#3"], ["kind:decision"]),
    mm("d1", "2026-09-15", ["lib#4"], ["kind:completion"]),          # 범위 밖 프로젝트
    mm("e1", "2026-10-02", ["app#5"], ["kind:todo"]),                # t 뒤 언급만 - 표에 없음
    mm("f1", "2026-09-01", ["app#10"], ["KIND:DONE"]),
]


def test_status_table_bank_only_states():
    rows = O.status_table(O3_META, {"app"}, T, TAG_ALIASES)
    assert rows == [
        {"key": "app#1", "state": "끝남", "date": "2026-09-20", "id": "a2"},
        {"key": "app#2", "state": "열림", "date": "2026-09-12", "id": "b1"},
        {"key": "app#3", "state": "열림", "date": "2026-09-14", "id": "c2"},
        {"key": "app#10", "state": "끝남", "date": "2026-09-01", "id": "f1"},
    ]


def test_status_table_text_and_stats():
    rows = O.status_table(O3_META, {"app", "lib"}, T, TAG_ALIASES)
    text = O.table_text(rows, "2026-10-01")
    lines = text.splitlines()
    assert lines[0].startswith("<item_status_table>") and lines[-1] == "</item_status_table>"
    assert "app#1 | 끝남 | 2026-09-20 | a2" in lines and "lib#4 | 끝남 | 2026-09-15 | d1" in lines
    st = O.table_stats(rows, text, counter=len)
    assert st == {"keys": 5, "done": 3, "open": 2, "chars": len(text), "tokens": len(text), "keys_per_project": [4, 1]}


def test_load_tag_aliases_from_template(tmp_path):
    p = tmp_path / "pol.json"
    p.write_text(json.dumps({"banks": {"x": {"tag_aliases": {"kind:done": "kind:completion"}}}}))
    assert O.load_tag_aliases(str(p)) == {"kind:done": "kind:completion"}
    assert "kind:work-log" in O.load_tag_aliases(str(REPO / "policies/templates/personal-bank.json"))


def test_build_prompt_with_table_matches_baseline_without_table():
    from bench import d_llm as D
    asm = D.assemble(stored([todo("a", "app#1")]))
    tpl = "Q {query_date}\n{recall_rows}\nEND"
    assert R.build_prompt_with_table(D, tpl, asm, "2026-10-01", "") == D.build_prompt(tpl, asm, query_date="2026-10-01")
    with_t = R.build_prompt_with_table(D, tpl, asm, "2026-10-01", "<item_status_table>x</item_status_table>")
    assert with_t.index(D.render_row(asm["rows"][0])) < with_t.index("<item_status_table>") < with_t.index("END")


def test_query_projects_from_plan_and_families(tmp_path):
    plan = tmp_path / "plan.json"
    fam = tmp_path / "fam.json"
    plan.write_text(json.dumps({"per_t": {"2026-10-01": {"queries": [{"qid": "F1"}, {"qid": "P:app"}]},
                                          "2026-10-28": {"queries": [{"qid": "F1"}]}}}))
    fam.write_text(json.dumps({"queries": [{"qid": "F1", "scope": "all"}, {"qid": "F2", "project": "lib"}]}))
    assert R.query_projects(str(plan), str(fam)) == {"2026-10-01": {"app", "lib"}, "2026-10-28": {"lib"}}


# --- R_bank: 뱅크가 열림으로 기억하는 대조만 분모(#362 코디네이터 결정, 운영자 direction-4 2(c)) --------------

def test_bank_states_strict_and_loose():
    meta = [
        mm("a1", "2026-09-10", ["app#1"], ["kind:todo"]),
        mm("a2", "2026-09-20", ["app#1"], ["kind:work-log"]),       # 완료 계열(템플릿 별칭)
        mm("a3", "2026-09-25", ["app#1"], ["kind:todo"]),           # 완료 뒤 비완료 언급 -> 느슨한 판본은 열림
        mm("b1", "2026-09-12", ["app#2"], ["kind:handoff"]),        # 완료 없음 -> 둘 다 열림
        mm("c1", "2026-09-12", ["app#3"], ["kind:todo"]),
        mm("c2", "2026-09-22", ["app#3"], ["kind:completion"]),     # 완료가 최신 -> 둘 다 끝남
        mm("d1", "2026-10-05", ["app#4"], ["kind:todo"]),           # t 뒤만 -> 상태 없음
        mm("e1", "2026-09-12", ["app#5"], ["kind:todo"], state="invalidated"),
    ]
    st = O.bank_states(meta, ["app#1", "app#2", "app#3", "app#4", "app#5", "app#9"], T, TAG_ALIASES)
    assert st == {"app#1": {"strict": "done", "loose": "open"}, "app#2": {"strict": "open", "loose": "open"},
                  "app#3": {"strict": "done", "loose": "done"}, "app#4": {"strict": None, "loose": None},
                  "app#5": {"strict": None, "loose": None}, "app#9": {"strict": None, "loose": None}}


def test_bank_states_agree_with_status_table():
    rows = {r["key"]: r["state"] for r in O.status_table(O3_META, {"app"}, T, TAG_ALIASES)}
    st = O.bank_states(O3_META, sorted(rows), T, TAG_ALIASES)
    assert {k: ("열림" if v["strict"] == "open" else "끝남") for k, v in st.items()} == rows


def test_r_bank_denominators():
    ctl = {("c#1", "t"), ("c#2", "t"), ("c#3", "t"), ("c#4", "t")}
    st = {("c#1", "t"): "open", ("c#2", "t"): "not_open", ("c#3", "t"): "open", ("c#4", "t"): "invalid"}
    bank = {("c#1", "t"): {"strict": "open", "loose": "open"}, ("c#2", "t"): {"strict": "open", "loose": "open"},
            ("c#3", "t"): {"strict": "done", "loose": "open"}, ("c#4", "t"): {"strict": None, "loose": None}}
    r = R.r_bank(st, bank, ctl)
    assert (r["R_bank_num"], r["R_bank_den"]) == (1, 2) and r["R_bank"] == pytest.approx(0.5)
    assert (r["R_bank2_num"], r["R_bank2_den"]) == (2, 3)
    assert r["bank_strict"] == {"open": 2, "done": 1, "none": 1}


# --- final-review #362-1 ------------------------------------------------------------------------------

def _cli_fixture(tmp_path):
    """dllm CLI 를 가짜 호출기로 돌리기 위한 최소 입력(라벨셋·별칭 표·recall 저장)."""
    ls = tmp_path / "labelset.jsonl"
    ls.write_text("\n".join(json.dumps(r) for r in [
        {"key": "app#1", "t": "2026-09-28", "label": "positive", "split": "dev", "fold": "r01"},
        {"key": "app#2", "t": "2026-10-01", "label": "control", "split": "dev", "fold": "r01"},
        {"key": "app#3", "t": "2026-10-22", "label": "positive", "split": "sealed", "fold": "r01"}]) + "\n")
    al = tmp_path / "aliases.json"
    al.write_text(json.dumps({"hc": "app"}))
    rec = tmp_path / "rec"
    rec.mkdir()
    for t in ("2026-09-28", "2026-10-01"):
        (rec / f"t-{t}.json").write_text(json.dumps(
            {"totals": {"errors": 0}, "runs": [{"q": 1, "max_tokens": 1, "results": [todo("a", "app#1", day="2026-09-20")]}]}))
    out = tmp_path / "gen"
    out.mkdir()
    return ls, al, rec, out


def _fake_caller(calls, cost):
    def call(prompt):
        calls.append(1)
        return {"is_error": False, "result": json.dumps({"items": [{"key": "app#1", "name": "a", "status": "open",
                                                                      "evidence_ids": [], "quote": ""}]}),
                "total_cost_usd": cost, "usage": {}}
    return call


def _gen_doc(label, t, n_gens, cost, finished=True, k=3):
    g = {"valid": True, "items": [], "calls": [{"cost_usd": cost, "valid": True}]}
    d = {"label": label, "t": t, "k": k, "generations": [g] * n_gens}
    if finished:
        d["finished_kst"] = "2026-11-01 00:00:00"
    return d


def _dllm_args(ls, al, rec, out, label, ts, max_usd=None):
    a = ["dllm", "--labelset", str(ls), "--aliases", str(al), "--recalls", str(rec), "--oracle", "none", "--k", "3",
         "--out-dir", str(out), "--label", label]
    for t in ts:
        a += ["--t", t]
    if max_usd is not None:
        a += ["--max-usd", str(max_usd)]
    return a


def test_dllm_refuses_incomplete_existing_gen(tmp_path, monkeypatch, capsys):
    from bench import d_llm as D
    ls, al, rec, out = _cli_fixture(tmp_path)
    (out / "b-2026-09-28-gen.json").write_text(json.dumps(_gen_doc("b", "2026-09-28", 1, 0.1, finished=False)))
    calls = []
    monkeypatch.setattr(D, "claude_caller", lambda *a, **k: _fake_caller(calls, 0.1))
    assert R.main(_dllm_args(ls, al, rec, out, "b", ["2026-09-28"])) == 1
    err = capsys.readouterr().err
    assert "incomplete" in err and "move" not in err and calls == []


def test_dllm_skips_complete_existing_gen(tmp_path, monkeypatch):
    from bench import d_llm as D
    ls, al, rec, out = _cli_fixture(tmp_path)
    (out / "b-2026-09-28-gen.json").write_text(json.dumps(_gen_doc("b", "2026-09-28", 3, 0.1)))
    calls = []
    monkeypatch.setattr(D, "claude_caller", lambda *a, **k: _fake_caller(calls, 0.1))
    assert R.main(_dllm_args(ls, al, rec, out, "b", ["2026-09-28"])) == 0 and calls == []


def test_judge_label_refuses_incomplete_gen(tmp_path):
    (tmp_path / "b-2026-09-28-gen.json").write_text(json.dumps(_gen_doc("b", "2026-09-28", 2, 0.1, finished=False)))
    with pytest.raises(R.Fail):
        R.judge_label(str(tmp_path), "b", ["2026-09-28"], {("app#1", "2026-09-28")}, set(), ALIASES)


def test_dllm_spend_counts_prior_complete_and_incomplete_files(tmp_path, monkeypatch, capsys):
    """같은 라벨의 앞선 실행 지출(완료 + 미완료 파일)을 합산해, 한도에 이미 닿았으면 호출 0으로 exit 3."""
    from bench import d_llm as D
    ls, al, rec, out = _cli_fixture(tmp_path)
    (out / "b-2026-09-25-gen.json").write_text(json.dumps(_gen_doc("b", "2026-09-25", 3, 0.5)))             # 1.5
    (out / "b-2026-09-22-gen.json").write_text(json.dumps(_gen_doc("b", "2026-09-22", 2, 0.4, finished=False)))  # 0.8
    (out / "other-2026-09-25-gen.json").write_text(json.dumps(_gen_doc("other", "2026-09-25", 3, 9.0)))     # 다른 라벨
    calls = []
    monkeypatch.setattr(D, "claude_caller", lambda *a, **k: _fake_caller(calls, 0.1))
    assert R.main(_dllm_args(ls, al, rec, out, "b", ["2026-10-01"], max_usd=2.2)) == 3
    assert calls == [] and not (out / "b-2026-10-01-gen.json").exists()
    assert "move" not in capsys.readouterr().err


def test_dllm_spend_checked_before_every_rep(tmp_path, monkeypatch):
    from bench import d_llm as D
    ls, al, rec, out = _cli_fixture(tmp_path)
    (out / "b-2026-09-25-gen.json").write_text(json.dumps(_gen_doc("b", "2026-09-25", 3, 0.5)))             # 1.5
    calls = []
    monkeypatch.setattr(D, "claude_caller", lambda *a, **k: _fake_caller(calls, 0.6))
    assert R.main(_dllm_args(ls, al, rec, out, "b", ["2026-10-01"], max_usd=2.0)) == 3
    assert len(calls) == 1                                       # 1.5 + 0.6 = 2.1 >= 2.0 -> 2번째 반복 전에 멈춤
    left = json.loads((out / "b-2026-10-01-gen.json").read_text())
    assert len(left["generations"]) == 1 and not R.gen_complete(left, 3)


def test_cluster_bootstrap_drops_zero_denominator_resamples():
    """비율 지표에서 분모 0 재표집은 0 으로 세지 않고 빼며, 뺀 수를 남긴다."""
    a = {("k#1", "t"): (1, 1), ("k#2", "t"): (0, 0)}
    b = {("k#1", "t"): (1, 1), ("k#2", "t"): (0, 1)}
    r = R.cluster_bootstrap(a, b, seed=3, n=400, keep_samples=True)
    assert r["diff"] == pytest.approx(0.5)
    assert r["dropped_zero_den"] > 0 and r["n"] + r["dropped_zero_den"] == 400
    assert len(r["samples"]) == r["n"] and all(x in (0.0, 1.0 / 3.0, 0.5) for x in r["samples"])
    assert R.cluster_bootstrap({("k#1", "t"): 1}, {("k#1", "t"): 0}, seed=3, n=50)["dropped_zero_den"] == 0


def test_keyless_judge_empty_list_rep_is_invalid():
    """키 없는 기준선도 키 있는 경로와 같이 '유효하지만 빈 목록' 반복을 invalid 로 센다."""
    tasks = [{"id": "x1", "keywords": ["alpha-kw"], "project_hint": None, "category": "controls"}]
    gens = [{"valid": True, "items": []},
            {"valid": True, "items": [{"name": "alpha-kw 진행", "status": "open", "evidence_ids": [], "quote": ""}]},
            {"valid": True, "items": []}]
    assert R.keyless_states(gens, tasks) == {"x1": "invalid"}
