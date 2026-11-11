"""R7-6 v2.1 드라이런 하네스(issue #412, 상위 #393): `bench/r7_endtoend.py`·`bench/r7_eval.py`. 네트워크 0, LLM 0.

고정하는 것(parametrize 행렬):
  (a) 벤치 렌더 = 게이트웨이 렌더: over-max 가 있는 고정 입력에서 게이트웨이 MCP recall 응답의 표와 벤치 `render_table`
      표(payload sha256)가 같다(손잡이 기본·drop·keep x 근거 상황). 벤치는 `repo_max_numbers(load_github(<저장소>))` 를 넘긴다.
  (b) v2.1 저장소 수락: 단계 A 감사 판정(`audit_check_v2`·`audit`)이 v2·v2.1 을 받고 v1 은 거부. r7_eval `table3` 가
      최대 번호를 넘긴다. v1·v2(over-max 없음) 경로 표 바이트 불변.
  (c) 관문 재정의: 롤백 범주(GitHub 끝남·표 반증·표 안 over-max 줄·교차 충돌 종결 줄·실행기 실패)가 각각 1이면 rollback,
      공개 범주(미연결 키·재오픈 미상·최대 번호 이하 없는 번호·키 없는 끝남·열린 PR·residual 종결)만 있으면 pass.
  (d) 교차 충돌: `확인된 종결`·`종결(미계획)` 줄 키의 근거 기억 같은 문장에 명시된 다른 리포의 같은 번호가 t0 열림.
  (e) exit 1(실패)과 요약 `gate.verdict` rollback 이 함께 나온다.
"""
import hashlib
import json
from datetime import datetime, timezone

import pytest

from bench import r7_endtoend as S
from bench import r7_eval as V
from gateway import item_open_table as OT
from gateway import item_roles as IR
from test_item_open_table import QTS, STATUS_Q, _s3_env, run, table_of  # noqa: F401
from test_item_open_table_410 import CASES as OM_CASES, FETCHED_EPOCH, HOUR, setup as om_setup
from test_item_status import PROJECT
from test_r7_endtoend import (ALIASES, T0, T0_TEXT, _fetch_factory, at, at11, attached_recall, gens_of, it, item,
                              smoke_cache, smoke_gen, smoke_store, write_json)


def _sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# --- (a) 벤치 렌더 = 게이트웨이 렌더 ----------------------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(OT, "_wall_clock", lambda: FETCHED_EPOCH + HOUR)


@pytest.mark.parametrize("knob", [None, "drop", "keep"])
@pytest.mark.parametrize("case", ["over-max", "unmentioned-higher-number-raises-max", "no-items-no-max",
                                  "repo-error-not-judged"])
def test_bench_render_equals_gateway_render(monkeypatch, tmp_path, clock, case, knob):
    repos, items, _, _, _ = OM_CASES[case]
    store = om_setup(tmp_path, repos=repos, items=items)
    knobs = {"table_fmt": "b", **({} if knob is None else {"over_max_rows": knob})}
    resp, _, up = run(monkeypatch, tmp_path, knobs=knobs)
    gw = table_of(resp)
    assert gw is not None
    parsed, warnings = OT.parse_knobs(json.dumps(knobs))
    assert warnings == []
    args = {"query": STATUS_Q, "query_timestamp": QTS, "tags": [PROJECT]}
    table, why = S.render_table(OT, args, up.rows, str(store), {}, parsed,
                                now=datetime.fromtimestamp(FETCHED_EPOCH + HOUR, timezone.utc))
    assert why == "attached"
    assert _sha(OT.payload_table(table)) == _sha(gw)


def test_render_passes_repo_max_from_full_evidence(tmp_path, clock):
    store = om_setup(tmp_path)
    assert S.store_max_numbers(str(store)) == OT.repo_max_numbers(IR.load_github(str(store)))
    assert S.store_max_numbers(str(store)) == OT.read_store(str(store), 60.0, OT.GITHUB_ALL).github_max


# --- (b) v2.1 수락, v1·v2 경로 불변 --------------------------------------------------------------------------------

