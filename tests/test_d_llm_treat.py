"""issue #352 R4-3 - d-llm 처치 변형(입력 추가·이름 recall 라운드로빈·근거 주입·제거)과 처치 프롬프트.

#349 "처치(층별)"·"T1-lite 판정"과 사전 등록 v4 2부 2.4(코디네이터 결정 A)가 정본이다:
기준선 입력(상한 적용 결과)을 그대로 앞에 두고 추가 행(id 중복 제거)을 뒤에 붙인다. 상한은 기준선 몫에만 적용한다.
T1b 이름 recall 은 후보 순서 라운드로빈으로 순위 1행씩 넣고 추가분 상한(100,000자)에서 멈춘다.
근거 주입은 뒤에 붙이고, 제거는 기준선 입력에서 id 를 지운다(잘린 행은 돌아오지 않는다).
LLM 호출은 가짜 주입으로만 한다(네트워크 0). recall 행·목록 출력은 전부 합성이다.
"""
import hashlib
import json
import os

import pytest

from bench import d_llm as D
from bench import d_llm_stats as S


def rrow(mid, text="t"):
    return {"id": mid, "text": text, "fact_type": "world", "context": "c",
            "mentioned_at": "2026-10-01T00:00:00+00:00", "tags": ["kind:x"]}


def store(*runs):
    """runs: 질의 순서대로 결과 행 id 목록."""
    return {"runs": [{"q": n, "max_tokens": 16384, "results": [rrow(i) for i in ids]}
                     for n, ids in enumerate(runs, 1)]}


BASE = store(["b1", "b2"], ["b2", "b3", "b4"])        # 합집합 b1 b2 b3 b4
ROW = len(D.render_row(rrow("b1"))) + 1                 # 합성 행 한 줄 글자 수(모든 합성 행이 같다)


def ids(asm):
    return [r["id"] for r in asm["rows"]]


# ---------------------------------------------------------------- 조립 규칙(함수 표면)

def test_no_treatment_is_exactly_baseline_assemble():
    for cap in (None, 3 * ROW):
        a, b = D.assemble_treat(BASE, max_input_chars=cap), D.assemble(BASE, max_input_chars=cap)
        assert a == b and "treatment" not in a


def test_add_keeps_capped_baseline_prefix_and_appends_new_rows_uncapped():
    add = store(["b2", "d1"], ["d2", "b4", "d1"])
    asm = D.assemble_treat(BASE, max_input_chars=3 * ROW, add_stored=add)
    # 기준선 상한 3행(b1 b2 b3, b4 잘림) 그대로 앞, 추가 질의 순 -> 순위 순 새 행이 뒤. 추가분에는 상한 없음.
    # 기준선에서 잘린 b4 는 추가 질의에도 나오므로 새 행으로 들어간다(모델 입력에 없던 행).
    assert ids(asm) == ["b1", "b2", "b3", "d1", "d2", "b4"]
    t = asm["treatment"]
    assert t["base_input_rows"] == 3 and t["base_truncated_rows"] == 1
    assert t["added_rows"] == 3 and t["added_chars"] == 3 * ROW
    assert asm["chars"] == 6 * ROW and asm["truncated_rows"] == 1


def test_names_round_robin_rank_by_rank_in_candidate_order():
    names = store(["n1", "n2", "n3"], ["b1", "m2"], ["k1"])
    asm = D.assemble_treat(BASE, add_names=names, names_cap=100 * ROW)
    # 순위 1: n1, (b1 는 이미 있음), k1 / 순위 2: n2, m2 / 순위 3: n3
    assert ids(asm)[4:] == ["n1", "k1", "n2", "m2", "n3"]
    assert asm["treatment"]["names_added_rows"] == 5 and asm["treatment"]["names_unadded_rows"] == 0


def test_names_stop_at_cap_and_count_unadded():
    names = store(["n1", "n2", "n3"], ["m1", "m2"])
    asm = D.assemble_treat(BASE, add_names=names, names_cap=3 * ROW + 1)
    assert ids(asm)[4:] == ["n1", "m1", "n2"]
    t = asm["treatment"]
    assert t["names_added_rows"] == 3 and t["names_added_chars"] == 3 * ROW
    assert t["names_unadded_rows"] == 2 and t["names_cap"] == 3 * ROW + 1


