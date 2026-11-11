"""issue #350 d-llm 오프라인 목록 경로 - 대응 규칙(#349 "d-llm 판정" (1)~(5), (4a)), 스키마 검증, 입력 조립.

LLM 호출은 가짜 출력 주입으로만 한다(네트워크 0). 작업 표·recall 행·목록 출력은 전부 합성이다(실제 항목 이름 없음).
"""
import json

import pytest

from bench import d_llm as D
from bench import d_llm_match as M


def task(tid, keywords, hint=None, keys=None):
    t = {"id": tid, "keywords": list(keywords), "project_hint": hint}
    if keys is not None:
        t["keys"] = list(keys)
    return t


TASKS = [
    task("T1", ["alpha-kw", "#901"], hint="proj-a"),
    task("T2", ["beta-kw"]),
    task("T3", ["gamma-kw", "gamma-kw-long"], hint="proj-b"),
    task("T4", ["shared-kw"]),
    task("T5", ["shared-kw"]),
]


def item(name, status="open", key=None, quote="", evidence=None, unverified=None):
    it = {"name": name, "status": status, "evidence_ids": list(evidence or []), "quote": quote}
    if key is not None:
        it["key"] = key
    if unverified is not None:
        it["unverified"] = unverified
    return it


# ---------------------------------------------------------------- 대응 규칙 행렬

@pytest.mark.parametrize("it, want_task, want_by, want_reason", [
    # (1) key 일치가 1순위: 이름에 다른 작업 keyword 가 있어도 key 가 이긴다
    (item("beta-kw 정리", key="proj-a#901"), "T1", "key", None),
    # key 표기 정규화(대소문자·공백·구분 기호)
    (item("무엇", key="Proj-A #901"), "T1", "key", None),
    # 작업 표의 명시 keys 와도 맞는다
    (item("무엇", key="repo-z#7"), "T6", "key", None),
    # (2) 키 없는 항목은 name keyword 로 대응
    (item("beta-kw 마감"), "T2", "name", None),
    # key 가 어느 작업과도 맞지 않으면 name 규칙으로 내려간다
    (item("beta-kw 마감", key="other#1"), "T2", "name", None),
    # 맨 번호 keyword 는 같은 name 에 프로젝트 이름이 있어야 센다
    (item("proj-a #901 후속"), "T1", "name", None),
    (item("#901 후속"), None, None, "none"),
    # (2) quote 에만 keyword 가 있으면 대응하지 않는다
    (item("이름에는 없음", quote="beta-kw 는 끝났다"), None, None, "none"),
    # (3) evidence id 만 겹치면 대응하지 않는다(가산 조건이지 필요조건이 아님)
    (item("이름에는 없음", evidence=["m-beta"]), None, None, "none"),
    # (4) 아무 데도 안 맞으면 unmapped
    (item("전혀 다른 일"), None, None, "none"),
    # (4a) 두 작업에 맞으면 가장 긴 keyword
    (item("gamma-kw-long 과 alpha-kw"), "T3", "name", None),
    # (4a) 동률이면 unmapped(ambiguous)
    (item("shared-kw 작업"), None, None, "ambiguous"),
    (item("zeta-kw 와 beta-kw"), None, None, "ambiguous"),
])
def test_match_item_matrix(it, want_task, want_by, want_reason):
    tasks = TASKS + [task("T6", ["zeta-kw"], keys=["repo-z#7"])]
    m = M.match_item(it, tasks)
    assert (m["task"], m["by"], m["reason"]) == (want_task, want_by, want_reason)


def test_key_shared_by_two_tasks_breaks_tie_by_name_then_ambiguous():
    tasks = [task("A", ["aa-kw"], keys=["r#1"]), task("B", ["bb-kw-longer"], keys=["r#1"])]
    assert M.match_item(item("bb-kw-longer", key="r#1"), tasks)["task"] == "B"
    m = M.match_item(item("nothing", key="r#1"), tasks)
    assert (m["task"], m["reason"]) == (None, "ambiguous")


def test_keyword_boundary_number_does_not_hit_longer_number():
    m = M.match_item(item("proj-a #9010"), TASKS)
    assert m["task"] is None


# ---------------------------------------------------------------- 반복 하나의 상태