def _use_line(key_rules):
    return json.dumps({"ts": "2026-11-06T01:00:00Z", "decision": OT.DECISION, "reason": "attached",
                       "item_open_table": {"knobs_source": "file", "knobs_sha256": "k", "key_rules": key_rules,
                                           "github_evidence": "use", "counts": {OT.ROW_CANDIDATE: 1},
                                           "unknown_reasons": {}, "github_fetched_at": "2026-11-06T00:30:00Z"}})


@pytest.mark.parametrize("key_rules,ok", [("v2", True), ("v2.1", True), ("v1", False)])
def test_audit_check_accepts_v21(key_rules, ok):
    c = S.audit_counts_v2([_use_line(key_rules)], "2026-11-06T00:00:00Z", "2026-11-06T02:00:00Z")
    got = S.audit_check_v2(c, 1, "k", "use", saved_tables=1, expected={"attached": 1})
    assert got["ok"] is ok, got


@pytest.mark.parametrize("key_rules,ok", [("v2", True), ("v2.1", True), (None, False)])
def test_cmd_audit_store_key_rules(tmp_path, key_rules, ok):
    meta = {"model": "m", "prompt_sha256": "p", "aliases_sha256": OT.aliases_sha256(ALIASES), "updated_at": "u"}
    if key_rules:
        meta["key_rules"] = key_rules
    ev = {"repos": {"hindsight-crew": {"owner": "o", "status": "ok", "fetched_at": "2026-11-06T00:30:00Z",
                                       "error_code": None}}, "items": {}}
    store = str(tmp_path / "s.sqlite")
    IR.publish(store, meta, {"a": {"document_id": "d", "mentioned_at": at(3), "state": "valid", "text_sha256": "s",
                                   "judged_at": "j"}}, {"a": {"hindsight-crew#1": "open"}}, mode=0o600, github=ev)
    kn = tmp_path / "kn.json"
    kn.write_text(json.dumps(S.knobs_stage_a("use")))
    line = json.loads(_use_line(key_rules or "v1"))
    line["item_open_table"]["knobs_sha256"] = OT._canon_sha256(S.knobs_stage_a("use"))
    log = tmp_path / "gateway.log"
    log.write_text(json.dumps(line) + "\n")
    win = tmp_path / "win.json"
    win.write_text(json.dumps({"on": {"start_utc": "2026-11-06T00:00:00Z", "end_utc": "2026-11-06T02:00:00Z",
                                      "recalls": 1, "arm": "on", "knobs_file": str(kn), "store": store}}))
    al = write_json(tmp_path / "al.json", ALIASES)
    out = tmp_path / "audit.json"
    assert S.main(["audit", "--log", str(log), "--windows", str(win), "--aliases", al, "--out", str(out)]) == 0
    assert json.loads(out.read_text())["windows"]["on"]["check"]["ok"] is ok


def _eval_store(tmp_path, key_rules, high=30, top=25):
    """열림 언급 #5·#20·#high, 근거: #20 열림, #top 닫힘(리포 최대 = top). high > top 이면 #high 는 over-max."""
    meta = {"model": "m", "prompt_sha256": "p", "aliases_sha256": OT.aliases_sha256(ALIASES), "updated_at": "u"}
    if key_rules:
        meta["key_rules"] = key_rules
    f = "2026-10-27T00:00:00Z"
    ev = {"repos": {"hindsight-crew": {"owner": "o", "status": "ok", "fetched_at": f, "error_code": None}},
          "items": {("hindsight-crew", 20): dict(it(), fetched_at=f),
                    ("hindsight-crew", top): dict(it(state="closed", reason="completed", closed=at(10)), fetched_at=f)}}
    path = str(tmp_path / ("e-%s-%d.sqlite" % (key_rules, high)))
    IR.publish(path, meta, {"a": {"document_id": "d", "mentioned_at": at(3), "state": "valid", "text_sha256": "s",
                                  "judged_at": "j"}},
               {"a": {"hindsight-crew#5": "open", "hindsight-crew#20": "open", "hindsight-crew#%d" % high: "open"}},
               mode=0o600, github=ev)
    return path


ET = datetime(2026, 10, 28, 14, 59, 59, tzinfo=timezone.utc)


@pytest.mark.parametrize("key_rules", ["v2", "v2.1"])
def test_table3_drops_over_max_like_the_gateway(tmp_path, key_rules):
    view = V.store_view(_eval_store(tmp_path, key_rules), ET)
    tab = V.table3(view, ET)
    keys = [r[0] for r in tab["table"]["rows"]]
    assert "hindsight-crew#30" not in keys and "hindsight-crew#5" in keys
    assert tab["table"].get("over_max") == 1


