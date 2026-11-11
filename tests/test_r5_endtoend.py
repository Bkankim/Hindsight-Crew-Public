"""bench/r5_endtoend.py - 런 5 종단 확인(issue #366, 상위 #360 "S4·S3 구현").

가짜 입력만 쓴다(네트워크 0, recall 은 가짜 call, d-llm 은 가짜 run_setting 주입). 키·문장·기억은 전부 합성이다.
덮는 것: payload 표 저장, 표 렌더 조립(d-llm 프롬프트), 봉인 (단계, 팔) 1회 가드, 감사 줄 집계, 봉인 판정, 원래 증상 채점.
"""
from __future__ import annotations

import argparse
import json
import os
import stat

import pytest

from bench import d_llm as D
from bench import r5_endtoend as E
from bench import r5_metrics as R
from bench import r5_roles as X

T = "2026-10-01"
TS = R.query_timestamp(R._day(T))


def row(rid: str, day: str = T, text: str = "x") -> dict:
    return {"id": rid, "text": text, "type": "world", "mentioned_at": f"{day}T03:00:00+00:00", "tags": []}


def table(text: str, rows=(("app#1", "열림", T),)) -> dict:
    return {"t": T, "fmt": "a", "rows": [list(r) for r in rows], "truncated": 0, "text": text}


def fake_call(tables, rows_by_q, seen=None):
    """질의 순서대로 결과 행과 표(없으면 None)를 돌려준다. 받은 인자를 seen 에 쌓는다."""
    state = {"n": 0}

    def call(tool, args):
        assert tool == "recall"
        if seen is not None:
            seen.append(dict(args))
        i = state["n"]
        state["n"] += 1
        res = {"results": rows_by_q[i]}
        if tables[i] is not None:
            res[E.FIELD] = tables[i]
        return res
    return call


QUERIES = [{"qid": "F1", "scope": "family", "text": "목록"}, {"qid": "P:app", "scope": "project-status", "text": "상태"}]


# --- payload 표 저장 ----------------------------------------------------------------------------------

def test_recall_doc_saves_payload_table_with_rows():
    tab = table("<item_open_table>\nT1\n</item_open_table>")
    doc = E.recall_doc(fake_call([tab, tab], [[row("m1")], [row("m2")]]), "지시", QUERIES, TS, 16384)
    assert [r["open_table"] for r in doc["runs"]] == [tab, tab]
    assert doc["runs"][0]["results"] == [row("m1")]
    assert doc["runs"][0]["open_table_sha256"] == E.sha256_text(tab["text"])
    assert doc["totals"]["tables"] == 2 and doc["totals"]["errors"] == 0
    assert doc["started_utc"] <= doc["finished_utc"]


def test_recall_doc_without_table_and_errors_continue():
    def call(tool, args):
        raise RuntimeError("boom")
    doc = E.recall_doc(call, "지시", QUERIES, TS, 16384)
    assert doc["totals"] == {"queries": 2, "errors": 2, "rows": 0, "tables": 0, "seconds": doc["totals"]["seconds"]}
    assert all(r["open_table"] is None and r["error"] == "RuntimeError" for r in doc["runs"])


def test_recall_args_equal_save_recalls_and_no_tags():
    """인자는 d_llm.save_recalls 와 같다(tags 없음). 원래 증상 경로는 지시문 없이 문안 그대로."""
    mine, theirs = [], []
    E.recall_doc(fake_call([None, None], [[], []], mine), "지시 2026-10-01", QUERIES, TS, 16384)
    D.save_recalls(fake_call([None, None], [[], []], theirs), "지시 2026-10-01", QUERIES, 16384, TS)
    assert mine == theirs
    assert all("tags" not in a for a in mine)
    assert E.query_args(None, " 문안 ", E.SYMPTOM_TS, 16384) == {"query": "문안", "query_timestamp": E.SYMPTOM_TS,
                                                                  "max_tokens": 16384}