@pytest.mark.parametrize("statuses, want", [
    (["open"], "open"),
    (["done"], "done"),
    (["residual"], "partial"),
    (["done", "open"], "open"),            # 하나라도 open 이면 open
    (["residual", "open"], "open"),
    (["done", "residual"], "done"),        # open 없고 done 있으면 done
    (["residual", "residual"], "partial"),
    ([], "absent"),                        # 목록에 없음 = unknown(absent)
])
def test_run_state_many_items_one_task(statuses, want):
    items = [item(f"beta-kw 조각 {i}", status=s) for i, s in enumerate(statuses)]
    r = M.judge_run(items, TASKS)
    assert r["states"]["T2"] == want
    assert M.STATE_CATEGORY[want] in ("open", "done", "partial", "unknown")


def test_unverified_open_counts_as_open_and_is_flagged():
    r = M.judge_run([item("beta-kw", status="open", unverified=True)], TASKS)
    assert r["states"]["T2"] == "open"
    assert r["unverified_open"] == {"T2": True}


def test_unmapped_counted_with_reasons():
    r = M.judge_run([item("shared-kw"), item("무관"), item("beta-kw")], TASKS)
    assert r["unmapped"] == 2
    assert r["unmapped_reasons"] == {"ambiguous": 1, "none": 1}
    assert [m["task"] for m in r["mapping"]] == [None, None, "T2"]


def test_judge_runs_invalid_keeps_denominator_and_majority():
    runs = [
        {"valid": True, "items": [item("alpha-kw")]},
        {"valid": True, "items": [item("alpha-kw")]},
        {"valid": False, "items": []},
        {"valid": True, "items": [item("alpha-kw", status="done")]},
        {"valid": True, "items": []},
    ]
    res = M.judge_runs(runs, TASKS)
    assert res["K"] == 5
    assert res["tasks"]["T1"]["states"] == ["open", "open", "invalid", "done", "absent"]
    assert res["tasks"]["T1"]["open"] == 2
    # 과반은 K 기준(5회면 3), invalid 가 문턱을 낮추지 않는다
    rep = M.reproduction(res, ["T1"])
    assert rep["majority"] == 3
    assert rep["reproduced"] is False
    assert rep["per_run_closed_open"] == [1, 1, 0, 0, 0]


def test_reproduction_true_when_closed_item_open_in_majority():
    runs = [{"valid": True, "items": [item("alpha-kw"), item("beta-kw", status="residual")]} for _ in range(3)]
    runs += [{"valid": True, "items": []} for _ in range(2)]
    rep = M.reproduction(M.judge_runs(runs, TASKS), ["T1", "T2"])
    assert rep["reproduced"] is True
    assert rep["items"] == ["T1"]          # residual(partial)은 재현으로 세지 않는다
    assert rep["per_run_closed_open"] == [1, 1, 1, 0, 0]


# ---------------------------------------------------------------- 스키마 검증과 재시도

GOOD = {"items": [{"key": "proj-a#901", "name": "x", "status": "open", "evidence_ids": ["m1"], "quote": "q"}]}


@pytest.mark.parametrize("text, ok", [
    (json.dumps(GOOD), True),
    ("```json\n" + json.dumps(GOOD) + "\n```", True),
    ("앞말\n" + json.dumps(GOOD) + "\n뒷말", True),
    (json.dumps({"items": [{"name": "x", "status": "open", "evidence_ids": [], "quote": "q", "unverified": True}]}), True),
    (json.dumps({"items": [{"name": "x", "status": "maybe", "evidence_ids": [], "quote": "q"}]}), False),
    (json.dumps({"items": [{"status": "open", "evidence_ids": [], "quote": "q"}]}), False),
    (json.dumps({"items": [{"name": "x", "status": "open", "evidence_ids": "m1", "quote": "q"}]}), False),
    (json.dumps({"items": [{"name": "x", "status": "open", "evidence_ids": [], "quote": "q", "unverified": "yes"}]}), False),
    (json.dumps({"items": [{"name": "x", "status": "open", "evidence_ids": [], "quote": "q", "key": 5}]}), False),
    (json.dumps({"list": []}), False),
    ("not json", False),
])
def test_parse_output_schema(text, ok):
    items, err = D.parse_output(text)
    assert (err is None) is ok
    if ok:
        assert isinstance(items, list)


def fake_caller(outputs):
    calls = []

    def call(prompt):
        calls.append(prompt)
        text = outputs[len(calls) - 1]
        return {"result": text, "is_error": False, "duration_ms": 10, "total_cost_usd": 0.01,
                "usage": {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 100}}
    call.calls = calls
    return call