@pytest.mark.parametrize("key_rules", [None, "v2", "v2.1"])
def test_table3_without_over_max_is_unchanged(tmp_path, key_rules):
    view = V.store_view(_eval_store(tmp_path, key_rules, high=24), ET)
    tab = V.table3(view, ET)
    plain = OT.build_table(view.states, ET, None, V.table_knobs(), github=view.github)
    assert tab["table"]["text"] == plain["text"] and tab["table"]["rows"] == plain["rows"]


@pytest.mark.parametrize("key_rules", ["v2", "v2.1"])
def test_smoke_attach_accepts_v21_store_without_over_max_bytes(tmp_path, key_rules):
    """over-max 없는 저장소: 최대 번호를 넘겨도 #408 렌더와 같은 표(v2 경로 불변), v2.1 저장소도 붙는다."""
    store = smoke_store(tmp_path, name="s-%s.sqlite" % key_rules)
    if key_rules == "v2.1":
        import sqlite3
        conn = sqlite3.connect(store)
        conn.execute("UPDATE meta SET v = 'v2.1' WHERE k = 'key_rules'")
        conn.commit()
        conn.close()
    rec, _, _ = attached_recall(tmp_path, store=store)
    tab = json.loads(open(rec, encoding="utf-8").read())["runs"][0]["open_table"]
    knobs = S.knobs_stage_a("use")
    args = {"query": "지금 남은 할 일 목록 알려줘", "query_timestamp": T0_TEXT}
    rows = json.loads(open(rec, encoding="utf-8").read())["runs"][0]["results"]
    st = OT.read_store(store, 60.0, OT.GITHUB_ALL)
    plain = OT.build_table(OT.role_states(st.mentions, T0, st.key_rules), T0, {"hindsight-crew"}, knobs,
                           github=st.github, result_keys=OT.result_keys(rows, ALIASES, st.key_rules), now=T0)
    assert tab["text"] == OT.payload_table(plain)["text"]
    assert json.loads(open(rec, encoding="utf-8").read())["runs"][0]["open_table_over_max"] == 0


# --- (c) 관문 재정의 ---------------------------------------------------------------------------------------------

GATE_CASES = [
    # 이름, 거짓 열림 사유별, 표 안 over-max 줄, 교차 충돌 줄, 실패 코드, 판정
    ("none", {}, 0, 0, [], "pass"),
    ("github-finished", {S.FALSE_GITHUB: 1}, 0, 0, [], "rollback"),
    ("table-refuted", {S.FALSE_REFUTED: 1}, 0, 0, [], "rollback"),
    ("over-max-row", {}, 1, 0, [], "rollback"),
    ("cross-conflict-row", {}, 0, 1, [], "rollback"),
    ("runner-failure", {}, 0, 0, ["valid-reps"], "rollback"),
    ("missing-number-only", {S.FALSE_MISSING: 3}, 0, 0, [], "pass"),
]


@pytest.mark.parametrize("name,by_reason,over,cross,fail,verdict", GATE_CASES, ids=[c[0] for c in GATE_CASES])
def test_gate_categories(name, by_reason, over, cross, fail, verdict):
    g = S.gate_of(by_reason, over, cross, fail)
    assert g["verdict"] == verdict
    assert set(g["rollback"]) == set(S.ROLLBACK_CATEGORIES)
    assert g["rollback_total"] == sum(g["rollback"].values())
    assert g["rollback"]["runner_failure"] == len(fail)


ITEM_CASES = [
    # 이름, 항목, 롤백 범주(없으면 None), 공개 칸(경로)
    ("closed-issue", item("hindsight-crew#2"), "github_finished", None),
    ("merged-pr", item("hindsight-crew#6"), "github_finished", None),
    ("refuted-row", item("hindsight-crew#7"), "table_refuted", None),
    ("missing-le-max", item("hindsight-crew#4"), None, ("missing_number_open", "le_max")),
    ("missing-over-max", item("hindsight-crew#99"), None, ("missing_number_open", "over_max")),
    ("missing-max-unknown", item("other-repo#3"), None, None),
    ("unlinked", item("gone#1"), None, ("unconfirmed",)),
    ("open-pr", item("hindsight-crew#5"), None, ("open_pr_items",)),
    ("open-issue", item("hindsight-crew#1"), None, None),
]