@pytest.mark.parametrize("texts,expect_texts,attached", [
    (["A", "A"], ["A"], 2),          # 같은 텍스트 1회
    (["A", "B"], ["A", "B"], 2),     # 질의 순
    (["B", None], ["B"], 1),         # 표 없는 질의
    ([None, None], [], 0),
])
def test_tables_of_query_order_same_text_once(texts, expect_texts, attached):
    tabs = [table(t) if t else None for t in texts]
    doc = E.recall_doc(fake_call(tabs, [[], []]), "지시", QUERIES, TS, 16384)
    got = E.tables_of(doc)
    assert got["texts"] == expect_texts and got["attached"] == attached
    assert got["sha256"] == (E.sha256_text("\n".join(expect_texts)) if expect_texts else None)


def test_open_keys_only_open_rows():
    tab = table("A", rows=(("app#1", "열림", T), ("app#2", "끝남", T)))
    doc = E.recall_doc(fake_call([tab, None], [[], []]), "지시", QUERIES, TS, 16384)
    assert E.open_keys(doc) == {"app#1"}


# --- 표 렌더 조립 ------------------------------------------------------------------------------------

def _doc(tabs):
    return E.recall_doc(fake_call(tabs, [[row("m1"), row("m9", day="2026-10-05")], [row("m2"), row("m1")]]),
                        "지시", QUERIES, TS, 16384)


def test_prompt_without_table_is_baseline_prompt():
    tpl = D.load_prompt(D.BASELINE_PROMPT)
    doc = _doc([None, None])
    b = E.build_input(D, tpl, doc, T, 200000)
    rows, _ = R.t_input(doc, T)
    asm = D.assemble({"runs": [{"q": 1, "max_tokens": 0, "results": rows}]}, max_input_chars=200000)
    assert b["prompt"] == D.build_prompt(tpl, asm, query_date=T)
    assert [r["id"] for r in b["asm"]["rows"]] == ["m1", "m2"]        # 미래 행 m9 는 시간여행 필터로 빠짐


def test_prompt_with_table_appends_payload_text_after_rows():
    tpl = D.load_prompt(D.BASELINE_PROMPT)
    text = "<item_open_table>\n머리\napp#1 | 열림\n</item_open_table>"
    doc = _doc([table(text), table(text)])
    b = E.build_input(D, tpl, doc, T, 200000)
    rendered = "\n".join(D.render_row(r) for r in b["asm"]["rows"])
    assert b["prompt"] == tpl.format(query_date=T, recall_rows=rendered + "\n" + text)
    assert b["prompt"].count(text) == 1


def test_check_gen_table_ties_head_to_payload_text():
    tpl = D.load_prompt(D.BASELINE_PROMPT)
    text = "<item_open_table>\nX\n</item_open_table>"
    doc = _doc([table(text), None])
    b = E.build_input(D, tpl, doc, T, 200000)
    gen = {"table_sha256": E.sha256_text(text), "prompt_text_sha256": E.sha256_text(b["prompt"])}
    E.check_gen_table(gen, doc, b["prompt"])
    with pytest.raises(E.Fail):
        E.check_gen_table(dict(gen, table_sha256=E.sha256_text("other")), doc, b["prompt"])
    with pytest.raises(E.Fail):
        E.check_gen_table(gen, doc, b["prompt"].replace(text, ""))
    off = _doc([None, None])
    pb = E.build_input(D, tpl, off, T, 200000)["prompt"]
    assert E.sha256_text(pb) != gen["prompt_text_sha256"]           # S3 끔과 켬의 프롬프트가 다르다


# --- 봉인 1회 가드 -----------------------------------------------------------------------------------

@pytest.mark.parametrize("step", E.SEAL_STEPS)
def test_seal_guard_each_stage_arm_once(tmp_path, step):
    g = E.SealGuard(str(tmp_path))
    g.claim(step)
    with pytest.raises(E.Fail, match="already ran once"):
        g.claim(step)
    others = [s for s in E.SEAL_STEPS if s != step]
    for s in others:                       # 다른 (단계, 팔)은 따로 1회
        E.SealGuard(str(tmp_path)).claim(s)
    assert stat.S_IMODE(os.stat(g.path).st_mode) == 0o600
    assert sorted(r["step"] for r in g.records()) == sorted(E.SEAL_STEPS)


def test_seal_guard_require_and_unknown(tmp_path):
    g = E.SealGuard(str(tmp_path))
    with pytest.raises(E.Fail, match="have not finished"):
        g.require("recall-on", "recall-off")
    g.claim("recall-on")
    with pytest.raises(E.Fail, match="recall-off"):
        g.require("recall-on", "recall-off")
    with pytest.raises(E.Fail):
        g.claim("judge-on")


