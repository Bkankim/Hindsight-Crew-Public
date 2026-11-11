"""bench/r5_roles.py - 런 5 역할 구분 원장 오라클(issue #372, 상위 #360 "R5-A3 역할 구분 원장 오라클").

가짜 입력만 쓴다(네트워크 0, LLM 호출은 가짜 호출기 주입). 키·문장·기억은 전부 합성이다(실제 항목 이름 없음).
"""
from __future__ import annotations

import ast
import json
import os
import stat
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from bench import r5_oracle as O
from bench import r5_roles as X

REPO = Path(__file__).resolve().parent.parent
ALIASES = {"hc": "app"}
T = "2026-10-01"
TAG_ALIASES = {"kind:done": "kind:completion", "kind:work-log": "kind:completion"}


def at(day: str, hh: int = 12, mm: int = 0, ss: int = 0, us: int = 0) -> str:
    d = datetime(*map(int, day.split("-")), hh, mm, ss, us, tzinfo=X.KST)
    return d.astimezone(timezone.utc).isoformat()


def meta(mid, keys, day="2026-09-20", hh=12, tags=("project:app",), state="valid", completion=False):
    tg = list(tags) + (["kind:completion"] if completion else [])
    return {"id": mid, "keys": list(keys), "mentioned_at": at(day, hh) if day else None, "tags": tg, "state": state}


def parsed_of(rows):
    """[(id, 본문, 태그)] -> parse 결과 {id: memory_mentions}."""
    return {mid: X.memory_mentions(mid, text, tags, ALIASES) for mid, text, tags in rows}


# --- 문장 역할 -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sentence, label, first, want", [
    ("app#1 후속으로 남김", None, False, X.OPEN),                 # 열림 표지어
    ("app#1 진행 중", None, False, X.OPEN),
    ("app#1 다음 단계에서 본다", None, False, X.OPEN),
    ("app#1 pending", None, False, X.OPEN),
    ("app#1 머지 완료", None, False, X.DONE),                     # 끝냄 표지어
    ("app#1 closed", None, False, X.DONE),
    ("app#1 미완료", None, False, X.OPEN),                        # 미완료 = 열림(끝냄 낱말을 품어도)
    ("app#1 완료, app#2 남은 일", None, False, X.OPEN),           # 한 문장에 둘 다 = 열림
    ("app#1 참고", None, False, X.REF),                           # 표지어 없음 = 참조
    ("다음과 같이 app#1 을 본다", None, False, X.REF),            # '다음과'는 열림이 아니다
    ("[완료] app#1 정리", "완료", True, X.DONE),                  # 표지어 없는 라벨 문장 = 라벨
    ("[진행] app#1 정리", "진행", True, X.OPEN),
    ("[결정] app#1 정리", "결정", True, X.REF),
    ("app#1 정리", "완료", False, X.REF),                         # 라벨은 첫 문장에만
    ("[완료] app#1 후속 남음", "완료", True, X.OPEN),             # 문장 표지어가 라벨보다 먼저
])
def test_sentence_role_markers_label_sentence_unit(sentence, label, first, want):
    assert X.sentence_role(sentence, label, first) == want


def test_label_word_is_not_a_sentence_marker():
    """라벨 안 낱말(`[진행]`)은 문장 표지어로 세지 않고 라벨 규칙으로만 본다."""
    assert X.sentence_role("[결정 진행] app#1 정리", "결정 진행", True) == X.OPEN   # 라벨 열림 낱말
    assert X.sentence_role("[완료] app#1 정리", "완료", True) == X.DONE


def test_split_sentences_newline_and_terminators_and_fence():
    text = "[완료] app#1 머지. app#2 후속 남음!\n- app#3 참고\n```\napp#9 끝\n```"
    assert X.split_sentences(text) == ["[완료] app#1 머지.", "app#2 후속 남음!", "- app#3 참고"]


def test_memory_mentions_multi_key_per_sentence_and_per_memory():
    p = X.memory_mentions("m1", "[완료] app#1 app#2 머지. app#2 후속 남음. app#3 참고.", ["project:app"], ALIASES)
    roles = {(m["key"], m["sent"]): m["role"] for m in p["mentions"]}
    assert roles == {("app#1", 0): X.DONE, ("app#2", 0): X.DONE, ("app#2", 1): X.OPEN, ("app#3", 2): X.REF}
    assert X.memory_roles(p["mentions"]) == {"m1": {"app#1": X.DONE, "app#2": X.OPEN, "app#3": X.REF}}  # 열림이 이긴다
    assert p["keys"] == ["app#1", "app#2", "app#3"]


def test_item_tag_only_key_is_reference_without_sentence():
    p = X.memory_mentions("m1", "[완료] app#1 끝냄", ["project:app", "item:app-7"], ALIASES)
    tag = [m for m in p["mentions"] if m["key"] == "app#7"]
    assert tag == [{"mid": "m1", "key": "app#7", "sent": None, "role": X.REF, "source": "item_tag"}]
    assert "app#7" in p["keys"]


def test_bare_number_uses_project_tag_and_item_tag_not_passed_to_sentences():
    p = X.memory_mentions("m1", "#5 진행 중", ["project:app", "item:app-5"], ALIASES)
    assert [(m["key"], m["sent"], m["role"]) for m in p["mentions"]] == [("app#5", 0, X.OPEN)]


@pytest.mark.parametrize("roles, want", [
    ([X.REF, X.DONE, X.OPEN], X.OPEN), ([X.REF, X.DONE], X.DONE), ([X.REF], X.REF), ([X.DONE, X.OPEN], X.OPEN),
])
def test_merge_roles_open_wins(roles, want):
    assert X.merge_roles(roles) == want