def test_names_stop_is_final_even_if_a_later_row_would_fit():
    big = dict(rrow("n1"), text="x" * 50)
    names = {"runs": [{"q": 1, "results": [big, rrow("n2")]}, {"q": 2, "results": [rrow("m1")]}]}
    asm = D.assemble_treat(BASE, add_names=names, names_cap=ROW + 5)
    # 첫 행(n1)이 상한을 넘으면 거기서 멈춘다(뒤의 짧은 행을 골라 넣지 않는다)
    assert ids(asm) == ["b1", "b2", "b3", "b4"]
    assert asm["treatment"]["names_added_rows"] == 0 and asm["treatment"]["names_unadded_rows"] == 3


def test_inject_appends_and_dedupes():
    asm = D.assemble_treat(BASE, inject_rows=[rrow("e1"), rrow("b2"), rrow("e1"), rrow("e2")])
    assert ids(asm) == ["b1", "b2", "b3", "b4", "e1", "e2"]
    assert asm["treatment"]["injected_rows"] == 2


def test_drop_removes_from_capped_baseline_and_truncated_rows_do_not_return():
    asm = D.assemble_treat(BASE, max_input_chars=3 * ROW, drop_ids=["b2", "zz"])
    assert ids(asm) == ["b1", "b3"]
    t = asm["treatment"]
    assert t["dropped_rows"] == 1 and t["drop_ids"] == 2 and t["base_truncated_rows"] == 1


def test_dropped_ids_are_not_readded_by_add_names_or_inject():
    asm = D.assemble_treat(BASE, drop_ids=["b3"], add_stored=store(["b3", "d1"]),
                           add_names=store(["b3", "n1"]), inject_rows=[rrow("b3"), rrow("e1")])
    assert "b3" not in ids(asm)
    assert ids(asm) == ["b1", "b2", "b4", "d1", "n1", "e1"]


def test_rows_without_id_are_never_added():
    names = {"runs": [{"q": 1, "results": [{"text": "no id"}, rrow("n1")]}]}
    asm = D.assemble_treat(BASE, add_names=names, inject_rows=[{"text": "x"}], add_stored=names)
    assert ids(asm) == ["b1", "b2", "b3", "b4", "n1"]


# ---------------------------------------------------------------- 함수 x CLI run 표면 행렬

CASES = {
    "none": ({}, ["b1", "b2", "b3"]),
    "add": ({"add": store(["d1", "b4"])}, ["b1", "b2", "b3", "d1", "b4"]),
    "names": ({"names": store(["n1", "n2"], ["m1"])}, ["b1", "b2", "b3", "n1", "m1", "n2"]),
    "inject": ({"inject": [rrow("e1")]}, ["b1", "b2", "b3", "e1"]),
    "drop": ({"drop": ["b1"]}, ["b2", "b3"]),
    "drop+inject": ({"drop": ["b1"], "inject": [rrow("e1"), rrow("b1")]}, ["b2", "b3", "e1"]),
}


def fake_caller(prompts):
    def call(prompt):
        prompts.append(prompt)
        return {"result": json.dumps({"items": []}), "usage": {}, "is_error": False}
    return call


def prompt_ids(prompt):
    body = prompt.split("<recall_results>\n", 1)[1].split("\n</recall_results>", 1)[0]
    return [json.loads(line)["id"] for line in body.splitlines()]