def _write_recall(d, t, stage, arm, tabs):
    doc = dict(_doc(tabs), stage=stage, arm=arm, t=t)
    R.write_private(R.recall_path(str(d), t), doc)


@pytest.mark.parametrize("arm", E.ARMS)
def test_dllm_sealed_runs_once_per_arm_and_judge_reads_only(tmp_path, monkeypatch, arm):
    """봉인 d-llm 은 팔마다 1회(두 번째는 거부, LLM 0). 기록은 호출 전에 남는다. dev 는 가드 없음."""
    rec = tmp_path / "rec"
    tab = [table("<item_open_table>\nX\n</item_open_table>"), None] if arm == "on" else [None, None]
    _write_recall(rec, T, "sealed", arm, tab)
    calls = []
    monkeypatch.setattr(D, "claude_caller", lambda *a, **k: (lambda p: {}))

    def fake_run(jobs, caller, guard, k, write=None, **kw):
        calls.append(len(jobs))
        for j in jobs:
            write(j["out"], dict(j["head"], generations=[{"valid": True, "items": [], "calls": []}] * k,
                                 finished_kst="x"))
        return 0
    monkeypatch.setattr(X, "run_setting", fake_run)
    argv = ["dllm", "--stage", "sealed", "--arm", arm, "--recalls", str(rec), "--t", T, "--k", "7",
            "--gen-dir", str(tmp_path / "gen"), "--spent-dir", str(tmp_path), "--max-usd", "25", "--soft-usd", "20",
            "--work", str(tmp_path / "work")]
    assert E.main(argv) == 0
    assert calls == [1]
    os.remove(tmp_path / "gen" / f"sealed-{arm}-{T}-gen.json")      # 출력이 없어도 기록이 있으면 거부
    assert E.main(argv) == 1
    assert calls == [1]
    assert E.SealGuard(str(tmp_path / "work")).done(f"dllm-{arm}")


def test_dllm_refuses_arm_mismatch(tmp_path):
    rec = tmp_path / "rec"
    _write_recall(rec, T, "dev", "on", [None, None])          # 켬이라면서 표가 없음
    argv = ["dllm", "--stage", "dev", "--arm", "on", "--recalls", str(rec), "--t", T, "--gen-dir", str(tmp_path / "g"),
            "--spent-dir", str(tmp_path), "--max-usd", "25", "--soft-usd", "20", "--work", str(tmp_path / "w")]
    assert E.main(argv) == 1


def test_sealed_plan_uses_sealed_pairs_only():
    labelset = [{"key": "app#1", "t": "2026-10-22", "label": "positive", "split": "sealed"},
                {"key": "tool#2", "t": "2026-10-25", "label": "control", "split": "sealed"},
                {"key": "app#3", "t": "2026-10-01", "label": "positive", "split": "dev"},
                {"key": "app#4", "t": "2026-10-22", "label": "data_layer", "split": "sealed"}]
    fam = {"queries": [{"qid": "F1", "scope": "f", "text": "문안"}]}
    plan = E.sealed_plan(labelset, fam, f"기준 {R.REF_DATE}", "{project} 상태")
    assert sorted(plan["per_t"]) == ["2026-10-22", "2026-10-25"] and plan["stage"] == "sealed"
    assert [q["qid"] for q in plan["per_t"]["2026-10-22"]["queries"]] == ["F1", "P:app"]
    assert plan["per_t"]["2026-10-25"]["instruction"] == "기준 2026-10-25"
    assert plan["per_t"]["2026-10-25"]["query_timestamp"] == R.query_timestamp(R._day("2026-10-25"))


# --- 감사 줄 -----------------------------------------------------------------------------------------

def _audit_line(ts, reason, source="file", sha="s1", **extra):
    return json.dumps({"ts": ts, "decision": "item-open-table", "reason": reason,
                       "item_open_table": dict({"knobs_source": source, "knobs_sha256": sha}, **extra)})