def _item_gh():
    gh = smoke_cache()
    gh["other-repo"] = {"status": 200, "owner": "o", "calls": 1, "fetched_at": "2026-11-06T01:05:00Z", "items": {},
                        "events": {}}
    return gh


@pytest.mark.parametrize("name,itm,cat,disc", ITEM_CASES, ids=[c[0] for c in ITEM_CASES])
def test_smoke_score_rollback_vs_disclosure(tmp_path, name, itm, cat, disc):
    rec, _, _ = attached_recall(tmp_path)
    rows = S.response_rows(json.loads(open(rec, encoding="utf-8").read()))
    filler = item("hindsight-crew#1")             # 분모(열린 항목·키·연결 키)를 채워 실행기 실패와 섞이지 않게
    res = S.smoke_score(gens_of([itm, filler]), aliases=ALIASES, gh=_item_gh(), t0=T0, bank={}, rows=rows, mems={},
                        min_valid=1, max_numbers={"hindsight-crew": 10})
    assert res["fail"] == []
    g = res["gate"]
    assert g["verdict"] == ("rollback" if cat else "pass")
    for c in S.ROLLBACK_CATEGORIES:
        assert g["rollback"][c] == int(c == cat)
    d = res["disclosure"]
    if disc:
        v = d
        for p in disc:
            v = v[p]
        assert v == 1
    if name == "missing-max-unknown":
        assert d["missing_number_open"] == {"le_max": 0, "over_max": 0, "max_unknown": 1}


def test_smoke_score_disclosure_keyless_reopened_residual(tmp_path):
    rec, _, _ = attached_recall(tmp_path)
    rows = S.response_rows(json.loads(open(rec, encoding="utf-8").read()))
    gh = smoke_cache()
    gh["hindsight-crew"]["items"]["8"] = it(state="open", reason="reopened")
    gh["hindsight-crew"]["events"]["8"] = []
    mems = {"m-done": {"id": "m-done", "text": "[완료] 대시보드 정리 작업을 마쳤다.", "tags": [],
                       "mentioned_at": at(20)}}
    items = [item(None, ev=["m-done"]), item("hindsight-crew#2", status="residual"), item("hindsight-crew#1"),
             item("hindsight-crew#8")]
    res = S.smoke_score(gens_of(items), aliases=ALIASES, gh=gh, t0=T0, bank={}, rows=rows, mems=mems, min_valid=1,
                        max_numbers={})
    assert res["gate"]["verdict"] == "pass"
    assert res["disclosure"]["keyless_finished"] == 1
    assert res["disclosure"]["residual_finished"] == {"items": 1, "unique_keys": 1}
    assert res["disclosure"]["reopened_unknown"] == 1


# --- 표 안 over-max 줄 ------------------------------------------------------------------------------------------

def _tab(rows):
    body = "\n".join(" | ".join(r) for r in rows)
    return {"t": "2026-11-06", "fmt": "b", "keys": [], "truncated": 0, "counts": {}, "fetched_at": "x",
            "text": "<item_open_table>\n머리\n%s\n</item_open_table>" % body}


OK_GH = {"repos": {"hindsight-crew": {"status": "ok", "fetched_at": "2026-11-06T00:30:00Z"},
                   "bad": {"status": "error", "fetched_at": "2026-11-06T00:30:00Z"}}, "items": {}}


@pytest.mark.parametrize("line,maxes,n", [
    (["hindsight-crew#30", OT.ROW_UNKNOWN, "no-number"], {"hindsight-crew": 25}, 1),
    (["hindsight-crew#20", OT.ROW_UNKNOWN, "no-number"], {"hindsight-crew": 25}, 0),
    (["hindsight-crew#30", OT.ROW_UNKNOWN, "pr-open"], {"hindsight-crew": 25}, 0),
    (["hindsight-crew#30", OT.ROW_UNKNOWN, "no-number"], {}, 0),
    (["bad#30", OT.ROW_UNKNOWN, "no-number"], {"bad": 25}, 0),
    (["hindsight-crew#30", OT.ROW_CANDIDATE, "2026-11-01"], {"hindsight-crew": 25}, 0),
])
def test_over_max_rows_in_table(line, maxes, n):
    assert len(S.over_max_rows(_tab([line]), OK_GH, maxes)) == n