def test_parse_all_key_mismatch_count():
    m = [meta("m1", ["app#1"]), meta("m2", ["app#2", "app#9"])]
    parsed, km = X.parse_all(m, {"m1": "app#1 끝냄", "m2": "app#2 끝냄"}, ALIASES)
    assert km == {"memories": 2, "key_mismatch_memories": 1, "keys_only_in_sentences": 0, "keys_only_in_meta": 1}


# --- 최신 비참조 규칙·시각 필터 -----------------------------------------------------------------------------

def states(rows, metas, t=T):
    by = {m["id"]: m for m in metas}
    return X.role_states(X.memory_roles(m for p in parsed_of(rows).values() for m in p["mentions"]), by, t)


def test_latest_nonref_mention_wins_and_ref_is_skipped():
    rows = [("a", "app#1 머지 완료", []), ("b", "app#1 후속 남음", []), ("c", "app#1 참고", [])]
    metas = [meta("a", ["app#1"], "2026-09-10"), meta("b", ["app#1"], "2026-09-20"), meta("c", ["app#1"], "2026-09-30")]
    assert states([(i, t, ["project:app"]) for i, t, _ in rows], metas)["app#1"] == \
        {"state": X.S_OPEN, "date": "2026-09-20", "id": "b"}


@pytest.mark.parametrize("later_day, later_hh, later_us, counted", [
    ("2026-10-01", 23, 59, True),          # t 끝 시각 직전
    ("2026-10-02", 0, 0, False),           # t 다음날 0시 KST
    ("2026-10-05", 12, 0, False),
])
def test_time_filter_mentioned_at_le_t_end(later_day, later_hh, later_us, counted):
    rows = [("a", "app#1 후속 남음", ["project:app"]), ("b", "app#1 머지 완료", ["project:app"])]
    late = meta("b", ["app#1"], later_day, later_hh)
    if later_us:
        late["mentioned_at"] = at(later_day, later_hh, 59, 59, 999999)
    metas = [meta("a", ["app#1"], "2026-09-20"), late]
    assert states(rows, metas)["app#1"]["state"] == (X.S_DONE if counted else X.S_OPEN)


def test_same_time_different_memories_open_wins():
    rows = [("a", "app#1 머지 완료", ["project:app"]), ("b", "app#1 후속 남음", ["project:app"])]
    metas = [meta("a", ["app#1"], "2026-09-20"), meta("b", ["app#1"], "2026-09-20")]
    assert states(rows, metas)["app#1"] == {"state": X.S_OPEN, "date": "2026-09-20", "id": "b"}


def test_all_reference_is_undetermined_and_invalid_memory_ignored():
    rows = [("a", "app#1 참고", ["project:app"]), ("b", "app#1 머지 완료", ["project:app"])]
    metas = [meta("a", ["app#1"], "2026-09-20"), meta("b", ["app#1"], "2026-09-25", state="invalidated")]
    assert states(rows, metas)["app#1"] == {"state": X.S_UNDET, "date": "2026-09-20", "id": "a"}


def test_same_memory_done_and_open_is_open():
    rows = [("a", "app#1 머지 완료. app#1 후속 남음.", ["project:app"])]
    assert states(rows, [meta("a", ["app#1"])])["app#1"]["state"] == X.S_OPEN


# --- o3a·o3b ------------------------------------------------------------------------------------------------

def test_o3_variant_plain_matches_r5a2_status_table():
    metas = [meta("a", ["app#1", "app#2"], "2026-09-10", completion=True), meta("b", ["app#2"], "2026-09-20"),
             meta("c", ["app#3"], "2026-09-25"), meta("d", ["app#4"], "2026-10-03", completion=True)]
    mine = X.o3_variant_states(metas, T, TAG_ALIASES, X.variant_pred("o3", TAG_ALIASES))
    theirs = {r["key"]: {"state": r["state"], "date": r["date"], "id": r["id"]}
              for r in O.status_table(metas, ["app"], date.fromisoformat(T), TAG_ALIASES)}
    assert mine == theirs


@pytest.mark.parametrize("keys, want", [(["app#1"], X.S_DONE), (["app#1", "app#2"], X.S_OPEN)])
def test_o3a_single_key_completion_only(keys, want):
    metas = [meta("a", keys, "2026-09-10", completion=True)]
    st = X.o3_variant_states(metas, T, TAG_ALIASES, X.variant_pred("o3a", TAG_ALIASES))
    assert st["app#1"]["state"] == want


def test_o3a_uses_tag_aliases_for_completion_family():
    metas = [meta("a", ["app#1"], "2026-09-10", tags=("project:app", "kind:work-log"))]
    assert X.o3_variant_states(metas, T, TAG_ALIASES, X.variant_pred("o3a", TAG_ALIASES))["app#1"]["state"] == X.S_DONE
    assert X.o3_variant_states(metas, T, {}, X.variant_pred("o3a", {}))["app#1"]["state"] == X.S_OPEN


@pytest.mark.parametrize("text, want", [
    ("[완료] app#1 머지. app#2 남은 일로 둔다.", {"app#1": X.S_DONE, "app#2": X.S_OPEN}),   # 같은 문장 기준
    ("[완료] app#1 머지, app#2 후속.", {"app#1": X.S_OPEN, "app#2": X.S_OPEN}),             # 한 문장 = 둘 다 제외
    ("[완료] app#1 app#2 머지.", {"app#1": X.S_DONE, "app#2": X.S_DONE}),
    ("[완료] app#1 머지. 다음 일정 미정.", {"app#1": X.S_DONE}),                           # 키 없는 문장의 표지어는 무관
])
def test_o3b_excludes_completion_with_open_marker_in_same_sentence(text, want):
    keys = sorted(want)
    metas = [meta("a", keys, "2026-09-10", completion=True)]
    parsed = parsed_of([("a", text, metas[0]["tags"])])
    st = X.o3_variant_states(metas, T, TAG_ALIASES, X.variant_pred("o3b", TAG_ALIASES, parsed))
    assert {k: v["state"] for k, v in st.items()} == want