def test_generate_retries_once_then_valid():
    caller = fake_caller(["garbage", json.dumps(GOOD)])
    rec = D.generate_one("PROMPT", caller)
    assert rec["valid"] is True and rec["attempts"] == 2 and len(caller.calls) == 2
    assert rec["items"][0]["key"] == "proj-a#901"
    assert len(rec["calls"]) == 2 and rec["calls"][0]["valid"] is False


def test_generate_invalid_after_one_retry():
    caller = fake_caller(["garbage", "still garbage", json.dumps(GOOD)])
    rec = D.generate_one("PROMPT", caller)
    assert rec["valid"] is False and rec["attempts"] == 2 and len(caller.calls) == 2
    assert rec["items"] == [] and rec["error"]


def test_generate_is_error_result_is_invalid_attempt():
    def call(prompt):
        return {"result": "x", "is_error": True, "duration_ms": 1, "total_cost_usd": 0, "usage": {}}
    rec = D.generate_one("P", call)
    assert rec["valid"] is False and rec["attempts"] == 2


# ---------------------------------------------------------------- 입력 조립

def rrow(mid, text="t", mentioned="2026-10-01T00:00:00+00:00"):
    return {"id": mid, "text": text, "fact_type": "world", "context": "c", "mentioned_at": mentioned,
            "document_id": "d", "tags": ["kind:x"], "scores": {"s": 1}}


def stored():
    return {"runs": [
        {"q": 2, "max_tokens": 4096, "results": [rrow("m4"), rrow("m1")]},
        {"q": 1, "max_tokens": 16384, "results": [rrow("m1"), rrow("m2"), rrow("m3")]},
        {"q": 1, "max_tokens": 4096, "results": [rrow("m2"), rrow("m1")]},
    ]}


def test_assemble_order_query_then_rank_and_dedupe():
    asm = D.assemble(stored())
    # 질의 순(q 1 먼저, 같은 q 안에서는 max_tokens 작은 것 먼저) -> 순위 순, 기억 id 중복 제거(처음 나온 자리)
    assert [r["id"] for r in asm["rows"]] == ["m2", "m1", "m3", "m4"]
    assert asm["truncated_rows"] == 0
    assert asm["source_rows"] == 7 and asm["union_rows"] == 4


def test_assemble_filters_max_tokens_for_report_inputs():
    asm = D.assemble(stored(), max_tokens=4096)
    assert [r["id"] for r in asm["rows"]] == ["m2", "m1", "m4"]


def test_assemble_truncates_by_char_limit_and_counts():
    full = D.assemble(stored())
    one = len(D.render_row(full["rows"][0]))
    asm = D.assemble(stored(), max_input_chars=one + 1)
    assert [r["id"] for r in asm["rows"]] == ["m2"]
    assert asm["truncated_rows"] == 3


def test_render_row_drops_scores_and_document_id():
    line = json.loads(D.render_row(rrow("m9")))
    assert line["id"] == "m9" and "scores" not in line and "document_id" not in line
    assert set(line) == {"id", "type", "mentioned_at", "tags", "context", "text"}


def test_build_prompt_uses_template_and_rows():
    asm = D.assemble(stored())
    prompt = D.build_prompt("HEAD {query_date}\n{recall_rows}\nTAIL", asm, query_date="2026-10-28")
    assert prompt.startswith("HEAD 2026-10-28\n") and prompt.endswith("\nTAIL")
    assert prompt.count('"id"') == 4


def test_baseline_prompt_file_has_placeholders_and_no_recheck_instruction():
    text = D.load_prompt(D.BASELINE_PROMPT)
    assert "{recall_rows}" in text and "{query_date}" in text
    assert "evidence_ids" in text and "residual" in text


# ---------------------------------------------------------------- 채점용 작업 표(세트 C 모양, 합성)

def test_tasks_from_answer_key_uses_item_keywords_and_categories():
    doc = {
        "closed": [{"id": "K01", "item": "X", "keywords": ["proj-a", "alpha-kw", "#901"], "project_hint": "proj-a"}],
        "controls": [{"id": "C01", "item": "Y", "keywords": ["gamma-kw"], "project_hint": None}],
        "partial": [{"id": "P01", "item": "Z", "keywords": ["pp-kw"], "project_hint": None}],
    }
    tasks = D.tasks_from_answer_key(doc)
    by = {t["id"]: t for t in tasks}
    assert by["K01"]["keywords"] == ["alpha-kw", "#901"]       # 프로젝트 이름 keyword 제외(item_keywords)
    assert by["K01"]["category"] == "closed" and by["C01"]["category"] == "controls"
    assert by["P01"]["category"] == "partial"
    assert M.match_item(item("x", key="proj-a#901"), tasks)["task"] == "K01"
    assert "item" not in by["K01"]                                # 이름은 대응에 쓰지 않는다