# --- (d) 교차 충돌 ----------------------------------------------------------------------------------------------

def _cross_gh(alpha_state):
    items = {"4": it(), "3": it()} if alpha_state == "open" else {
        "4": it(state="closed", reason="completed", closed=at11(2)), "3": it()}
    gh = {"hindsight-crew": {"status": 200, "owner": "o", "calls": 1, "fetched_at": "2026-11-06T01:05:00Z",
                             "items": {"4": it(state="closed", reason="completed", closed=at11(2))}, "events": {}}}
    if alpha_state != "missing":
        gh["alpha"] = {"status": 200, "owner": "o", "calls": 1, "fetched_at": "2026-11-06T01:05:00Z", "items": items,
                       "events": {} if alpha_state == "open" else {"4": [["closed", at11(2), None]]}}
    else:
        gh["alpha"] = {"status": 404, "owner": "o", "calls": 1, "fetched_at": "2026-11-06T01:05:00Z"}
    return gh


CROSS = [
    # 이름, 근거 기억 본문, alpha#4 상태, 줄 상태, 교차 충돌 줄 수
    ("other-open", "o/alpha#4 진행 중, hindsight-crew#4 완료", "open", OT.ROW_CLOSED, 1),
    ("other-open-not-planned-row", "o/alpha#4 진행 중, hindsight-crew#4 완료", "open", OT.ROW_NOT_PLANNED, 1),
    ("other-closed", "o/alpha#4 진행 중, hindsight-crew#4 완료", "closed", OT.ROW_CLOSED, 0),
    ("other-repo-missing", "o/alpha#4 진행 중, hindsight-crew#4 완료", "missing", OT.ROW_CLOSED, 0),
    ("other-sentence", "o/alpha#4 진행 중.\nhindsight-crew#4 완료", "open", OT.ROW_CLOSED, 0),
    ("same-repo-only", "hindsight-crew#4 완료, hindsight-crew#9 진행", "open", OT.ROW_CLOSED, 0),
    ("enumeration", "o/alpha#3 · #4 진행, hindsight-crew#4 완료", "open", OT.ROW_CLOSED, 1),
    ("candidate-row-not-counted", "o/alpha#4 진행 중, hindsight-crew#4 완료", "open", OT.ROW_CANDIDATE, 0),
]


@pytest.mark.parametrize("name,text,alpha,state,n", CROSS, ids=[c[0] for c in CROSS])
@pytest.mark.parametrize("rules", ["v2", "v2.1"])
def test_cross_conflict_rows(name, text, alpha, state, n, rules):
    tab = _tab([["hindsight-crew#4", state, "2026-11-02"]])
    listing = {"m": {"id": "m", "text": text, "tags": ["project:hindsight-crew"]}}
    cands, missing = S.cross_candidates(tab, {"m": {"hindsight-crew#4"}}, listing, ALIASES, rules)
    assert missing == 0
    rows = S.cross_conflict_rows(cands, _cross_gh(alpha), T0)
    assert len(rows) == n


def test_cross_candidates_counts_missing_bodies():
    tab = _tab([["hindsight-crew#4", OT.ROW_CLOSED, "2026-11-02"]])
    cands, missing = S.cross_candidates(tab, {"m": {"hindsight-crew#4"}, "z": {"hindsight-crew#4"}},
                                        {"m": {"id": "m", "text": "hindsight-crew#4 완료", "tags": []}}, ALIASES,
                                        "v2.1")
    assert cands == [] and missing == 1


# --- (e) exit 1 과 gate.verdict ----------------------------------------------------------------------------------

def _listing(tmp_path):
    bodies = tmp_path / "bodies.jsonl"
    meta = tmp_path / "meta.jsonl"
    bodies.write_text(json.dumps({"id": "a", "text": "hindsight-crew#1 진행, hindsight-crew#2 와 hindsight-crew#7 정리 남음"},
                                 ensure_ascii=False) + "\n")
    meta.write_text(json.dumps({"id": "a", "tags": ["project:hindsight-crew"], "state": "valid", "document_id": "d",
                                "mentioned_at": at(20), "fact_type": "world"}) + "\n")
    return str(bodies), str(meta)