# --- 표 렌더·최근 끝남 N일 ---------------------------------------------------------------------------------

ST = {"app#1": {"state": X.S_OPEN, "date": "2026-09-01", "id": "a"},
      "app#2": {"state": X.S_DONE, "date": "2026-09-25", "id": "b"},     # t-6 = 포함
      "app#3": {"state": X.S_DONE, "date": "2026-09-24", "id": "c"},     # t-7 = 빠짐
      "app#4": {"state": X.S_UNDET, "date": "2026-09-30", "id": "d"},
      "other#1": {"state": X.S_OPEN, "date": "2026-09-30", "id": "e"}}   # 범위 밖


@pytest.mark.parametrize("form, want", [
    ("open", ["app#1", "app#2"]),
    ("full", ["app#1", "app#2", "app#3", "app#4"]),
])
def test_table_rows_open_only_recent_done_n_days_and_scope(form, want):
    assert [r["key"] for r in X.table_rows(ST, ["app"], T, form)] == want


def test_recent_done_window_is_n_days():
    assert X.RECENT_DONE_DAYS == 7
    assert [r["key"] for r in X.table_rows(ST, ["app"], T, "open", n_days=8)] == ["app#1", "app#2", "app#3"]


def test_render_table_lines():
    txt = X.render_table(X.table_rows(ST, ["app"], T, "open"), T, "open")
    lines = txt.splitlines()
    assert lines[0].startswith("<item_status_table>") and "최근 7일" in lines[0] and lines[-1] == "</item_status_table>"
    assert lines[1:-1] == ["app#1 | 열림 | 2026-09-01 | a", "app#2 | 끝남 | 2026-09-25 | b"]
    assert "미정" in X.render_table([], T, "full").splitlines()[0]


def test_o1c_annotates_every_row_of_done_keys_only():
    rows = [{"id": "r1", "text": "app#2 진행", "tags": ["project:app"]},
            {"id": "r2", "text": "app#1 진행", "tags": ["project:app"]},
            {"id": "r3", "text": "[완료] app#2 app#3", "tags": ["project:app", "kind:completion"]}]

    def rk(r):
        return {"%s#%d" % k for k in X.extract_keys(r["text"], r["tags"], ALIASES)}
    out, st = X.o1c(rows, ST, rk)
    assert out[0]["text"] == "[항목 상태: app#2 끝남 2026-09-25 근거 b] app#2 진행"
    assert out[1] == rows[1]
    assert out[2]["text"].startswith("[항목 상태: app#2 끝남 2026-09-25 근거 b] [항목 상태: app#3 끝남 2026-09-24 근거 c]")
    assert st == {"rows": 3, "annotated_rows": 2, "annotated_keys": 2}
    assert rows[0]["text"] == "app#2 진행"                       # 입력은 바꾸지 않는다


# --- LLM 역할 판정(가짜 호출기) ----------------------------------------------------------------------------

def items(n):
    return [{"i": i, "mid": f"m{i}", "key": "app#1", "sent": 0, "sentence": "s", "rule": X.REF} for i in range(1, n + 1)]


def fake_caller(script):
    """호출마다 script 에서 하나씩 꺼내 {i: role} 을 JSON 결과로 돌려준다."""
    seen = []

    def call(prompt):
        seen.append(prompt)
        roles = script.pop(0)
        return {"result": "답:\n" + json.dumps([{"i": i, "role": r} for i, r in roles.items()]),
                "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5}}
    call.seen = seen
    return call


def test_llm_roles_batches_retry_missing_and_fallback_ref():
    call = fake_caller([{1: "open", 2: "done"}, {3: "ref"}, {4: "done"}, {}])
    res = X.llm_roles(items(4), call, batch=3)
    assert res["roles"] == {"1": "open", "2": "done", "3": "ref", "4": "done"}
    assert res["fallback_ref"] == 0 and len(res["calls"]) == 3 and res["cost_usd"] == pytest.approx(0.03)
    assert '"i": 3' in call.seen[1] and '"i": 1' not in call.seen[1]          # 재시도는 빠진 항목만


def test_llm_roles_missing_after_retry_is_fallback_and_budget_stops():
    res = X.llm_roles(items(2), fake_caller([{1: "open"}, {}]), batch=2)
    assert res["fallback_ref"] == 1
    lookup = X.llm_role_lookup(items(2), res)
    assert lookup({"mid": "m2", "key": "app#1", "sent": 0}) == X.REF
    assert lookup({"mid": "m1", "key": "app#1", "sent": 0}) == X.OPEN
    assert lookup({"mid": "m1", "key": "app#1", "sent": None}) == X.REF   # item 태그 언급
    stop = X.llm_roles(items(4), fake_caller([{1: "open", 2: "open"}]), batch=2, budget=lambda s: s < 0.005)
    assert stop["stopped"] and stop["roles"] == {"1": "open", "2": "open"} and stop["fallback_ref"] is None


@pytest.mark.parametrize("text, want", [
    ('[{"i": 1, "role": "open"}, {"i": 9, "role": "done"}]', {1: "open"}),     # 요청 밖 번호는 버린다
    ('[{"i": 1, "role": "maybe"}]', {}),
    ("not json", {}),
    ('설명 [{"i": 2, "role": "ref"}] 끝', {2: "ref"}),
])
def test_parse_roles(text, want):
    assert X.parse_roles(text, [1, 2]) == want


def test_role_prompt_has_sentence_and_key_only():
    p = X.role_prompt([{"i": 1, "key": "app#1", "sentence": "app#1 후속", "mid": "secret-id", "rule": "open"}])
    assert '"key": "app#1"' in p and "app#1 후속" in p and "secret-id" not in p and '"rule"' not in p


def test_agreement_confusion():
    a = X.agreement(["open", "done", "ref"], ["open", "ref", "ref"])
    assert a["agree"] == 2 and a["n"] == 3 and a["confusion"] == {"done->ref": 1, "open->open": 1, "ref->ref": 1}


# --- R_bank·지출 ----------------------------------------------------------------------------------------

def test_rbank_split_denominator_and_excluded_counts():
    p = [("app#1", T), ("app#2", T), ("app#3", T), ("app#4", T), ("app#5", T)]
    st = {p[0]: X.S_OPEN, p[1]: X.S_OPEN, p[2]: X.S_DONE, p[3]: X.S_UNDET, p[4]: None}
    r = X.rbank_split(st, {p[0]: "open", p[1]: "not_open", p[2]: "open"}, p)
    assert r == {"R_bank": 0.5, "R_bank_num": 1, "R_bank_den": 2, "label_bank_mismatch": 1, "no_bank_mention": 2,
                 "undetermined": 1}


def test_spent_total_sums_dllm_and_role_llm(tmp_path):
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "x-2026-10-01-gen.json").write_text(json.dumps(
        {"generations": [{"calls": [{"cost_usd": 1.5}, {"cost_usd": 0.5}]}]}))
    (tmp_path / "roles-llm.json").write_text(json.dumps({"cost_usd": 0.25}))
    (tmp_path / "other.json").write_text(json.dumps({"cost_usd": 9}))
    total, parts = X.spent_total(str(tmp_path))
    assert total == pytest.approx(2.25) and parts == {"dllm": 2.0, "roles_llm": 0.25}