def test_judge_summary_from_fake_outputs():
    doc = {
        "closed": [{"id": "K01", "item": "X", "keywords": ["alpha-kw"], "project_hint": None},
                   {"id": "K02", "item": "X2", "keywords": ["beta-kw"], "project_hint": None}],
        "controls": [{"id": "C01", "item": "Y", "keywords": ["gamma-kw"], "project_hint": None}],
        "partial": [],
    }
    gens = [{"valid": True, "items": [item("alpha-kw"), item("gamma-kw"), item("무관")]} for _ in range(3)]
    gens += [{"valid": True, "items": [item("beta-kw", status="done", unverified=True)]},
             {"valid": False, "items": []}]
    s = D.judge_summary(gens, D.tasks_from_answer_key(doc))
    assert s["K"] == 5 and s["invalid"] == 1
    assert s["reproduction"]["reproduced"] is True and s["reproduction"]["items"] == ["K01"]
    assert s["open_rate"]["K01"] == 3 / 5 and s["open_rate"]["C01"] == 3 / 5
    assert s["controls_kept"] == [1, 1, 1, 0, 0]
    assert s["unmapped"] == [1, 1, 1, 0, 0]
    assert s["closed_in_list"] == [1, 1, 1, 1, 0]


# ---------------------------------------------------------------- final-review #350-1 재작업

def test_claude_caller_timeout_becomes_error_result(monkeypatch):
    import subprocess as sp

    def fake_run(cmd, **kw):
        if cmd == [D.TOKEN_CMD]:
            return sp.CompletedProcess(cmd, 0, stdout="tok\n", stderr="")
        raise sp.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(D.subprocess, "run", fake_run)
    raw = D.claude_caller(timeout=1)("P")
    assert raw["is_error"] is True and "timeout" in raw["error"]
    rec = D.generate_one("P", D.claude_caller(timeout=1))
    assert rec["valid"] is False and rec["attempts"] == 2


def test_hintless_bare_number_keyword_matches_by_name():
    tasks = [task("N1", ["#12"])]
    m = M.match_item(item("#12 작업 마무리", key="repo#12"), tasks)
    assert (m["task"], m["by"]) == ("N1", "name")
    assert M.match_item(item("#120 다른 일"), tasks)["task"] is None


@pytest.mark.parametrize("raw, want", [
    ("Owner/Repo-Name#12", "reponame#12"),
    ("owner/sub/REPO #12", "repo#12"),
    ("Repo Name #7", "reponame#7"),
    ("REPO-NAME#007", "reponame#7"),
    ("#12", None),
    ("no key", None),
])
def test_norm_key_owner_repo_case_and_spaces(raw, want):
    assert M.norm_key(raw) == want


def test_owner_repo_key_and_spaced_hint_match_task_key():
    tasks = [task("H1", ["#12"], hint="Repo Name")]
    assert M.task_keys(tasks[0]) == {"reponame#12"}
    assert M.match_item(item("무관", key="Owner/Repo-Name#12"), tasks)["task"] == "H1"


def test_assemble_counts_rows_without_id():
    s = {"runs": [{"q": 1, "max_tokens": 4096, "results": [rrow("m1"), {"text": "x"}, {"id": None, "text": "y"}]}]}
    asm = D.assemble(s)
    assert [r["id"] for r in asm["rows"]] == ["m1"]
    assert asm["no_id_rows"] == 2 and asm["source_rows"] == 3


def _agree_fixture(tmp_path, items_by_gen, ak_doc):
    gen = {"generations": [{"valid": True, "items": its} for its in items_by_gen]}
    g, ak = tmp_path / "gen.json", tmp_path / "ak.json"
    g.write_text(json.dumps(gen))
    ak.write_text(json.dumps(ak_doc))
    assert D.main(["judge", "--gen", str(g), "--answer-key", str(ak), "--out", str(tmp_path / "judge.json")]) == 0
    assert D.main(["blind", "--gen", str(g), "--out", str(tmp_path / "blind.json"),
                   "--map", str(tmp_path / "map.json")]) == 0
    mp = json.loads((tmp_path / "map.json").read_text())["map"]
    rid = {(m["gen"], m["item"]): m["rid"] for m in mp}
    return g, ak, rid


