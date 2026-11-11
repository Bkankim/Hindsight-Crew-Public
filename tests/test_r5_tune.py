"""bench/r5_tune.py - 런 5 S3 손잡이 튜닝(issue #365, 상위 #360 "S4·S3 구현").

가짜 입력만 쓴다(네트워크 0, d-llm 은 가짜 generate 주입, 저장소는 임시 sqlite). 키·문장·기억은 전부 합성이다.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
from datetime import date, datetime, timedelta, timezone

import pytest

from bench import d_llm as D
from bench import r5_metrics as R
from bench import r5_roles as X
from bench import r5_tune as T
from gateway import item_open_table as OT

KST = timezone(timedelta(hours=9))
TD = "2026-10-01"
ALIASES = {"app": "app", "tool": "tool"}


def iso(day: str, hh: int = 12) -> str:
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, tzinfo=KST).astimezone(timezone.utc).isoformat()


def make_store(path, mentions, aliases_sha=None):
    """mentions = [(memory_id, key, role, day)]. 별칭 sha 기본값은 ALIASES 의 정규 sha(하네스 가드와 맞춤)."""
    aliases_sha = aliases_sha or OT.aliases_sha256(ALIASES)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);"
        "CREATE TABLE memories(memory_id TEXT PRIMARY KEY, document_id TEXT, mentioned_at TEXT, state TEXT NOT NULL,"
        " text_sha256 TEXT NOT NULL, judged_at TEXT NOT NULL);"
        "CREATE TABLE mention_roles(memory_id TEXT NOT NULL, key TEXT NOT NULL, role TEXT NOT NULL,"
        " PRIMARY KEY(memory_id, key));")
    for k, v in {"schema_version": "1", "model": "m", "prompt_sha256": "p" * 64, "aliases_sha256": aliases_sha,
                 "updated_at": "2026-11-01T00:00:00Z"}.items():
        conn.execute("INSERT INTO meta VALUES (?, ?)", (k, v))
    seen = set()
    for mid, key, role, day in mentions:
        if mid not in seen:
            conn.execute("INSERT INTO memories VALUES (?, ?, ?, 'valid', ?, ?)", (mid, "d-" + mid, iso(day), "s" * 64,
                                                                                   "2026-11-01T00:00:00Z"))
            seen.add(mid)
        conn.execute("INSERT INTO mention_roles VALUES (?, ?, ?)", (mid, key, role))
    conn.commit()
    conn.close()


QUERIES = [{"qid": "F1", "scope": None, "text": "남은 일 목록"}, {"qid": "F2", "scope": None, "text": "요약해 줘"},
           {"qid": "P:app", "scope": "project-status", "text": "app 프로젝트 상황"}]
ROWS = [{"id": "m1", "text": "app#1 진행 중", "mentioned_at": iso("2026-09-20"), "tags": ["project:app"],
         "fact_type": "world"},
        {"id": "m2", "text": "app#2 머지 완료", "mentioned_at": iso("2026-09-21"), "tags": ["kind:completion"],
         "fact_type": "world"}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "tiktoken_counter", lambda: (lambda s: len(s or "")))
    d = tmp_path
    (d / "aliases.json").write_text(json.dumps(ALIASES))
    lab = [{"key": "app#1", "t": TD, "label": "control", "split": "dev", "fold": "r01"},
           {"key": "app#2", "t": TD, "label": "positive", "split": "dev", "fold": "r01"}]
    (d / "labelset.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lab))
    plan = {"max_tokens": 16384, "per_t": {TD: {"instruction": f"기준일 {TD} 지시", "queries": QUERIES,
                                               "query_timestamp": R.query_timestamp(date.fromisoformat(TD))}}}
    (d / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
    rec = d / "rep1"
    rec.mkdir()
    doc = {"plan_sha256": X.sha256_file(str(d / "plan.json")), "query_timestamp": plan["per_t"][TD]["query_timestamp"],
           "max_tokens": 16384, "totals": {"errors": 0},
           "runs": [{"q": i + 1, "qid": q["qid"], "max_tokens": 16384, "results": ROWS if i == 0 else []}
                    for i, q in enumerate(QUERIES)]}
    (rec / f"t-{TD}.json").write_text(json.dumps(doc, ensure_ascii=False))
    make_store(str(d / "store.sqlite"), [("m1", "app#1", "open", "2026-09-20"), ("m2", "app#2", "done", "2026-09-21"),
                                         ("m3", "tool#5", "open", "2026-09-22"),
                                         ("m9", "app#7", "open", "2026-10-20")])     # 기준 시각 뒤 = 표 밖
    (d / "header.txt").write_text("기준일 {t} 열린 작업 전체.\n줄: {cols}\n")
    args = argparse.Namespace(labelset=str(d / "labelset.jsonl"), aliases=str(d / "aliases.json"), policy=None,
                              plan=str(d / "plan.json"), recalls=str(rec), store=str(d / "store.sqlite"), t=[TD],
                              c3_header_file=str(d / "header.txt"), max_input_chars=200000)
    return d, args


def fake_generate(calls):
    def gen(prompt, caller):
        calls.append(prompt)
        return {"valid": True, "attempts": 1, "error": None, "items": [],
                "calls": [{"cost_usd": 0.5, "input_tokens": 10, "output_tokens": 2, "duration_ms": 5, "valid": True}]}
    return gen


def run(ctx, d, combo="C1", rep=1, calls=None, **kw):
    calls = [] if calls is None else calls
    opts = dict(gen_dir=str(d / "w" / "gen"), spent_dir=str(d / "w"), ledger=str(d / "ledger.jsonl"), max_usd=30.0,
                soft_usd=25.0, k=3)
    opts.update(kw)
    return T.run_rep(ctx, combo, rep, caller=lambda p: {}, generate=fake_generate(calls), now=lambda: "NOW", **opts)


# --- 질의 인자 재구성(O7) ------------------------------------------------------------------------------

def test_query_args_equal_to_save_recalls_args(env):
    d, args = env
    spec = json.loads((d / "plan.json").read_text())["per_t"][TD]
    sent = []
    D.save_recalls(lambda tool, a: (sent.append((tool, a)) or {"results": []}), spec["instruction"], spec["queries"],
                   16384, spec["query_timestamp"])
    assert [a for _, a in sent] == [T.query_args(spec, q, 16384) for q in spec["queries"]]
    assert all(tool == "recall" and "tags" not in a for tool, a in sent)          # 태그 없음


def test_recall_doc_must_match_plan(env):
    d, args = env
    spec = json.loads((d / "plan.json").read_text())["per_t"][TD]
    doc = json.loads((d / "rep1" / f"t-{TD}.json").read_text())
    sha = X.sha256_file(args.plan)
    T.check_recall_doc(doc, spec, sha, 16384)
    for bad in ({"plan_sha256": "x"}, {"query_timestamp": "2026-10-02T00:00:00Z"},
                {"max_tokens": 4096},
                {"runs": [dict(r, qid=q["qid"]) for r, q in zip(doc["runs"], reversed(QUERIES))]}):
        with pytest.raises(T.Fail):
            T.check_recall_doc(dict(doc, **bad), spec, sha, 16384)


# --- 질의별 표 조립 ------------------------------------------------------------------------------------

@pytest.mark.parametrize("attach, texts, attached", [
    ("always", ["남은 일 목록", "요약해 줘", "상황"], 3),
    ("status_query", ["남은 일 목록", "요약해 줘", "상황"], 1),     # '남은' 만 status_terms
    ("status_query", ["요약해 줘", "개요", "상황"], 0),
])
@pytest.mark.parametrize("scope", ["request_project", "all"])
def test_t_tables_attach_scope_and_same_text_once(env, attach, texts, attached, scope):
    d, args = env
    store = OT.read_store(args.store, 5)
    spec = {"instruction": "지시", "query_timestamp": R.query_timestamp(date.fromisoformat(TD)),
            "queries": [{"qid": f"Q{i}", "text": x} for i, x in enumerate(texts)]}
    kn = T.knobs_of(dict(T.combo_raw("C1", None), attach=attach, scope=scope))
    tab = T.t_tables(store.mentions, spec, 16384, kn, ALIASES)
    assert tab["attached_queries"] == attached
    assert len(tab["texts"]) == (1 if attached else 0)                          # 같은 표 텍스트 1회
    if attached:
        keys = [r[0] for r in tab["objs"][0]["rows"]]
        assert keys == ["app#1", "tool#5"]                    # 태그 없음 = 뱅크 전체 범위, 끝남·기준 시각 뒤 제외
        assert tab["text"] == tab["objs"][0]["text"]
    else:
        assert tab["text"] == ""


def test_no_table_prompt_is_byte_identical_to_baseline(env):
    d, args = env
    ctx = T.Ctx(args)
    kn = T.knobs_of(dict(T.combo_raw("C1", None), attach="status_query", status_terms=["없는낱말"]))
    b = ctx.build(TD, kn)
    asm, _st, _tok = ctx.rows(TD)
    assert b["prompt"] == D.build_prompt(ctx.template, asm, query_date=TD)
    with_table = ctx.build(TD, ctx.knobs("C1"))
    assert with_table["prompt"].startswith(b["prompt"].split("\n")[0])
    assert OT.OPEN_TAG in with_table["prompt"] and with_table["stats"]["rows"] == 2


@pytest.mark.parametrize("combo, fmt, header_default", [("C1", "a", True), ("C2", "b", True), ("C3", "a", False)])
def test_combo_knobs_fixed_space(env, combo, fmt, header_default):
    d, args = env
    ctx = T.Ctx(args)
    kn = ctx.knobs(combo)
    assert kn["table_fmt"] == fmt and kn["attach"] == "always" and kn["recent_done_days"] == 0
    assert kn["max_rows"] == 300 and kn["scope"] == "request_project" and kn["timeout_ms"] == 1500
    # #391: C2는 머리를 지정하지 않아 게이트웨이 기본 머리를 받는다. 런 5 C2 기록값은 당시 기본(= HEADER_R6_1 = C1_HEADER)으로
    # 잰 값이다. header_default = C1_HEADER 또는 게이트웨이 기본 머리(덮어쓰기 없음).
    expected = {"C1": T.C1_HEADER, "C2": OT.HEADER_DEFAULT}.get(combo)
    assert (kn["header"] == expected) is header_default
    assert kn["header"] not in (T.C1_HEADER, OT.HEADER_DEFAULT) or header_default
    c4 = ctx.knobs("C4", kn)
    assert c4["attach"] == "status_query" and c4["table_fmt"] == fmt and c4["header"] == kn["header"]


def test_c1_knobs_sha_equals_365_default_audit_sha():
    # C1 은 #365 기본 머리 문안을 명시 값으로 고정한다(게이트웨이 기본값이 바뀌어도 #365 C1 이 재현된다, #381)
    assert T.sha256_text(T.C1_HEADER) == "82149001b5936b923e378761c001d5f1463330810beafc5b301108505825f3cb"
    assert T.combo_raw("C1", None)["header"] == T.C1_HEADER
    # 런 5 C1 정규 sha f7fcf626... 는 산출물 기록값이다. 재계산 값은 R6-1(#387) 기본 어휘(status_terms) 반영.
    assert T.knobs_sha256(T.knobs_of(T.combo_raw("C1", None))) == \
        "dfcc684d75bd3457c373169077742293897049003887517f31848bb3fc0990a1"


def test_knobs_of_rejects_invalid_values():
    with pytest.raises(T.Fail):
        T.knobs_of({"table_fmt": "z"})
    with pytest.raises(T.Fail):
        T.combo_raw("C3", None)
    with pytest.raises(T.Fail):
        T.combo_raw("C4", None, None)


# --- sha 불일치 정지 -----------------------------------------------------------------------------------

def test_sha_mismatch_stops_before_any_call(env):
    d, args = env
    ctx = T.Ctx(args)
    calls = []
    assert run(ctx, d, rep=1, calls=calls) == 0
    n = len(calls)
    with open(args.labelset, "a") as f:                       # 라벨셋이 바뀌었다
        f.write(json.dumps({"key": "app#3", "t": TD, "label": "control", "split": "dev"}) + "\n")
    assert run(ctx, d, rep=2, calls=calls) == 5
    assert len(calls) == n
    lines = T.ledger_lines(str(d / "ledger.jsonl"))
    assert lines[-1]["event"] == "sha-stop" and "labelset" in lines[-1]["detail"]


def test_sha_guard_ignores_optional_keys_absent_at_start(env):
    d, args = env
    args.c3_header_file = None
    assert run(T.Ctx(args), d, combo="C1", rep=1) == 0          # start 줄에 c3_header 없음
    args.c3_header_file = str(d / "header.txt")
    assert run(T.Ctx(args), d, combo="C3", rep=1) == 0          # 파일 변경 없음 -> 멈추지 않는다


def test_store_alias_sha_mismatch_refused(env):
    d, args = env
    other = d / "store2.sqlite"
    make_store(str(other), [("m1", "app#1", "open", "2026-09-20")], aliases_sha="b" * 64)
    args.store = str(other)
    with pytest.raises(T.Fail):
        T.Ctx(args)


def test_sha_checked_before_each_dllm_call(env):
    d, args = env
    ctx = T.Ctx(args)
    calls = []

    def gen(prompt, caller):
        calls.append(prompt)
        with open(args.store, "ab") as f:                   # 첫 호출 중 저장소가 바뀐다
            f.write(b"x")
        return fake_generate([])(prompt, caller)
    rc = T.run_rep(ctx, "C1", 1, gen_dir=str(d / "w" / "gen"), spent_dir=str(d / "w"), ledger=str(d / "ledger.jsonl"),
                   max_usd=30.0, soft_usd=25.0, k=3, caller=lambda p: {}, generate=gen, now=lambda: "NOW")
    assert rc == 5 and len(calls) == 1


def test_sha_guard_paths_cover_harness_judge_code_labelset_store_gateway(env):
    d, args = env
    names = set(T.Ctx(args).guard_paths())
    for f in ("bench/r5_tune.py", "bench/r5_metrics.py", "bench/r5_labels.py", "bench/r5_oracle.py",
              "bench/r5_roles.py", "gateway/item_open_table.py", "labelset", "store"):
        assert f in names


# --- 원장 append-only ----------------------------------------------------------------------------------

def test_ledger_append_only_keeps_existing_lines(env):
    d, args = env
    ctx = T.Ctx(args)
    led = d / "ledger.jsonl"
    T.ledger_append(str(led), {"event": "note", "n": 1})
    first = led.read_bytes()
    assert run(ctx, d, rep=1) == 0
    after1 = led.read_bytes()
    assert after1.startswith(first)
    assert run(ctx, d, combo="C2", rep=1) == 0
    assert led.read_bytes().startswith(after1)
    assert stat.S_IMODE(os.stat(led).st_mode) == 0o600
    events = [r["event"] for r in T.ledger_lines(str(led))]
    assert events == ["note", "start", "run", "run"]
    runs = [r for r in T.ledger_lines(str(led)) if r["event"] == "run"]
    assert runs[0]["calls"] == 3 and runs[0]["cost_usd"] == pytest.approx(1.5)
    assert runs[0]["knobs_sha256"] != runs[1]["knobs_sha256"]
    assert set(runs[0]["sha256"]) >= {"labelset", "store", "bench/r5_tune.py"}


def test_run_writes_private_gen_with_head_and_skips_complete(env):
    d, args = env
    ctx = T.Ctx(args)
    calls = []
    assert run(ctx, d, rep=1, calls=calls) == 0
    g = X.read_json(str(d / "w" / "gen" / f"C1-r1-{TD}-gen.json"))
    assert len(g["generations"]) == 3 and g["finished_kst"] == "NOW"
    assert g["knobs_sha256"] == T.knobs_sha256(g["knobs"]) and g["prompt_text_sha256"] == T.sha256_text(calls[0])
    assert g["table"]["rows"] == 2 and g["table"]["attached_queries"] == 3 and g["table"]["tables"] == 1
    assert stat.S_IMODE(os.stat(d / "w" / "gen" / f"C1-r1-{TD}-gen.json").st_mode) == 0o600
    assert run(ctx, d, rep=1, calls=calls) == 0 and len(calls) == 3          # 완료 출력은 건너뛴다
    args.max_input_chars = len(D.render_row(ROWS[0])) + 1                    # 같은 라벨, 다른 입력 정의(첫 행만)
    assert run(T.Ctx(args), d, rep=1, calls=calls) == 1 and len(calls) == 3


# --- 지출 상한 -----------------------------------------------------------------------------------------

def _spent(d, usd):
    p = d / "w" / "gen" / "old-2026-09-01-gen.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"generations": [{"calls": [{"cost_usd": usd}]}]}))


def test_soft_cap_stops_before_rep(env):
    d, args = env
    _spent(d, 25.5)
    calls = []
    assert run(T.Ctx(args), d, calls=calls) == 4 and calls == []
    assert T.ledger_lines(str(d / "ledger.jsonl"))[-1]["event"] == "soft-stop"


def test_hard_cap_preflight_stops(env):
    d, args = env
    _spent(d, 29.0)                                       # 29 + 3 호출 x 평균 29 > 30
    calls = []
    assert run(T.Ctx(args), d, calls=calls, soft_usd=100.0) == 3 and calls == []


def test_hard_cap_rechecked_between_calls(env):
    d, args = env
    _spent(d, 0.1)
    calls = []

    def gen(prompt, caller):          # 호출 하나가 상한을 넘긴다(누계는 spent-dir 의 gen 파일에서 다시 읽는다)
        calls.append(prompt)
        return {"valid": True, "attempts": 1, "error": None, "items": [],
                "calls": [{"cost_usd": 40.0, "valid": True}]}
    rc = T.run_rep(T.Ctx(args), "C1", 1, gen_dir=str(d / "w" / "gen"), spent_dir=str(d / "w"),
                   ledger=str(d / "ledger.jsonl"), max_usd=30.0, soft_usd=25.0, k=3, caller=lambda p: {},
                   generate=gen, now=lambda: "NOW")
    assert rc == 3 and len(calls) == 1


# --- C4 동일 입력 생략 ---------------------------------------------------------------------------------

def test_c4_skipped_when_inputs_identical(env):
    d, args = env
    calls = []
    assert run(T.Ctx(args), d, combo="C4", c4_base="C1", calls=calls) == 0     # 'F1' 에 '남은' -> 같은 표
    assert calls == []
    last = T.ledger_lines(str(d / "ledger.jsonl"))[-1]
    assert last["event"] == "skip" and last["reason"] == "identical-input"


def test_c4_runs_when_inputs_differ(env):
    d, args = env
    plan = json.loads((d / "plan.json").read_text())
    for q in plan["per_t"][TD]["queries"]:
        q["text"] = "요약"
    (d / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
    doc = json.loads((d / "rep1" / f"t-{TD}.json").read_text())
    doc["plan_sha256"] = X.sha256_file(args.plan)
    (d / "rep1" / f"t-{TD}.json").write_text(json.dumps(doc, ensure_ascii=False))
    ctx = T.Ctx(args)
    assert T.same_inputs(ctx, ctx.knobs("C4", ctx.knobs("C1")), ctx.knobs("C1")) == {TD: False}
    calls = []
    assert run(ctx, d, combo="C4", c4_base="C1", calls=calls) == 0 and len(calls) == 3
    assert OT.OPEN_TAG not in calls[0]


def test_c4_needs_base(env):
    d, args = env
    with pytest.raises(T.Fail):
        run(T.Ctx(args), d, combo="C4", c4_base=None)


# --- 최선 선택 -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("combos, best, verdict", [
    # P_bank 문턱(두 반복 모두) 통과 중 R_bank 평균 최대
    ({"C1": {"P_bank": [0.96, 0.97], "R_bank": [0.60, 0.62], "tokens": 0.02},
      "C2": {"P_bank": [0.99, 0.94], "R_bank": [0.90, 0.90], "tokens": 0.03},
      "C3": {"P_bank": [0.95, 1.00], "R_bank": [0.70, 0.66], "tokens": 0.02}}, "C3", "eligible"),
    # R_bank 같으면 토큰 최소
    ({"C1": {"P_bank": [1.0, 1.0], "R_bank": [0.7, 0.7], "tokens": 0.03},
      "C2": {"P_bank": [1.0, 1.0], "R_bank": [0.7, 0.7], "tokens": 0.02}}, "C2", "eligible"),
    # 토큰 10% 초과는 자격 밖
    ({"C1": {"P_bank": [1.0, 1.0], "R_bank": [0.9, 0.9], "tokens": 0.11},
      "C2": {"P_bank": [1.0, 1.0], "R_bank": [0.5, 0.5], "tokens": 0.05}}, "C2", "eligible"),
    # 자격 조합 없음 -> P_bank(두 반복 최솟값) 최대
    ({"C1": {"P_bank": [0.90, 0.93], "R_bank": [0.9, 0.9], "tokens": 0.02},
      "C2": {"P_bank": [0.94, 0.92], "R_bank": [0.5, 0.5], "tokens": 0.02}}, "C2", "no-eligible"),
    ({"C1": {"P_bank": [None, 0.99], "R_bank": [0.9, 0.9], "tokens": 0.02},
      "C2": {"P_bank": [0.95, 0.95], "R_bank": [0.1, 0.1], "tokens": 0.02}}, "C2", "eligible"),
    # 반복이 1개뿐이면 자격 밖(두 반복 모두)
    ({"C1": {"P_bank": [0.99], "R_bank": [0.9], "tokens": 0.02},
      "C2": {"P_bank": [0.96, 0.97], "R_bank": [0.5, 0.5], "tokens": 0.02}}, "C2", "eligible"),
])
def test_select_best_rule(combos, best, verdict):
    got = T.select_best(combos)
    assert got["best"] == best and got["verdict"] == verdict


def test_check_combo_gens_rejects_other_combo_labels(env):
    d, args = env
    ctx = T.Ctx(args)
    assert run(ctx, d, combo="C1", rep=1) == 0 and run(ctx, d, combo="C2", rep=1) == 0
    gd = str(d / "w" / "gen")
    assert T.check_combo_gens(gd, "C1", ["C1-r1"], [TD]) == T.knobs_sha256(ctx.knobs("C1"))
    with pytest.raises(T.Fail):
        T.check_combo_gens(gd, "C2", ["C1-r1", "C2-r1"], [TD])


def test_parse_runs_rejects_duplicate_labels():
    with pytest.raises(T.Fail):
        T.parse_runs(["C1=C1-r1,C1-r1"])
    assert T.parse_runs(["C1=C1-r1,C1-r2"]) == {"C1": ["C1-r1", "C1-r2"]}


# --- 사유 재분류(S4 상태) ------------------------------------------------------------------------------

def test_reclassify_reuses_reading_and_uses_s4_state():
    code = X.key_code("app#1")
    ev = {"has_side_mention": True, "reader_state": X.S_OPEN, "llm_state": X.S_OPEN}
    base = [{"setting": "a", "t": TD, "key_code": code, "side": "P", "reason": "iii", "evidence": ev},
            {"setting": "b", "t": TD, "key_code": code, "side": "P", "reason": "iii", "evidence": ev},   # 같은 쌍 1회
            {"setting": "a", "t": TD, "key_code": X.key_code("app#2"), "side": "R", "reason": "ii",
             "evidence": {"has_side_mention": False, "reader_state": X.S_UNDET, "llm_state": None}}]
    s4 = {("app#1", TD): X.S_DONE, ("app#2", TD): X.S_OPEN}
    items, changed = T.reclassify(base, lambda k, t: s4[(k, t)], {X.key_code(k): k for k in ("app#1", "app#2")})
    assert len(items) == 2 and changed == 1
    assert items[0]["reason"] == "i" and items[0]["reason_374"] == "iii"       # S4 상태가 판독과 다르면 (i)
    assert items[0]["evidence"]["llm_state"] == X.S_DONE and items[0]["evidence"]["llm_state_374"] == X.S_OPEN
    assert items[1]["reason"] == "ii"
    new = T.new_read_items([{"t": TD, "key_code": X.key_code("app#2"), "side": "R",
                             "evidence": {"has_side_mention": True, "reader_state": X.S_OPEN}}],
                           lambda k, t: s4[(k, t)], {X.key_code("app#2"): "app#2"})
    assert new[0]["reason"] == "iv"


def test_best_writes_knobs_file_with_canonical_sha(env, tmp_path):
    d, args = env
    ctx = T.Ctx(args)
    assert run(ctx, d, combo="C3", rep=1) == 0
    judge = {"t": [TD], "selection": {"best": "C3", "verdict": "eligible"},
             "combos": {"C3": {"reps": [{"label": "C3-r1"}]}}}
    (tmp_path / "judge.json").write_text(json.dumps(judge))
    out = tmp_path / "knobs.json"
    assert T.main(["best", "--judge", str(tmp_path / "judge.json"), "--gen-dir", str(d / "w" / "gen"),
                   "--out", str(out)]) == 0
    values, warnings = OT.parse_knobs(out.read_text())
    assert warnings == [] and values == ctx.knobs("C3")
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


# --- C5·C6 머리 문안(#381) -----------------------------------------------------------------------------

C1_FIRST = T.C1_HEADER.split("\n")[0]
SYN = {"C5": C1_FIRST + " 합성 지시 하나.\n줄: {cols}",
       "C6": C1_FIRST + " 합성 지시 하나. 합성 지시 둘.\n줄: {cols}"}


@pytest.fixture
def headers(env, monkeypatch):
    """합성 C5·C6 문안 파일(끝 줄바꿈 1개)과 그 sha 를 기대 상수로 둔 하네스(CI 에 리포 밖 파일 없음)."""
    d, args = env
    for c, text in SYN.items():
        (d / f"{c}.txt").write_text(text + "\n")
    monkeypatch.setattr(T, "EXPECTED_HEADER_SHA256", {c: T.sha256_text(x) for c, x in SYN.items()})
    args.c5_header_file, args.c6_header_file = str(d / "C5.txt"), str(d / "C6.txt")
    return d, args


@pytest.mark.parametrize("combo", ["C5", "C6"])
def test_c5_c6_registered_with_c1_knobs_but_header(headers, combo):
    d, args = headers
    ctx = T.Ctx(args)
    kn, c1 = ctx.knobs(combo), ctx.knobs("C1")
    assert kn["header"] == SYN[combo]
    assert {k: v for k, v in kn.items() if k != "header"} == {k: v for k, v in c1.items() if k != "header"}
    assert T.knobs_sha256(kn) != T.knobs_sha256(c1)
    assert combo in T.parse_runs([f"{combo}={combo}-r1,{combo}-r2"])
    assert f"{combo.lower()}_header" in ctx.guard_paths()
    b = ctx.build(TD, kn)
    assert b["prompt"].count(OT.OPEN_TAG) == 1 and b["stats"]["rows"] == 2
    assert run(ctx, d, combo=combo, rep=1) == 0
    gen = json.loads((d / "w" / "gen" / f"{combo}-r1-{TD}-gen.json").read_text())
    assert gen["combo"] == combo and gen["knobs"]["header"] == SYN[combo]


@pytest.mark.parametrize("combo", ["C5", "C6"])
def test_header_file_other_than_expected_sha_refused(headers, combo):
    d, args = headers
    (d / f"{combo}.txt").write_text(SYN[combo] + " 바뀜\n")
    with pytest.raises(T.Fail, match="sha256"):
        T.Ctx(args)


@pytest.mark.parametrize("combo", ["C5", "C6"])
def test_combo_without_header_file_refused(env, combo):
    with pytest.raises(T.Fail):
        T.combo_raw(combo, None)
    d, args = env
    with pytest.raises(T.Fail):
        T.Ctx(args).knobs(combo)


@pytest.mark.parametrize("combo, text", [
    ("C5", "다른 머리 {t}. 지시.\n줄: {cols}"),                       # C1 머리 문장을 그대로 두지 않음
    ("C5", C1_FIRST + "\n줄: {cols}"),                                 # 덧붙인 지시 없음
    ("C5", C1_FIRST + " 지시.\n줄 {cols}"),                            # 마지막 줄이 `줄: {cols}` 아님
    ("C5", C1_FIRST + " 지시.\n둘째 줄.\n줄: {cols}"),                 # 줄 수
    ("C6", C1_FIRST + " 다른 지시. 합성 지시 둘.\n줄: {cols}"),         # C5 를 그대로 두지 않음
    ("C6", SYN["C5"]),                                                 # C5 + 지시가 아님
    ("C5", C1_FIRST + " 지시 하나. 지시 둘.\n줄: {cols}"),               # 지시 2문장
    ("C5", C1_FIRST + " 마침표 없는 지시\n줄: {cols}"),                  # 문장으로 끝나지 않음
    ("C6", C1_FIRST + " 합성 지시 하나. 합성 지시 둘. 셋.\n줄: {cols}"), # C5 + 2문장
])
def test_header_structure_refused(headers, monkeypatch, combo, text):
    d, args = headers
    if combo == "C5":                    # C5 경우는 C5 만 본다(C6 파일 검사로 거부되는 다른 이유를 빼려고)
        args.c6_header_file = None
    (d / f"{combo}.txt").write_text(text + "\n")
    monkeypatch.setitem(T.EXPECTED_HEADER_SHA256, combo, T.sha256_text(text))
    with pytest.raises(T.Fail, match="structure"):
        T.Ctx(args)


def test_expected_header_shas_are_fixed_constants():
    # 산출물 r5-c2-header.md 2절에 적은 값 그대로(상수를 바꾸면 여기서 실패한다)
    assert T.EXPECTED_HEADER_SHA256 == {
        "C5": "9a3f9342ab6dd602068ece7a0c3c4fc33080bc0b566d2ca1f81b2587d4c45a09",
        "C6": "a950bab3820a329d966e3f18923546f339c88cda34c9292bdd1ea8017b9ab0f1"}


# --- 1단계: C3 P 오답의 표 안/밖(#381, 지출 0) -----------------------------------------------------------

def test_outside_split_counts_pairs_keys_and_pbank_pairs():
    code = X.key_code
    errs = [("app#1", "t1"), ("app#1", "t2"), ("app#2", "t1"), ("app#3", "t1"), ("app#3", "t2"), ("app#4", "t2")]
    open_by_t = {"t1": {"app#1", "app#3"}, "t2": {"app#4"}}
    reasons = {("t1", code("app#1"), "P"): "i", ("t2", code("app#1"), "P"): "iv", ("t1", code("app#2"), "P"): "ii",
               ("t1", code("app#3"), "P"): "iii", ("t2", code("app#3"), "P"): "iv", ("t2", code("app#4"), "P"): "i"}
    got = T.outside_split(errs, open_by_t, reasons)
    assert got["all"]["pairs"] == 6 and got["all"]["pairs_in"] == 3 and got["all"]["pairs_out"] == 3
    assert got["all"]["pair_out_ratio"] == 0.5
    # 키: app#2 밖만, app#4 안만, app#1·app#3 섞임
    assert (got["all"]["keys"], got["all"]["keys_in"], got["all"]["keys_out"], got["all"]["keys_mixed"]) == (4, 1, 1, 2)
    assert got["all"]["key_out_ratio"] == 0.25
    # P_bank 분자에 남는 (i)·(iv): app#1 t1 안(i), app#1 t2 밖(iv), app#3 t2 밖(iv), app#4 t2 안(i)
    pb = got["pbank"]
    assert (pb["pairs"], pb["pairs_in"], pb["pairs_out"]) == (4, 2, 2)
    assert (pb["keys"], pb["keys_in"], pb["keys_out"], pb["keys_mixed"]) == (3, 1, 1, 1)
    assert got["by_reason"]["iii"] == {"in": 1, "out": 0} and got["by_reason"]["ii"] == {"in": 0, "out": 1}


def test_outside_split_refuses_unreasoned_error():
    with pytest.raises(T.Fail):
        T.outside_split([("app#1", "t1")], {"t1": set()}, {})


def test_outside_split_empty_ratios_are_none():
    got = T.outside_split([], {}, {})
    assert got["all"]["pairs"] == 0 and got["all"]["pair_out_ratio"] is None and got["all"]["key_out_ratio"] is None


# --- C1 참고값과 비교·선택(#381) ------------------------------------------------------------------------

REF = {"P_bank": [0.972, 0.951], "R_bank": [0.603, 0.672], "tokens": 0.0216}


@pytest.mark.parametrize("cands, best", [
    ({"C5": {"P_bank": [0.96, 0.97], "R_bank": [0.70, 0.70], "tokens": 0.03}}, "C5"),        # 자격 + 엄격히 높음
    ({"C5": {"P_bank": [0.96, 0.97], "R_bank": [0.603, 0.672], "tokens": 0.01}}, "C1"),      # 동률이면 C1(토큰이 적어도)
    ({"C5": {"P_bank": [0.94, 0.99], "R_bank": [0.90, 0.90], "tokens": 0.02}}, "C1"),        # P_bank 한 반복 미달
    ({"C5": {"P_bank": [0.99, 0.99], "R_bank": [0.90, 0.90], "tokens": 0.11}}, "C1"),        # 토큰 초과
    ({"C5": {"P_bank": [0.96, 0.96], "R_bank": [0.70, 0.70], "tokens": 0.02},
      "C6": {"P_bank": [0.96, 0.96], "R_bank": [0.80, 0.80], "tokens": 0.02}}, "C6"),        # 자격 중 최대
    ({"C5": {"P_bank": [0.96, 0.96], "R_bank": [0.60, 0.60], "tokens": 0.02},
      "C6": {"P_bank": [0.90, 0.96], "R_bank": [0.95, 0.95], "tokens": 0.02}}, "C1"),        # 높은 쪽은 자격 밖, 자격은 낮음
])
def test_select_vs_ref_rule(cands, best):
    got = T.select_vs_ref(dict(cands, C1=REF), "C1")
    assert got["best"] == best and got["replace"] is (best != "C1")


def test_load_ref_checks_t_store_and_knobs(tmp_path):
    kn = T.knobs_sha256(T.knobs_of(T.combo_raw("C1", None)))
    rep = {"P_bank": 0.97, "R_bank_s4": {"R_bank": 0.6}, "tokens": {"ratio_max": 0.02}, "knobs_sha256": kn}
    doc = {"t": [TD], "store_sha256": "s" * 64, "combos": {"C1": {"reps": [dict(rep, label="C1-r1"),
                                                                           dict(rep, label="C1-r2", P_bank=0.95)]}}}
    p = tmp_path / "judge.json"
    p.write_text(json.dumps(doc))
    got = T.load_ref(str(p), "C1", [TD], "s" * 64, kn)
    assert got["P_bank"] == [0.97, 0.95] and got["R_bank"] == [0.6, 0.6] and got["tokens"] == 0.02
    for bad in (([TD, "2026-10-04"], "s" * 64, kn), ([TD], "x" * 64, kn), ([TD], "s" * 64, "k" * 64)):
        with pytest.raises(T.Fail):
            T.load_ref(str(p), "C1", *bad)
    pairs = {"positive": 1, "control": 1, "present_control": 1}
    p.write_text(json.dumps(dict(doc, pairs=pairs)))
    assert T.load_ref(str(p), "C1", [TD], "s" * 64, kn, pairs)["P_bank"] == [0.97, 0.95]
    with pytest.raises(T.Fail):                                   # 다른 라벨셋 쌍 수
        T.load_ref(str(p), "C1", [TD], "s" * 64, kn, dict(pairs, control=2))
    for combos in ({}, {"C1": {"reps": []}}, {"C1": {"reps": doc["combos"]["C1"]["reps"][:1]}}):
        p.write_text(json.dumps(dict(doc, combos=combos)))
        with pytest.raises(T.Fail):                               # 조합 없음·반복 2개 미만은 KeyError 가 아니라 Fail
            T.load_ref(str(p), "C1", [TD], "s" * 64, kn)


def test_prep_reports_c5_c6_tokens_without_c4(headers, tmp_path):
    d, args = headers
    out = tmp_path / "prep.json"
    argv = ["prep", "--labelset", args.labelset, "--aliases", args.aliases, "--plan", args.plan, "--recalls", args.recalls,
            "--store", args.store, "--t", TD, "--c3-header-file", args.c3_header_file, "--c5-header-file",
            args.c5_header_file, "--c6-header-file", args.c6_header_file, "--out", str(out)]
    assert T.main(argv) == 0
    doc = json.loads(out.read_text())
    assert set(doc["combos"]) == {"C1", "C2", "C3", "C5", "C6"} and set(doc["c4_identical"]) == {"C1", "C2", "C3"}
    # 런 5 C1 정규 sha f7fcf626... 는 산출물 기록값이다. 재계산 값은 R6-1(#387) 기본 어휘(status_terms) 반영.
    assert doc["combos"]["C1"]["knobs_sha256"] == "dfcc684d75bd3457c373169077742293897049003887517f31848bb3fc0990a1"


@pytest.mark.parametrize("vs_ref, rc, combo", [
    ({"best": "C1", "replace": False, "ref": "C1"}, 1, None),     # 참고 유지면 자격 없는 C5 knobs 를 쓰지 않는다
    ({"best": "C6", "replace": True, "ref": "C1"}, 0, "C6"),      # 교체면 selection 이 아닌 selection_vs_ref 의 조합
])
def test_best_follows_selection_vs_ref(headers, tmp_path, vs_ref, rc, combo):
    d, args = headers
    ctx = T.Ctx(args)
    assert run(ctx, d, combo="C5", rep=1) == 0 and run(ctx, d, combo="C6", rep=1) == 0
    judge = {"t": [TD], "selection": {"best": "C5", "verdict": "no-eligible"}, "selection_vs_ref": vs_ref,
             "combos": {"C5": {"reps": [{"label": "C5-r1"}]}, "C6": {"reps": [{"label": "C6-r1"}]}}}
    (tmp_path / "judge.json").write_text(json.dumps(judge))
    out = tmp_path / "knobs.json"
    assert T.main(["best", "--judge", str(tmp_path / "judge.json"), "--gen-dir", str(d / "w" / "gen"),
                   "--out", str(out)]) == rc
    if combo is None:
        assert not out.exists()
    else:
        assert OT.parse_knobs(out.read_text())[0] == ctx.knobs(combo)


# --- #383 3단계: 표 자체 정밀도(소비자 없음) ----------------------------------------------------------------

def test_table_states_open_rows_only():
    st = T.table_states({TD: {"app#1"}}, {("app#1", TD), ("app#2", TD), ("app#3", "2026-10-04")})
    assert st == {("app#1", TD): "open", ("app#2", TD): "closed", ("app#3", "2026-10-04"): "closed"}


@pytest.mark.parametrize("with_reasons", [False, True])
def test_table_score_p_pbank_and_two_rbank_denominators(with_reasons):
    pos = {("app#2", TD), ("app#4", TD)}
    ctl = {("app#1", TD), ("app#3", TD), ("app#5", TD)}
    present = {("app#1", TD), ("app#3", TD)}
    open_by_t = {TD: {"app#1", "app#2", "app#5"}}          # P 오답 app#2, R 누락 app#3
    bank_new = {("app#1", TD): "열림", ("app#3", TD): "끝남", ("app#5", TD): "열림"}
    bank_ref = {("app#1", TD): "열림", ("app#3", TD): "열림", ("app#5", TD): "끝남"}
    reasons = {(TD, X.key_code("app#2"), "P"): "i", (TD, X.key_code("app#3"), "R"): "iii"} if with_reasons else {}
    sc = T.table_score(open_by_t, pos, ctl, present, bank_new, bank_ref, reasons)
    assert sc["pr"]["P_num"] == 2 and sc["pr"]["P_den"] == 3 and sc["P_errors"] == 1 and sc["R_misses"] == 1
    assert sc["R_bank_new"]["R_bank_den"] == 2 and sc["R_bank_new"]["R_bank"] == 1.0
    assert sc["R_bank_ref"]["R_bank_den"] == 2 and sc["R_bank_ref"]["R_bank"] == 0.5
    if with_reasons:
        assert sc["missing"] == [] and sc["adj"]["P_bank"] == pytest.approx(2 / 3)    # (i) 는 분모에 남는다
        assert sc["adj"]["R_bank2_den"] == 1
    else:
        assert sc["adj"] is None and sorted(m[0] for m in sc["missing"]) == ["P", "R"]


def test_table_command_is_registered_with_ref_store():
    with pytest.raises(SystemExit):
        T.main(["table", "--help"])


# --- final-review #383-1: 사유 파일 저장소 대조 ----------------------------------------------------------------

@pytest.mark.parametrize("doc, ok", [
    ({"store_sha256": "s" * 64, "items": []}, True),
    ({"store_sha256": "o" * 64, "items": []}, False),           # 다른 저장소로 분류한 사유
    ({"items": []}, False),                                    # 저장소 sha 없음
    ([], False),                                               # 목록 형식: 대조할 수 없음
])
def test_table_reasons_must_be_classified_with_this_store(tmp_path, doc, ok):
    p = tmp_path / "reasons.json"
    p.write_text(json.dumps(doc))
    if ok:
        T.check_reasons_store(str(p), "s" * 64)
    else:
        with pytest.raises(T.Fail, match="store_sha256"):
            T.check_reasons_store(str(p), "s" * 64)