def _argv(tmp_path, rec, store, al, gen, extra=()):
    owners = write_json(tmp_path / "owners.json", {"hindsight-crew": "o"})
    work = tmp_path / "w"
    work.mkdir(mode=0o700, exist_ok=True)
    b, m = _listing(tmp_path)
    return ["smoke", "--t0", T0_TEXT, "--gen", gen, "--recall", rec, "--store", store, "--aliases", al,
            "--github-owners", owners, "--github-default-owner", "o", "--work", str(work), "--listing", b, m,
            *extra], work


@pytest.mark.parametrize("case,rc,verdict", [
    ("pass", 0, "pass"),
    ("rollback", 4, "rollback"),
    ("valid-short", 1, "rollback"),
    ("input-t0", 1, "rollback"),
])
def test_cmd_smoke_exit_and_verdict(tmp_path, capsys, case, rc, verdict):
    rec, store, al = attached_recall(tmp_path)
    items = [item("hindsight-crew#2")] if case == "rollback" else [item("hindsight-crew#1")]
    gen = smoke_gen(tmp_path, rec, gens_of(*([items] * 3)))
    extra = {"valid-short": ("--min-valid", "4"), "input-t0": ("--t0", "2026-11-06T02:00:00Z")}.get(case, ())
    argv, work = _argv(tmp_path, rec, store, al, gen)
    if extra:
        argv = argv[:argv.index("--t0")] + argv[argv.index("--t0") + 2:] if case == "input-t0" else argv
        argv += list(extra)
    fetch, fill = _fetch_factory()
    assert S.cmd_smoke(S.parse_args(argv), fetch=fetch, fill=fill) == rc
    out = json.loads((work / "r7-4-smoke.json").read_text())
    assert out["gate"]["verdict"] == verdict
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["verdict"] == verdict
    if rc == 1:
        assert out["gate"]["rollback"]["runner_failure"] == len(out["fail"]) > 0
    if rc != 1:
        assert set(out["tables"]["symptom"]) >= {"over_max_rows", "cross_conflict_rows"}
        assert out["tables"]["symptom"]["cross_conflict_rows"] == 0


def test_cmd_smoke_needs_listing(tmp_path):
    rec, store, al = attached_recall(tmp_path)
    gen = smoke_gen(tmp_path, rec, gens_of(*([[item("hindsight-crew#1")]] * 3)))
    argv, work = _argv(tmp_path, rec, store, al, gen)
    i = argv.index("--listing")
    with pytest.raises(SystemExit):
        S.parse_args(argv[:i] + argv[i + 3:])


def test_cmd_table_gate(tmp_path):
    rec, store, al = attached_recall(tmp_path)
    owners = write_json(tmp_path / "owners.json", {"hindsight-crew": "o"})
    work = tmp_path / "w"
    work.mkdir(mode=0o700)
    b, m = _listing(tmp_path)
    fetch, fill = _fetch_factory()
    argv = ["table-gate", "--t0", T0_TEXT, "--recall", rec, "--store", store, "--aliases", al, "--listing", b, m,
            "--github-owners", owners, "--github-default-owner", "o", "--work", str(work), "--label", "tg"]
    assert S.cmd_table_gate(S.parse_args(argv), fetch=fetch, fill=fill) == S.EXIT_ROLLBACK   # #414: #7 종결 줄 오류
    out = json.loads((work / "tg-tables.json").read_text())
    t = out["tables"][next(iter(out["tables"]))]
    assert t["closed_errors"] == 1 and out["gate"]["rollback"]["closed_errors"] == 1        # #414
    assert t["over_max_rows"] == 0 and t["cross_conflict_rows"] == 0 and t["rows"] == 4
    assert out["totals"] == {"over_max_rows": 0, "cross_conflict_rows": 0, "cross_conflict_rows_all": 0,
                             "missing_bodies": 0}


# --- rework #412-1: 교차 충돌 좁힌 정의(줄 키 근거 언급이 명시 접두만이면 제외) ---------------------------------------