@pytest.mark.parametrize("surface", ["function", "cli"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_treatment_matrix_function_and_cli(case, surface, tmp_path, monkeypatch):
    opts, want = CASES[case]
    cap = 3 * ROW
    if surface == "function":
        asm = D.assemble_treat(BASE, max_input_chars=cap, add_stored=opts.get("add"), add_names=opts.get("names"),
                               names_cap=D.R43_NAMES_ADD_CAP, inject_rows=opts.get("inject"),
                               drop_ids=opts.get("drop"))
        assert ids(asm) == want
        return
    inp = tmp_path / "base.json"
    inp.write_text(json.dumps(BASE))
    argv = ["run", "--input", str(inp), "--out", str(tmp_path), "--k", "2", "--label", case,
            "--max-input-chars", str(cap)]
    if "add" in opts:
        (tmp_path / "add.json").write_text(json.dumps(opts["add"]))
        argv += ["--add", str(tmp_path / "add.json")]
    if "names" in opts:
        (tmp_path / "names.json").write_text(json.dumps(opts["names"]))
        argv += ["--add-names", str(tmp_path / "names.json")]
    if "inject" in opts:
        (tmp_path / "inject.json").write_text(json.dumps({"rows": opts["inject"]}))
        argv += ["--inject", str(tmp_path / "inject.json")]
    if "drop" in opts:
        (tmp_path / "drop.json").write_text(json.dumps({"ids": opts["drop"]}))
        argv += ["--drop-ids", str(tmp_path / "drop.json")]
    prompts = []
    monkeypatch.setattr(D, "claude_caller", lambda model: fake_caller(prompts))
    assert D.main(argv) == 0
    assert len(prompts) == 2 and all(prompt_ids(p) == want for p in prompts)
    head = json.loads((tmp_path / f"{case}-gen.json").read_text())
    assert head["input_rows"] == len(want)
    if case == "none":
        # 처치 옵션이 없으면 R4-1 기준선 run 과 같은 머리(처치 칸 없음)
        assert "treatment" not in head and "input_ids" not in head
    else:
        assert head["input_ids"] == want
        assert head["treatment"]["base_input_rows"] == 3
        for opt, fld in (("add", "add_sha256"), ("names", "names_sha256"), ("inject", "inject_sha256"),
                         ("drop", "drop_sha256")):
            assert (head["treatment"].get(fld) is not None) == (opt in opts)


def test_run_rejects_treatment_file_without_expected_shape(tmp_path, monkeypatch):
    inp = tmp_path / "base.json"
    inp.write_text(json.dumps(BASE))
    (tmp_path / "inject.json").write_text(json.dumps([rrow("e1")]))
    monkeypatch.setattr(D, "claude_caller", lambda model: fake_caller([]))
    assert D.main(["run", "--input", str(inp), "--out", str(tmp_path), "--inject", str(tmp_path / "inject.json")]) == 1


# ---------------------------------------------------------------- 인과 진단 입력 파일(diag)

def gitem(name, status="open", evidence=(), key=None):
    it = {"name": name, "status": status, "evidence_ids": list(evidence), "quote": ""}
    if key:
        it["key"] = key
    return it


SETD = {"closed": [{"id": "W01", "keywords": ["aa-kw"], "project_hint": None},
                   {"id": "W02", "keywords": ["bb-kw"], "project_hint": None}],
        "controls": [{"id": "W03", "keywords": ["cc-kw"], "project_hint": None}], "partial": [], "unknown": []}


def test_diag_inputs_inject_done_rows_and_drop_stale_open_evidence():
    gen = {"generations": [
        {"valid": True, "items": [gitem("aa-kw 남음", evidence=["s1", "s2", "d1"]),
                                  gitem("bb-kw", status="done", evidence=["s3"]),
                                  gitem("cc-kw", evidence=["c1"]), gitem("무관", evidence=["u1"])]},
        {"valid": True, "items": [gitem("bb-kw 열림", evidence=["s4"])]},
        {"valid": False, "items": [gitem("aa-kw", evidence=["bad"])]},
    ]}
    stores = [store(["x1", "d1"]), store(["d2", "d1"])]
    done = {"tasks": {"W01": ["d1", "dz"], "W02": ["d2"], "W03": ["c9"]}}
    out = D.diag_inputs(SETD, gen, done, stores)
    # 주입: 끝난 일(세트 D closed 순)의 완료 근거 행 전체(저장 recall 에서 처음 찾은 행). 없는 id 는 missing.
    assert [r["id"] for r in out["inject"]] == ["d1", "d2"]
    assert out["missing"] == ["dz"]
    # 제거: 기준선 유효 반복에서 끝난 일에 대응된 open 항목의 evidence_ids 합집합, 완료 근거 행은 빼고. 대조군·done·무관 항목은 제외
    assert out["drop"] == ["s1", "s2", "s4"]
    assert out["per_task"]["W01"] == {"done_rows": 2, "inject": 1, "stale": 2}
    assert out["per_task"]["W02"] == {"done_rows": 1, "inject": 1, "stale": 1}


def test_diag_cli_writes_private_files(tmp_path):
    gen = {"generations": [{"valid": True, "items": [gitem("aa-kw", evidence=["s1"])]}]}
    for n, obj in (("set", SETD), ("gen", gen), ("done", {"tasks": {"W01": ["d1"]}}), ("st", store(["d1"]))):
        (tmp_path / f"{n}.json").write_text(json.dumps(obj))
    rc = D.main(["diag", "--set", str(tmp_path / "set.json"), "--gen", str(tmp_path / "gen.json"),
                 "--done-rows", str(tmp_path / "done.json"), "--rows-from", str(tmp_path / "st.json"),
                 "--out-inject", str(tmp_path / "inj.json"), "--out-drop", str(tmp_path / "drop.json")])
    assert rc == 0
    assert [r["id"] for r in json.loads((tmp_path / "inj.json").read_text())["rows"]] == ["d1"]
    assert json.loads((tmp_path / "drop.json").read_text())["ids"] == ["s1"]
    assert oct(os.stat(tmp_path / "inj.json").st_mode & 0o777) == "0o600"
    # 이미 있으면 덮어쓰지 않는다
    assert D.main(["diag", "--set", str(tmp_path / "set.json"), "--gen", str(tmp_path / "gen.json"),
                   "--done-rows", str(tmp_path / "done.json"), "--rows-from", str(tmp_path / "st.json"),
                   "--out-inject", str(tmp_path / "inj.json"), "--out-drop", str(tmp_path / "d2.json")]) == 1


# ---------------------------------------------------------------- 처치 프롬프트 파일

BASELINE_SHA = "2448cb932f81624bfed3fd9d671f6ff2e5ff89c5a379d3fce407aa88d30034ab"


def test_baseline_prompt_still_frozen():
    with open(D.BASELINE_PROMPT, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == BASELINE_SHA


@pytest.mark.parametrize("path, marker", [
    (D.T1LITE_PROMPT, "unverified"),
    (D.T1A_PROMPT, "확인"),
])
def test_treatment_prompt_is_baseline_plus_one_instruction(path, marker):
    base = D.load_prompt(D.BASELINE_PROMPT)
    text = D.load_prompt(path)
    mark = "<recall_results>\n{recall_rows}"
    head, tail = base.split(mark, 1)
    # 기준선 문안 전체를 그대로 두고 recall 행 블록 앞에 지시 한 문단만 더한다
    assert text.startswith(head) and text.endswith(mark + tail)
    extra = text[len(head):len(text) - len(mark + tail)]
    assert extra.strip() and marker in extra
    asm = D.assemble(store(["b1"]))
    assert '"id": "b1"' in D.build_prompt(text, asm)


def test_t1_lite_unverified_open_counts_as_open_in_stats():
    # T1-lite 의 unverified 는 표시라 주 통계량에서 open 으로 센다(#349 "T1-lite 판정")
    sd = {"closed": [{"id": "W01", "keywords": ["aa-kw"], "project_hint": None}], "controls": [], "partial": []}
    g = {"generations": [{"valid": True, "items": [dict(gitem("aa-kw"), unverified=True)]}]}
    tasks = S.tasks_of(sd)
    assert S.open_flags(g, tasks)["W01"] == [1]
    assert S.open_flags(g, tasks, strict_unverified=True)["W01"] == [0]


# ---------------------------------------------------------------- final-review #352-1 재작업

@pytest.mark.parametrize("opt, doc", [
    ("--inject", {"rows": ["m1"]}),                 # 행이 아니라 id 문자열
    ("--drop-ids", {"ids": [{"id": "b1"}]}),        # id 가 아니라 객체
    ("--add", {"runs": ["x"]}),
    ("--add-names", {"runs": [1]}),
])
def test_run_refuses_treatment_file_with_wrong_item_types(opt, doc, tmp_path, monkeypatch):
    inp = tmp_path / "base.json"
    inp.write_text(json.dumps(BASE))
    (tmp_path / "t.json").write_text(json.dumps(doc))
    monkeypatch.setattr(D, "claude_caller", lambda model: fake_caller([]))
    assert D.main(["run", "--input", str(inp), "--out", str(tmp_path), opt, str(tmp_path / "t.json")]) == 1


def test_diag_refuses_same_inject_and_drop_path(tmp_path):
    gen = {"generations": [{"valid": True, "items": [gitem("aa-kw", evidence=["s1"])]}]}
    for n, obj in (("set", SETD), ("gen", gen), ("done", {"tasks": {"W01": ["d1"]}}), ("st", store(["d1"]))):
        (tmp_path / f"{n}.json").write_text(json.dumps(obj))
    same = str(tmp_path / "same.json")
    rc = D.main(["diag", "--set", str(tmp_path / "set.json"), "--gen", str(tmp_path / "gen.json"),
                 "--done-rows", str(tmp_path / "done.json"), "--rows-from", str(tmp_path / "st.json"),
                 "--out-inject", same, "--out-drop", same])
    assert rc == 1 and not os.path.exists(same)