# --- 봉인 보호·입력 경계 -------------------------------------------------------------------------------------

def test_body_targets_keyed_and_le_dev_last_t():
    m = [meta("a", ["app#1"], "2026-10-13", 23), meta("b", ["app#1"], "2026-10-14", 0), meta("c", [], "2026-09-01"),
         meta("d", ["app#1"], None)]
    m[0]["mentioned_at"] = at("2026-10-13", 23, 59, 59, 999999)
    assert sorted(X.body_targets(m)) == ["a"]


def test_load_bodies_refuses_after_cutoff_and_writes_private(tmp_path):
    ok = tmp_path / "b.jsonl"
    X.write_private_text(str(ok), json.dumps({"id": "a", "text": "x", "mentioned_at": at("2026-10-13")}) + "\n")
    assert stat.S_IMODE(os.stat(ok).st_mode) == 0o600
    assert X.load_bodies(str(ok)) == {"a": "x"}
    bad = tmp_path / "c.jsonl"
    bad.write_text(json.dumps({"id": "b", "text": "x", "mentioned_at": at("2026-10-14")}) + "\n")
    with pytest.raises(X.Fail):
        X.load_bodies(str(bad))


def test_tables_refuses_sealed_window_t(tmp_path):
    lab = tmp_path / "l.jsonl"
    lab.write_text("".join(json.dumps(r) + "\n" for r in [
        {"key": "app#1", "t": "2026-10-13", "label": "control", "split": "dev"},
        {"key": "app#1", "t": "2026-10-25", "label": "control", "split": "sealed"}]))
    rc = X.main(["tables", "--labelset", str(lab), "--aliases", "x", "--meta", "x", "--mentions", "x", "--plan", "x",
                 "--families", "x", "--tag-aliases", "x", "--recalls", "x", "--t", "2026-10-19", "--out", "x"])
    assert rc == 1


# --- import 검사: GitHub·라벨셋·라벨 쪽 규칙이 상태 판정 입력에 들지 않는다 ---------------------------------