NARROW = [
    # 이름, 근거 기억들 {id: 본문}, 원래 정의 줄 수, 좁힌 정의 줄 수, 줄 키 출처 분류
    ("explicit-only", {"m": "o/alpha#4 진행 중, hindsight-crew#4 완료"}, 1, 0, "explicit-only"),
    ("mixed-bare-elsewhere", {"m": "o/alpha#4 진행 중, hindsight-crew#4 완료", "b": "#4 완료"}, 1, 1, "mixed"),
    ("enum-propagated", {"m": "o/alpha#4 진행, hindsight-crew#3 · #4 완료"}, 1, 1, "mixed"),
    ("no-conflict", {"m": "hindsight-crew#4 완료"}, 0, 0, None),
]


@pytest.mark.parametrize("name,texts,n_all,n_narrow,src", NARROW, ids=[c[0] for c in NARROW])
def test_cross_conflict_narrowed_definition(tmp_path, name, texts, n_all, n_narrow, src):
    tab = _tab([["hindsight-crew#4", OT.ROW_CLOSED, "2026-11-02"]])
    listing = {m: {"id": m, "text": t, "tags": ["project:hindsight-crew"]} for m, t in texts.items()}
    mem_keys = {m: {"hindsight-crew#4"} for m in texts}
    tg = S.TableGate.__new__(S.TableGate)
    tg.store_github, tg.max_numbers, tg.mem_keys, tg.rules = {}, {}, mem_keys, "v2.1"
    tg.aliases, tg.listing, tg.t0, tg._cands = ALIASES, listing, T0, {}
    got = tg.check(tab, _cross_gh("open"))
    assert got["cross_conflict_rows_all"] == n_all
    assert got["cross_conflict_rows"] == n_narrow
    if src:
        assert got["cross_detail"]["row_sources"] == {src: 1}
        assert got["cross_detail"]["other_key_in_sentence"] == {"yes": 1}     # o/alpha#4 는 문장에 키로 있다


def test_cross_both_values_in_smoke_and_gate(tmp_path):
    rec, store, al = attached_recall(tmp_path)
    gen = smoke_gen(tmp_path, rec, gens_of(*([[item("hindsight-crew#1")]] * 3)))
    argv, work = _argv(tmp_path, rec, store, al, gen)
    fetch, fill = _fetch_factory()
    assert S.cmd_smoke(S.parse_args(argv), fetch=fetch, fill=fill) == 0
    t = json.loads((work / "r7-4-smoke.json").read_text())["tables"]["symptom"]
    assert t["cross_conflict_rows"] == 0 and t["cross_conflict_rows_all"] == 0 and "cross_detail" in t
    d = json.loads((work / "r7-4-smoke.json").read_text())["disclosure"]
    assert d["cross_conflict_rows_all"] == 0 and d["cross_detail"] == {"symptom": t["cross_detail"]}


# --- rework #412-2: 본문 없는 기억 = 실패, 예상 밖 예외·봉인에서도 요약이 pass 로 남지 않음 ---------------------------

def _no_body_listing(tmp_path):
    bodies, meta = tmp_path / "nb-bodies.jsonl", tmp_path / "nb-meta.jsonl"
    bodies.write_text(json.dumps({"id": "zz", "text": "무관"}, ensure_ascii=False) + "\n")
    meta.write_text(json.dumps({"id": "zz", "tags": [], "state": "valid", "document_id": "d", "mentioned_at": at(20),
                                "fact_type": "world"}) + "\n")
    return str(bodies), str(meta)


def _swap_listing(argv, b, m):
    i = argv.index("--listing")
    return argv[:i + 1] + [b, m] + argv[i + 3:]


@pytest.mark.parametrize("cmd", ["smoke", "table-gate"])
def test_missing_bodies_is_a_failure(tmp_path, cmd):
    rec, store, al = attached_recall(tmp_path)
    b, m = _no_body_listing(tmp_path)
    fetch, fill = _fetch_factory()
    if cmd == "smoke":
        gen = smoke_gen(tmp_path, rec, gens_of(*([[item("hindsight-crew#1")]] * 3)))
        argv, work = _argv(tmp_path, rec, store, al, gen)
        rc = S.cmd_smoke(S.parse_args(_swap_listing(argv, b, m)), fetch=fetch, fill=fill)
        out = json.loads((work / "r7-4-smoke.json").read_text())
    else:
        owners = write_json(tmp_path / "owners.json", {"hindsight-crew": "o"})
        work = tmp_path / "w"
        work.mkdir(mode=0o700)
        argv = ["table-gate", "--t0", T0_TEXT, "--recall", rec, "--store", store, "--aliases", al, "--listing", b, m,
                "--github-owners", owners, "--github-default-owner", "o", "--work", str(work), "--label", "tg"]
        rc = S.cmd_table_gate(S.parse_args(argv), fetch=fetch, fill=fill)
        out = json.loads((work / "tg-tables.json").read_text())
    assert rc == 1
    assert any(f.startswith("missing-bodies") for f in out["fail"])
    assert out["gate"]["verdict"] == "rollback"