def test_audit_counts_window_and_ok():
    lines = [_audit_line("2026-11-02T01:00:00Z", "attached"), _audit_line("2026-11-02T01:00:05Z", "attached"),
             _audit_line("2026-11-02T02:00:00Z", "attached"),                          # 구간 밖
             json.dumps({"ts": "2026-11-02T01:00:01Z", "decision": "allow", "reason": "ok"}), "not json"]
    c = E.audit_counts(lines, "2026-11-02T01:00:00Z", "2026-11-02T01:30:00Z")
    assert c["lines"] == 2 and c["reason"] == {"attached": 2}
    assert E.audit_ok(c, 2, "s1") and not E.audit_ok(c, 3, "s1") and not E.audit_ok(c, 2, "s2")


@pytest.mark.parametrize("bad", [{"reason": "bank-unset"}, {"source": "default"}, {"knob_warnings": ["x"]},
                                 {"aliases_mismatch": True}])
def test_audit_ok_rejects(bad):
    reason = bad.pop("reason", "attached")
    line = _audit_line("2026-11-02T01:00:00Z", reason, **bad)
    c = E.audit_counts([line], "2026-11-02T01:00:00Z", "2026-11-02T01:00:00Z")
    assert not E.audit_ok(c, 1, "s1")


# --- 봉인 판정·원래 증상 ------------------------------------------------------------------------------

@pytest.mark.parametrize("pb,pbd,on,off,rbd,expect", [
    (0.96, 40, 0.6, 0.5, 10, "met"),
    (0.94, 40, 0.6, 0.5, 10, "not-met"),
    (0.96, 40, 0.5, 0.5, 10, "not-met"),
    (0.96, 4, 0.6, 0.5, 10, "undecidable"),
    (0.96, 40, 0.6, 0.5, 4, "undecidable"),
])
def test_seal_verdict(pb, pbd, on, off, rbd, expect):
    assert E.seal_verdict(pb, pbd, on, off, rbd)["sealed"] == expect


def test_union_tasks_dedup_and_conflict():
    c = {"closed": [{"id": "C1", "item": "가 작업", "keywords": ["app#1"]},
                    {"id": "C2", "item": "나 작업", "keywords": ["app#2"]}],
         "controls": [{"id": "C3", "item": "다 작업", "keywords": ["다작업"]}]}
    d = {"closed": [{"id": "D1", "name": "가 작업", "keys": ["app#1"], "keywords": ["app#1"]}],
         "controls": [{"id": "D2", "name": "나 작업", "keys": ["app#2"], "keywords": ["app#2"]}]}
    u = E.union_tasks([c, d])
    ids = sorted(t["id"] for t in u["tasks"])
    assert ids == ["s0:C1", "s0:C3"]                 # C1=D1 같은 라벨 1개, C2/D2 라벨 충돌 둘 다 제외
    assert u["duplicates_same_label"] == 1 and u["excluded_conflicting"] == 2


def test_symptom_score_done_ratio():
    tasks = [{"id": "a", "keywords": ["app#1"], "project_hint": None, "category": "closed", "keys": ["app#1"]},
             {"id": "b", "keywords": ["app#2"], "project_hint": None, "category": "controls", "keys": ["app#2"]}]
    g1 = {"valid": True, "items": [{"name": "app#1 일", "status": "open"}, {"name": "app#2 일", "status": "open"},
                                   {"name": "다른 일", "status": "open"}, {"name": "app#1 끝", "status": "done"}]}
    g2 = {"valid": False, "items": []}
    s = E.symptom_score([g1, g1, g2], tasks)
    assert s["reps"][0]["open_items"] == 3 and s["reps"][0]["closed"] == 1 and s["reps"][0]["unmapped"] == 1
    assert s["done_ratio_mean"] == pytest.approx(1 / 3) and s["invalid"] == 1
    assert s["majority_open"]["controls"] == 1


# --- rework #366-1: 봉인 t 전체 요구(Codex P1), 성공 기록만 선행 조건(Codex P2) -----------------------

SEALED_TS = ("2026-10-22", "2026-10-25")


def _sealed_recalls(d):
    for t in SEALED_TS:
        _write_recall(d, t, "sealed", "on", [table("<item_open_table>\nX\n</item_open_table>"), None])