def _imports(mod):
    tree = ast.parse((REPO / mod).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {(node.module, a.name) for a in node.names}
        if isinstance(node, ast.Import):
            names |= {(a.name, None) for a in node.names}
    return names


def test_roles_module_imports_no_label_rules_and_only_key_and_s1_completion_from_gateway():
    names = _imports("bench/r5_roles.py")
    assert {n for n in names if (n[0] or "").startswith("gateway")} <= {
        ("gateway.item_keys", "extract_keys"), ("gateway.item_status", "is_completion_family")}, names
    assert not any("r5_labels" in (m or "") or a == "r5_labels" for m, a in names), names
    assert not any("arch_v2_regression" in (m or "") for m, _ in names), names


@pytest.mark.parametrize("fn", ["role_states", "o3_variant_states", "variant_pred", "states_for", "table_rows",
                                "memory_mentions", "sentence_role", "o1c", "llm_roles", "role_items"])
def test_state_functions_take_no_labelset_or_github(fn):
    import inspect
    params = set(inspect.signature(getattr(X, fn)).parameters)
    assert not params & {"labelset", "labels", "github", "gh", "pos", "ctl", "positives", "controls"}, params


# --- rework #372-1: 지출 한도(반복마다 다시 읽기·설정 전 예상), 역할 파일 sha 대조 -------------------------

def _gen_file(path, costs, finished=True):
    doc = {"k": len(costs), "generations": [{"calls": [{"cost_usd": c}]} for c in costs]}
    if finished:
        doc["finished_kst"] = "x"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def test_spend_guard_rereads_other_process_spend_before_next_call(tmp_path):
    """다른 프로세스가 같은 --spent-dir 에 지출 파일을 쓰면 다음 호출 전에 멈춘다."""
    spent = tmp_path / "work"
    out = spent / "gen"
    _gen_file(out / "base-2026-10-01-gen.json", [1.0])
    calls = []

    def caller(prompt):
        calls.append(prompt)
        if len(calls) == 1:              # 첫 호출 도중 다른 설정이 큰 지출을 기록
            _gen_file(out / "other-2026-10-01-gen.json", [9.0], finished=False)
        return {"result": "[]", "total_cost_usd": 0.5, "usage": {}}

    def gen(prompt, c):
        raw = c(prompt)
        return {"valid": True, "items": [], "calls": [{"cost_usd": raw["total_cost_usd"]}]}

    guard = X.SpendGuard(str(spent), 10.0, str(out))
    jobs = [{"t": "2026-10-01", "out": str(out / "mine-2026-10-01-gen.json"), "head": {"k": 3}, "prompt": "p"}]
    rc = X.run_setting(jobs, caller, guard, k=3, generate=gen)
    assert rc == 3 and len(calls) == 1                     # 1.0 + 9.0 + 0.5 >= 10 -> 두 번째 호출 전 멈춤
    assert guard.total() == pytest.approx(10.5)


def test_spend_guard_counts_own_spend_outside_spent_dir(tmp_path):
    guard = X.SpendGuard(str(tmp_path / "work"), 1.0, str(tmp_path / "elsewhere"))
    guard.add_own(0.6)
    assert not guard.over()
    guard.add_own(0.5)
    assert guard.over()


@pytest.mark.parametrize("existing, n_calls, max_usd, ok", [
    ([0.2, 0.2], 18, 10.0, True),        # 0.4 + 18 x 0.2 = 4.0
    ([0.2, 0.2], 18, 3.9, False),        # 0.4 + 3.6 > 3.9 -> 시작하지 않음
    ([], 18, 5.0, False),                # 측정 없음 -> 보수 기본값 0.30 x 18 = 5.4
    ([], 10, 5.0, True),                 # 3.0
])
def test_spend_guard_preflight_estimate_blocks_setting(tmp_path, existing, n_calls, max_usd, ok):
    spent = tmp_path / "work"
    if existing:
        _gen_file(spent / "gen" / "a-2026-10-01-gen.json", existing)
    guard = X.SpendGuard(str(spent), max_usd, str(spent / "gen"))
    est = guard.preflight(n_calls)
    assert est["ok"] is ok and est["calls"] == n_calls


def test_run_setting_does_not_start_when_preflight_fails(tmp_path):
    spent = tmp_path / "work"
    _gen_file(spent / "gen" / "a-2026-10-01-gen.json", [3.0, 3.0])     # 평균 3.0/호출
    calls = []
    guard = X.SpendGuard(str(spent), 10.0, str(spent / "gen"))
    jobs = [{"t": "2026-10-04", "out": str(spent / "gen" / "b-2026-10-04-gen.json"), "head": {"k": 3}, "prompt": "p"}]
    rc = X.run_setting(jobs, lambda p: calls.append(p), guard, k=3,
                       generate=lambda p, c: {"valid": True, "items": [], "calls": []})
    assert rc == 3 and calls == [] and not os.path.exists(jobs[0]["out"])


def test_states_for_rejects_llm_roles_from_other_mentions_file():
    parsed = parsed_of([("a", "app#1 후속 남음", ["project:app"])])
    metas = [meta("a", ["app#1"])]
    items = X.role_items(parsed)
    llm = {"n_items": len(items), "stopped": False, "roles": {"1": "open"}, "mentions_sha256": "aaa"}
    assert X.states_for("o3c_llm", T, metas, parsed, {}, llm, mentions_sha256="aaa")["app#1"]["state"] == X.S_OPEN
    with pytest.raises(X.Fail):
        X.states_for("o3c_llm", T, metas, parsed, {}, llm, mentions_sha256="bbb")
    with pytest.raises(X.Fail):
        X.states_for("o3c_llm", T, metas, parsed, {}, llm)            # sha 없이 부르면 거부


# --- R5-A4(issue #374): 표 형식 변형, 추가 토큰, 사유 파일 -> P_bank·R_bank2, R_bank 노이즈 바닥 ---------------

@pytest.mark.parametrize("form", X.FORMS)
def test_fmt_base_is_byte_identical_to_372_render(form):
    rows = X.table_rows(ST, ["app"], T, form)
    assert X.table_rows_fmt(ST, ["app"], T, form, "base") == rows
    assert X.render_table_fmt(rows, T, form, "base") == X.render_table(rows, T, form)
    assert X.render_table_fmt(rows, T, form) == X.render_table(rows, T, form)       # 기본값 = base


@pytest.mark.parametrize("fmt, keys, body, cols, recent", [
    ("base", ["app#1", "app#2"], ["app#1 | 열림 | 2026-09-01 | a", "app#2 | 끝남 | 2026-09-25 | b"],
     "키 | 상태 | 날짜 | 근거 id", True),
    ("a", ["app#1", "app#2"], ["app#1 | 열림", "app#2 | 끝남"], "키 | 상태", True),
    ("b", ["app#1", "app#2"], ["app#1 | 열림 | 2026-09-01", "app#2 | 끝남 | 2026-09-25"], "키 | 상태 | 날짜", True),
    ("c", ["app#1"], ["app#1 | 열림 | 2026-09-01 | a"], "키 | 상태 | 날짜 | 근거 id", False),
    ("a+c", ["app#1"], ["app#1 | 열림"], "키 | 상태", False),
    ("b+c", ["app#1"], ["app#1 | 열림 | 2026-09-01"], "키 | 상태 | 날짜", False),
])
def test_fmt_render_columns_head_and_recent_done(fmt, keys, body, cols, recent):
    rows = X.table_rows_fmt(ST, ["app"], T, "open", fmt)
    assert [r["key"] for r in rows] == keys
    lines = X.render_table_fmt(rows, T, "open", fmt).splitlines()
    assert lines[1:-1] == body and lines[-1] == "</item_status_table>"
    head = lines[0]
    assert head.endswith("줄: " + cols)
    assert ("최근 7일 안에 끝난 키" in head) is recent
    base_head = X.render_table([], T, "open").splitlines()[0]
    # 나머지 문구는 #372 열림만 머리말과 같다(열 이름, c 계열의 '와 최근 7일 안에 끝난 키' 구만 다름)
    want = base_head.replace("줄: 키 | 상태 | 날짜 | 근거 id", "줄: " + cols)
    if not recent:
        want = want.replace("와 최근 7일 안에 끝난 키", "")
    assert head == want


@pytest.mark.parametrize("fmt", [f for f in X.TABLE_FMTS if f != "base"])
def test_fmt_variants_refuse_full_table(fmt):
    with pytest.raises(X.Fail):
        X.table_rows_fmt(ST, ["app"], T, "full", fmt)
    with pytest.raises(X.Fail):
        X.render_table_fmt([], T, "full", fmt)


def test_forms_unchanged_and_fmt_list():
    assert X.FORMS == ("open", "full")
    assert X.TABLE_FMTS == ("base", "a", "b", "c", "a+c", "b+c")


@pytest.mark.parametrize("variant, form, fmt, on, want", [
    ("o3c_llm", "open", "base", False, "o3c_llm-open"),           # #372 와 같은 문자열
    ("o3c_llm", "open", "base", True, "o3c_llm-open+o1c"),
    ("o3c", "full", "base", False, "o3c-full"),
    ("o3c_llm", "open", "a+c", False, "o3c_llm-open[a+c]"),
    ("o3c_llm", "open", "b", True, "o3c_llm-open[b]+o1c"),
])
def test_oracle_name_base_matches_372_and_roundtrips(variant, form, fmt, on, want):
    name = X.oracle_name(variant, form, fmt, on)
    assert name == want
    if fmt == "base":
        assert name == f"{variant}-{form}" + ("+o1c" if on else "")
    assert X.parse_oracle(name) == {"variant": variant, "form": form, "fmt": fmt, "o1c": on}
    assert X.parse_oracle(None) is None


def test_dllm_table_fmt_default_is_base():
    seen = {}

    def fake(args):
        seen["fmt"] = args.table_fmt
        return 0
    orig = X.cmd_dllm
    try:
        X.cmd_dllm = fake
        argv = ["dllm", "--labelset", "l", "--aliases", "a", "--meta", "m", "--mentions", "n", "--plan", "p",
                "--families", "f", "--tag-aliases", "g", "--recalls", "r", "--t", T, "--out-dir", "o",
                "--variant", "o3c_llm", "--form", "open", "--label", "x"]
        # main 은 cmds 사전을 호출 시점에 만들므로 모듈 속성 교체가 반영된다
        assert X.main(argv) == 0 and seen["fmt"] == "base"
        assert X.main(argv + ["--table-fmt", "a+c"]) == 0 and seen["fmt"] == "a+c"
        with pytest.raises(SystemExit):
            X.main(argv + ["--table-fmt", "d"])
    finally:
        X.cmd_dllm = orig


@pytest.mark.parametrize("table, rows, rows_o1c, want", [
    (100, 1000, None, 0.10),           # o1c 없음 = 표 토큰 비율
    (100, 1000, 1000, 0.10),
    (100, 1000, 1250, 0.35),           # o1c 주석 +250 토큰
    (0, 1000, 1100, 0.10),
])
def test_extra_ratio_includes_o1c_added_input_tokens(table, rows, rows_o1c, want):
    assert X.extra_ratio(table, rows, rows_o1c) == pytest.approx(want)


def test_extra_ratio_refuses_zero_input():
    with pytest.raises(X.Fail):
        X.extra_ratio(1, 0, None)


def test_o1c_tokens_counts_annotation_growth():
    from bench import d_llm as D
    from bench import r5_metrics as R
    rows = [{"id": "r1", "text": "app#2 진행", "tags": ["project:app"], "mentioned_at": at("2026-09-20")},
            {"id": "r2", "text": "app#1 진행", "tags": ["project:app"], "mentioned_at": at("2026-09-20")}]
    counter = lambda s: len(s.split())                       # noqa: E731  (가짜 토큰 셈)
    base = counter("\n".join(D.render_row(r) for r in D.assemble({"runs": [{"q": 1, "max_tokens": 0,
                                                                            "results": rows}]})["rows"]))
    _asm, tok, st = X._o1c_tokens(D, R, rows, ST, ALIASES, 200000, counter)
    assert st["annotated_rows"] == 1                         # app#2(끝남)만
    assert tok - base == len(O.note("app#2", ST["app#2"]).split())


@pytest.mark.parametrize("side, has, reader, llm, want", [
    ("P", False, X.S_OPEN, X.S_OPEN, "ii"),       # 끝냄 언급 없음이 먼저
    ("P", False, X.S_DONE, X.S_OPEN, "ii"),       # (ii) 가 (i) 보다 먼저
    ("P", True, X.S_DONE, X.S_OPEN, "i"),         # 판독은 끝남인데 LLM 상태는 열림
    ("P", True, X.S_DONE, X.S_UNDET, "i"),
    ("P", True, X.S_OPEN, X.S_OPEN, "iii"),       # 뱅크 마지막 비참조 = 열림(라벨 끝남과 반대)
    ("P", True, X.S_OPEN, X.S_DONE, "i"),         # LLM 상태가 판독과 다르면 (iii) 보다 먼저 (i)
    ("P", True, X.S_OPEN, X.S_OPEN, "iii"),
    ("P", True, X.S_DONE, X.S_DONE, "iv"),        # 입력은 맞는데 소비자 오판
    ("R", False, X.S_DONE, X.S_DONE, "ii"),       # 열림 언급 없음
    ("R", False, X.S_UNDET, X.S_UNDET, "ii"),
    ("R", True, X.S_OPEN, X.S_DONE, "i"),
    ("R", True, X.S_OPEN, X.S_UNDET, "i"),
    ("R", True, X.S_DONE, X.S_DONE, "iii"),
    ("R", True, X.S_DONE, X.S_OPEN, "i"),
    ("R", True, X.S_DONE, X.S_UNDET, "i"),
    ("R", True, X.S_OPEN, X.S_OPEN, "iv"),
])
def test_classify_reason_order_ii_i_iii_iv(side, has, reader, llm, want):
    assert X.classify_reason(side, has, reader, llm) == want
    assert X.REASONS == ("ii", "i", "iii", "iv")


def _reason(setting, t, key, side, reason, has, reader, llm):
    return {"setting": setting, "t": t, "key_code": X.key_code(key), "side": side, "reason": reason,
            "evidence": {"has_side_mention": has, "reader_state": reader, "llm_state": llm}}


def test_key_code_is_stable_and_hides_key():
    c = X.key_code("app#1")
    assert c == X.key_code("app#1") and c != X.key_code("app#2") and "app" not in c and len(c) == 11


def test_load_reasons_rechecks_order_and_conflicts(tmp_path):
    p = tmp_path / "reasons.json"
    ok = [_reason("s", T, "app#1", "P", "iii", True, X.S_OPEN, X.S_OPEN)]
    p.write_text(json.dumps({"items": ok}))
    assert len(X.load_reasons(str(p))) == 1
    bad = [_reason("s", T, "app#1", "P", "iv", True, X.S_OPEN, X.S_OPEN)]       # 판정 순서상 (iii)
    p.write_text(json.dumps({"items": bad}))
    with pytest.raises(X.Fail, match="order"):
        X.load_reasons(str(p))
    p.write_text(json.dumps({"items": ok + [_reason("s2", T, "app#1", "P", "iv", True, X.S_DONE, X.S_DONE)]}))
    with pytest.raises(X.Fail, match="conflicting"):
        X.load_reasons(str(p))
    p.write_text(json.dumps({"items": [dict(ok[0], reason="v")]}))
    with pytest.raises(X.Fail):
        X.load_reasons(str(p))


@pytest.mark.parametrize("evidence", [
    None,                                                                   # 근거 없음
    {},                                                                     # 빈 근거
    {"reader_state": X.S_UNDET, "llm_state": X.S_UNDET},                    # has_side_mention 빠짐
    {"has_side_mention": 0, "reader_state": X.S_UNDET, "llm_state": None},  # bool 아님
    {"has_side_mention": False, "reader_state": X.S_UNDET},                 # llm_state 칸 빠짐
    {"has_side_mention": False, "reader_state": "x", "llm_state": None},    # 상태 어휘 밖
    {"has_side_mention": True, "reader_state": X.S_UNDET, "llm_state": X.S_OPEN},   # 언급 있음인데 판독 미정
])
def test_load_reasons_refuses_missing_or_inconsistent_evidence(tmp_path, evidence):
    p = tmp_path / "reasons.json"
    it = {"setting": "s", "t": T, "key_code": X.key_code("app#1"), "side": "P", "reason": "ii", "evidence": evidence}
    p.write_text(json.dumps({"items": [it]}))
    with pytest.raises(X.Fail, match="evidence"):
        X.load_reasons(str(p))


def _pairs():
    pos = {(f"app#{i}", T) for i in range(1, 6)}                # 양성 5
    ctl = {(f"app#{i}", T) for i in range(11, 21)}              # 대조 10
    present = {(f"app#{i}", T) for i in range(11, 19)}          # R 분모 8
    st = {p: "not_open" for p in pos | ctl}
    for i in (1, 2, 3):                                         # P 오답 3
        st[(f"app#{i}", T)] = "open"
    for i in (11, 12, 13, 14, 19):                              # 대조 open 5(19 는 R 분모 밖)
        st[(f"app#{i}", T)] = "open"
    return pos, ctl, present, st


def test_bank_adjusted_p_bank_and_r_bank2_denominators():
    pos, ctl, present, st = _pairs()
    items = [_reason("s", T, "app#1", "P", "ii", False, X.S_OPEN, X.S_OPEN),
             _reason("s", T, "app#2", "P", "iii", True, X.S_OPEN, X.S_OPEN),
             _reason("s", T, "app#3", "P", "iv", True, X.S_DONE, X.S_DONE)]
    miss = {15: ("ii", False, X.S_DONE, X.S_DONE), 16: ("i", True, X.S_OPEN, X.S_DONE),
            17: ("iii", True, X.S_DONE, X.S_DONE), 18: ("iv", True, X.S_OPEN, X.S_OPEN)}
    items += [_reason("s", T, f"app#{i}", "R", r, h, a, b) for i, (r, h, a, b) in miss.items()]
    out = X.bank_adjusted(st, pos, ctl, present, X.reason_index(items))
    assert (out["P_num"], out["P_den"]) == (5, 8)               # open 라벨 쌍 8 = 양성 3 + 대조 5
    assert out["P_bank_den"] == 8 - 2 and out["P_bank"] == pytest.approx(5 / 6)     # (ii)·(iii) 2 제외
    assert (out["R_num"], out["R_den"]) == (4, 8)
    assert out["R_bank2_den"] == 8 - 2 and out["R_bank2"] == pytest.approx(4 / 6)
    assert out["P_reasons"] == {"ii": 1, "i": 0, "iii": 1, "iv": 1}
    assert out["R_reasons"] == {"ii": 1, "i": 1, "iii": 1, "iv": 1}
    assert (out["P_errors"], out["R_misses"]) == (3, 4)


def test_bank_adjusted_refuses_unclassified_error_pair():
    pos, ctl, present, st = _pairs()
    with pytest.raises(X.Fail, match="no reason"):
        X.bank_adjusted(st, pos, ctl, present, {})


def test_error_pairs_r_side_only_within_present_denominator():
    pos, ctl, present, st = _pairs()
    e = X.error_pairs(st, pos, ctl, present)
    assert [k for k, _ in e["P"]] == ["app#1", "app#2", "app#3"]
    assert sorted(k for k, _ in e["R"]) == ["app#15", "app#16", "app#17", "app#18"]     # 19·20 은 분모 밖


def test_rbank_values_and_noise_floor_use_bank_open_denominator():
    ctl = {(f"app#{i}", T) for i in range(1, 7)}
    bank = {("app#1", T): X.S_OPEN, ("app#2", T): X.S_OPEN, ("app#3", T): X.S_OPEN, ("app#4", T): X.S_DONE,
            ("app#5", T): X.S_UNDET}
    a = {p: "open" for p in ctl}
    b = {p: "not_open" for p in ctl}
    assert X.rbank_values(a, bank, ctl) == {("app#1", T): 1, ("app#2", T): 1, ("app#3", T): 1}
    nf = X.rbank_noise_floor([a, b, a], bank, ctl)
    assert nf["rates"] == [1.0, 0.0, 1.0] and len(nf["pairs"]) == 3 and nf["seed"] == X.SEED
    assert nf["floor"] == pytest.approx(1.0)


@pytest.mark.parametrize("spec, want", [
    ("x=/a/b::lab", ("x", "/a/b", "lab")),
    ("x=/a::b::lab", ("x", "/a::b", "lab")),
])
def test_setting_specs(spec, want):
    assert X._setting_specs([spec]) == [want]


@pytest.mark.parametrize("spec", ["x=/a/b", "=/a::b", "x=/a::"])
def test_setting_specs_refuses_bad(spec):
    with pytest.raises(X.Fail):
        X._setting_specs([spec])


@pytest.mark.parametrize("fn", ["table_rows_fmt", "render_table_fmt", "extra_ratio", "classify_reason"])
def test_a4_state_functions_take_no_labelset_or_github(fn):
    import inspect
    params = set(inspect.signature(getattr(X, fn)).parameters)
    assert not params & {"labelset", "labels", "github", "gh", "positives", "controls"}, params


def test_a4_judge_refuses_single_base_noise_floor(capsys):
    argv = ["a4-judge", "--labelset", "l", "--aliases", "a", "--meta", "m", "--mentions", "n", "--llm-roles", "r",
            "--recalls", "c", "--out", "o", "--t", T, "--reasons", "x", "--plan", "p", "--families", "f",
            "--tag-aliases", "g", "--base-gen-dir", "b", "--base", "base1"]
    assert X.main(argv) == 1                   # 입력 파일을 읽기 전에 거부
    assert "two distinct --base" in capsys.readouterr().err
    assert X.main(argv + ["--base", "base1"]) == 1     # 같은 라벨 두 번 = 서로 다른 짝 없음
    assert "two distinct --base" in capsys.readouterr().err


@pytest.mark.parametrize("prev_oracle, fmt, want_rc", [
    ("o3c_llm-open[a+c]", "base", 1),          # 같은 라벨의 다른 형식 출력 = 거부
    ("o3c_llm-open", "a+c", 1),
    ("o3c_llm-open+o1c", "base", 1),            # o1c 유무도 설정
    ("o3c_llm-open[a+c]", "a+c", 0),            # 같은 설정 = 건너뜀
])
def test_dllm_resume_refuses_existing_gen_of_another_setting(tmp_path, monkeypatch, capsys, prev_oracle, fmt, want_rc):
    from bench import d_llm as D
    from bench import r5_metrics as R
    monkeypatch.setattr(X, "_table_ctx", lambda a: ([], [], {}, None, {}, {}))
    monkeypatch.setattr(X, "_aliases", lambda a: {})
    monkeypatch.setattr(D, "tiktoken_counter", lambda: None)
    monkeypatch.setattr(D, "load_prompt", lambda p: "")
    monkeypatch.setattr(R, "load_recalls", lambda d, ts: {})
    (tmp_path / "m.json").write_text("{}")
    out = tmp_path / "gen"
    out.mkdir()
    (out / f"lab-{T}-gen.json").write_text(json.dumps({"oracle": prev_oracle, "k": 3, "finished_kst": "x",
                                                         "generations": [{}, {}, {}]}))
    argv = ["dllm", "--labelset", "l", "--aliases", "a", "--meta", "m", "--mentions", str(tmp_path / "m.json"),
            "--plan", "p", "--families", "f", "--tag-aliases", "g", "--recalls", "r", "--t", T, "--out-dir", str(out),
            "--variant", "o3c_llm", "--form", "open", "--label", "lab", "--table-fmt", fmt]
    assert X.main(argv) == want_rc
    err = capsys.readouterr().err
    assert ("refuse" in err) is (want_rc == 1)