AK = {"closed": [{"id": "K01", "item": "X", "keywords": ["alpha-kw", "#901"], "project_hint": "proj-a"},
                 {"id": "K02", "item": "Y", "keywords": ["beta-kw"], "project_hint": None}],
      "controls": [], "partial": []}


def _agree(tmp_path, g, ak, readers, extra=()):
    paths = []
    for n, rd in enumerate(readers, 1):
        p = tmp_path / f"r{n}.json"
        p.write_text(json.dumps({"items": rd}))
        paths += ["--reader", str(p)]
    return D.main(["agree", "--gen", str(g), "--map", str(tmp_path / "map.json"), "--judge",
                   str(tmp_path / "judge.json"), "--answer-key", str(ak), "--out", str(tmp_path / "agree.json"),
                   *paths, *extra])


def test_agree_empty_work_and_missing_claims_are_not_counted(tmp_path):
    g, ak, rid = _agree_fixture(tmp_path, [[item("alpha-kw", key="proj-a#901"), item("beta-kw")]], AK)
    a, b = rid[(0, 0)], rid[(0, 1)]
    r1 = [{"rid": a, "work": "proj-a#901", "claims_open": True}, {"rid": b, "work": None}]
    r2 = [{"rid": a, "work": "proj-a#901", "claims_open": True}, {"rid": b, "work": None}]
    assert _agree(tmp_path, g, ak, [r1, r2]) == 0
    out = json.loads((tmp_path / "agree.json").read_text())
    assert out["a_work"]["n"] == 1 and out["a_open"]["n"] == 1
    assert out["b_open_r1"]["n"] == 1


def test_agree_reports_unread_rids(tmp_path):
    g, ak, rid = _agree_fixture(tmp_path, [[item("alpha-kw", key="proj-a#901"), item("beta-kw")]], AK)
    a = rid[(0, 0)]
    r = [{"rid": a, "work": "proj-a#901", "claims_open": True}]
    assert _agree(tmp_path, g, ak, [r, r]) == 0
    assert json.loads((tmp_path / "agree.json").read_text())["unread"] == [1, 1]


def test_agree_keyless_task_goes_manual_not_mismatch(tmp_path):
    g, ak, rid = _agree_fixture(tmp_path, [[item("beta-kw")]], AK)
    r = [{"rid": rid[(0, 0)], "work": "repo#5", "claims_open": True}]
    assert _agree(tmp_path, g, ak, [r, r]) == 0
    out = json.loads((tmp_path / "agree.json").read_text())
    assert out["b_work_r1"]["n"] == 0 and out["b_work_r1"]["manual"] == 1


def test_agree_manual_lists_not_on_stdout(tmp_path, capsys):
    g, ak, rid = _agree_fixture(tmp_path, [[item("beta-kw")]], AK)
    r1 = [{"rid": rid[(0, 0)], "work": "label:SECRET-LABEL-ONE", "claims_open": True}]
    r2 = [{"rid": rid[(0, 0)], "work": "label:SECRET-LABEL-TWO", "claims_open": True}]
    capsys.readouterr()
    assert _agree(tmp_path, g, ak, [r1, r2]) == 0
    printed = capsys.readouterr().out
    assert "SECRET-LABEL" not in printed
    manual = tmp_path / "agree-manual.json"
    assert "SECRET-LABEL-ONE" in manual.read_text()
    assert (manual.stat().st_mode & 0o777) == 0o600
    assert "SECRET-LABEL" not in (tmp_path / "agree.json").read_text()


def test_agree_rejects_mismatched_gen(tmp_path):
    g, ak, rid = _agree_fixture(tmp_path, [[item("alpha-kw")]], AK)
    other = tmp_path / "other-gen.json"
    other.write_text(json.dumps({"generations": [{"valid": True, "items": [item("alpha-kw")]}], "x": 1}))
    r = [{"rid": rid[(0, 0)], "work": "proj-a#901", "claims_open": True}]
    paths = []
    for n in (1, 2):
        p = tmp_path / f"r{n}.json"
        p.write_text(json.dumps({"items": r}))
        paths += ["--reader", str(p)]
    rc = D.main(["agree", "--gen", str(other), "--map", str(tmp_path / "map.json"), "--judge",
                 str(tmp_path / "judge.json"), "--answer-key", str(ak), "--out", str(tmp_path / "agree.json"), *paths])
    assert rc == 1