def _fake_dllm(monkeypatch, rc=0):
    calls = []
    monkeypatch.setattr(D, "claude_caller", lambda *a, **k: (lambda p: {}))

    def fake_run(jobs, caller, guard, k, write=None, **kw):
        calls.append(len(jobs))
        for j in jobs:
            write(j["out"], dict(j["head"], generations=[{"valid": True, "items": [], "calls": []}] * k,
                                 finished_kst="x"))
        return rc
    monkeypatch.setattr(X, "run_setting", fake_run)
    return calls


@pytest.mark.parametrize("ts", [(), ("2026-10-22",), ("2026-10-25",)])
def test_sealed_dllm_requires_all_sealed_t_before_record(tmp_path, monkeypatch, ts):
    """봉인 d-llm 에 --t 가 없거나 일부만이면 기록을 남기기 전에 거부한다(LLM 0, 기록 0)."""
    rec = tmp_path / "rec"
    _sealed_recalls(rec)
    calls = _fake_dllm(monkeypatch)
    argv = ["dllm", "--stage", "sealed", "--arm", "on", "--recalls", str(rec), "--k", "7",
            "--gen-dir", str(tmp_path / "gen"), "--spent-dir", str(tmp_path), "--max-usd", "25", "--soft-usd", "20",
            "--work", str(tmp_path / "work")]
    for t in ts:
        argv += ["--t", t]
    assert E.main(argv) == 1
    assert calls == []
    assert E.SealGuard(str(tmp_path / "work")).records() == []


def test_sealed_dllm_all_t_runs_and_marks_success(tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    _sealed_recalls(rec)
    calls = _fake_dllm(monkeypatch)
    argv = ["dllm", "--stage", "sealed", "--arm", "on", "--recalls", str(rec), "--t", SEALED_TS[1], "--t", SEALED_TS[0],
            "--k", "7", "--gen-dir", str(tmp_path / "gen"), "--spent-dir", str(tmp_path), "--max-usd", "25",
            "--soft-usd", "20", "--work", str(tmp_path / "work")]
    assert E.main(argv) == 0 and calls == [2]
    g = E.SealGuard(str(tmp_path / "work"))
    assert [r.get("status") for r in g.records()] == ["started", "ok"]
    g.require("dllm-on")


def test_sealed_dllm_failed_run_does_not_satisfy_require(tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    _sealed_recalls(rec)
    _fake_dllm(monkeypatch, rc=3)
    argv = ["dllm", "--stage", "sealed", "--arm", "on", "--recalls", str(rec), "--t", SEALED_TS[0], "--t", SEALED_TS[1],
            "--k", "7", "--gen-dir", str(tmp_path / "gen"), "--spent-dir", str(tmp_path), "--max-usd", "25",
            "--soft-usd", "20", "--work", str(tmp_path / "work")]
    assert E.main(argv) == 3
    g = E.SealGuard(str(tmp_path / "work"))
    assert g.done("dllm-on")                      # 재실행은 여전히 거부
    with pytest.raises(E.Fail, match="dllm-on"):
        g.require("dllm-on")


@pytest.mark.parametrize("ts", [["2026-10-22"], ["2026-10-25"]])
def test_sealed_recall_partial_t_refused_before_record(tmp_path, ts, capsys):
    plan = {"stage": "sealed", "max_tokens": 16384,
            "per_t": {t: {"instruction": "지시", "query_timestamp": R.query_timestamp(R._day(t)), "queries": QUERIES}
                      for t in SEALED_TS}}
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan))
    argv = ["recall", "--stage", "sealed", "--arm", "on", "--plan", str(p), "--out-dir", str(tmp_path / "o"),
            "--work", str(tmp_path / "w")]
    for t in ts:
        argv += ["--t", t]
    assert E.main(argv) == 1
    assert "sealed" in capsys.readouterr().err
    assert E.SealGuard(str(tmp_path / "w")).records() == []


def test_seal_guard_require_counts_only_success(tmp_path):
    g = E.SealGuard(str(tmp_path))
    g.claim("recall-on")
    with pytest.raises(E.Fail, match="recall-on"):
        g.require("recall-on")
    g.succeed("recall-on")
    g.require("recall-on")
    with pytest.raises(E.Fail):
        g.succeed("recall-off")                   # 시작 기록 없는 성공 줄은 거부