@pytest.mark.parametrize("cmd", ["smoke", "table-gate"])
@pytest.mark.parametrize("where", ["early", "after-pass"])
def test_unexpected_exception_never_leaves_pass(tmp_path, monkeypatch, cmd, where):
    import sqlite3
    rec, store, al = attached_recall(tmp_path)
    fetch, fill = _fetch_factory()
    if cmd == "smoke":
        gen = smoke_gen(tmp_path, rec, gens_of(*([[item("hindsight-crew#1")]] * 3)))
        argv, work = _argv(tmp_path, rec, store, al, gen)
        run_ = lambda: S.cmd_smoke(S.parse_args(argv), fetch=fetch, fill=fill)  # noqa: E731
        summary, details = work / "r7-4-smoke.json", "-smoke-details.json"
    else:
        owners = write_json(tmp_path / "owners.json", {"hindsight-crew": "o"})
        work = tmp_path / "w"
        work.mkdir(mode=0o700)
        b, m = _listing(tmp_path)
        argv = ["table-gate", "--t0", T0_TEXT, "--recall", rec, "--store", store, "--aliases", al, "--listing", b, m,
                "--github-owners", owners, "--github-default-owner", "o", "--work", str(work), "--label", "tg"]
        run_ = lambda: S.cmd_table_gate(S.parse_args(argv), fetch=fetch, fill=fill)  # noqa: E731
        summary, details = work / "tg-tables.json", "-tables-details.json"
    if where == "early":
        def boom(*a, **k):
            raise sqlite3.OperationalError("synthetic")
        monkeypatch.setattr(S, "check_tables", boom)
    else:
        real = S.V.write_private

        def wp(path, text):
            if str(path).endswith(details):
                raise OSError("synthetic")
            return real(path, text)
        monkeypatch.setattr(S.V, "write_private", wp)
    with pytest.raises((sqlite3.OperationalError, OSError)):
        run_()
    out = json.loads(summary.read_text())
    assert out["gate"]["verdict"] == "rollback"
    assert any(f.startswith("error:") for f in out["fail"])


@pytest.mark.parametrize("cmd", ["smoke", "table-gate"])
def test_seal_guard_summary_is_not_pass(tmp_path, cmd):
    rec, store, al = attached_recall(tmp_path)
    sealed = tmp_path / "sealed-x.json"
    sealed.write_text(open(rec, encoding="utf-8").read())
    owners = write_json(tmp_path / "owners.json", {"hindsight-crew": "o"})
    work = tmp_path / "w"
    work.mkdir(mode=0o700)
    b, m = _listing(tmp_path)
    if cmd == "smoke":
        gen = smoke_gen(tmp_path, rec, gens_of(*([[item("hindsight-crew#1")]] * 3)))
        argv = ["smoke", "--t0", T0_TEXT, "--gen", gen, "--recall", rec, "--table-recall", str(sealed), "--store", store,
                "--aliases", al, "--github-owners", owners, "--github-default-owner", "o", "--work", str(work),
                "--listing", b, m]
        summary = work / "r7-4-smoke.json"
    else:
        argv = ["table-gate", "--t0", T0_TEXT, "--recall", str(sealed), "--store", store, "--aliases", al,
                "--listing", b, m, "--github-owners", owners, "--github-default-owner", "o", "--work", str(work),
                "--label", "tg"]
        summary = work / "tg-tables.json"
    assert S.main(argv) == 2
    out = json.loads(summary.read_text())
    assert out["gate"]["verdict"] == "rollback" and any(f.startswith("error:SealGuard") for f in out["fail"])
