"""bench/r6_endtoend.py - 런 6 종단 재확인(issue #388, 상위 #386).

가짜 입력만 쓴다(네트워크 0, LLM 0, 가짜 토큰 셈). 키·문장·기억은 전부 합성이다.
덮는 것: 새 payload 키 목록 읽기(`keys`, 런 5 `rows` 호환), 감사 줄 판정(attached = 상태 어휘 + 결과 키,
attached + not_attached 사유별 = recall 수), 짝 집계(켬 - 끔 군집 재표집, 표 안·밖 R 누락, C1 대비 빠진 키),
추가 토큰 두 가지, C7 자격, 손잡이 파일 정규 sha256.
"""
from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from bench import d_llm as D
from bench import r5_metrics as R
from bench import r5_roles as X
from bench import r6_endtoend as E6
from gateway import item_open_table as OT

T = "2026-10-01"
TS = R.query_timestamp(R._day(T))


def row(rid: str, text: str = "x", day: str = T) -> dict:
    return {"id": rid, "text": text, "type": "world", "mentioned_at": f"{day}T03:00:00+00:00", "tags": []}


def new_tab(keys, text="<item_open_table>\nT\n</item_open_table>") -> dict:
    return {"t": T, "fmt": "a", "keys": list(keys), "truncated": 0, "text": text}


# --- payload 키 읽기 -----------------------------------------------------------------------------------

@pytest.mark.parametrize("tab,want", [
    (new_tab(["a#1", "b#2"]), ["a#1", "b#2"]),                                              # R6-1 모양
    ({"t": T, "fmt": "a", "rows": [["a#1", "열림", T], ["a#2", "끝남", T]], "truncated": 0, "text": "x"}, ["a#1"]),  # 런 5
    (new_tab([]), []),
])
def test_table_keys_reads_new_and_old_shape(tab, want):
    assert E6.table_keys(tab) == want


def test_table_keys_rejects_unknown_shape():
    with pytest.raises(E6.Fail):
        E6.table_keys({"t": T, "text": "x"})


def test_doc_keys_union_and_per_query():
    doc = {"runs": [{"q": 2, "open_table": new_tab(["b#1", "c#1"], "B")}, {"q": 1, "open_table": new_tab(["a#1", "b#1"], "A")},
                    {"q": 3, "open_table": None}]}
    k = E6.doc_keys(doc)
    assert k["union"] == {"a#1", "b#1", "c#1"} and k["per_query"] == [2, 2]


# --- 감사 줄 ------------------------------------------------------------------------------------------

def line(reason, ts="2026-11-02T06:00:00Z", na=None, sha=E6.DEFAULT_KNOBS_SHA256, src="file", **extra):
    info = {"knobs_source": src, "knobs_sha256": sha, **extra}
    if na is not None:
        info["not_attached"] = na
    return json.dumps({"decision": OT.DECISION, "ts": ts, "reason": reason, "item_open_table": info})


W0, W1 = "2026-11-02T05:59:00Z", "2026-11-02T06:01:00Z"


def test_audit_counts_not_attached_string_dict_and_none():
    lines = [line("attached"), line("not-attached", na="no-status-term"),
             line("not-attached", na={"no-result-keys": 2, "no-status-term": 1}), line("not-attached"),
             line("attached", ts="2026-11-02T07:00:00Z"),                                 # 구간 밖
             json.dumps({"decision": "other", "ts": "2026-11-02T06:00:00Z", "reason": "attached"}), "not json", ""]
    c = E6.audit_counts(lines, W0, W1)
    assert c["lines"] == 4 and c["reason"] == {"attached": 1, "not-attached": 3}
    assert c["not_attached"] == {"no-status-term": 2, "no-result-keys": 2, "none": 1}


def counts(att=3, nst=1, nrk=1, **over):
    c = {"lines": att + nst + nrk, "reason": {"attached": att, "not-attached": nst + nrk},
         "not_attached": {k: v for k, v in (("no-status-term", nst), ("no-result-keys", nrk)) if v},
         "knobs_source": {"file": att + nst + nrk}, "knobs_sha256": {E6.DEFAULT_KNOBS_SHA256: att + nst + nrk},
         "knob_warnings": 0, "aliases_mismatch": 0}
    c.update(over)
    return c


EXP = {"attached": 3, "no-status-term": 1, "no-result-keys": 1}


@pytest.mark.parametrize("c,recalls,saved,expected,ok,frag", [
    (counts(), 5, 3, EXP, True, None),
    (counts(att=5, nst=0, nrk=0), 5, 5, {"attached": 5}, True, None),                     # dev 상태 질의 전부 붙음
    (counts(att=0, nst=4, nrk=1, knobs_sha256={}), 5, 0, {"no-status-term": 4, "no-result-keys": 1}, True, None),
    (counts(), 6, 3, EXP, False, "lines"),                                                 # 감사 줄 수 != recall 수
    (counts(reason={"attached": 3, "not-attached": 1, "bank-unset": 1}), 5, None, None, False, "other reasons"),
    (counts(not_attached={"no-status-term": 1, "none": 1}), 5, None, None, False, "unknown not_attached"),
    (counts(not_attached={"no-status-term": 1}), 5, None, None, False, "attached + not_attached"),
    (counts(), 5, 4, None, False, "saved tables"),                                         # 저장 표 수와 다름
    (counts(), 5, 3, {"attached": 4, "no-status-term": 1}, False, "expected"),            # 다시 한 판정과 다름
    (counts(knobs_sha256={"f7fcf626": 5}), 5, None, None, False, "knobs_sha256"),
    (counts(knobs_source={"default": 5}), 5, None, None, False, "knobs_source"),
    (counts(knob_warnings=1), 5, None, None, False, "warnings"),
    (counts(aliases_mismatch=2), 5, None, None, False, "aliases"),
])
def test_audit_check_matrix(c, recalls, saved, expected, ok, frag):
    r = E6.audit_check(c, recalls, E6.DEFAULT_KNOBS_SHA256, saved_tables=saved, expected=expected)
    assert r["ok"] is ok
    if frag:
        assert any(frag in f for f in r["fail"])


ALIASES = {"alpha": "alpha"}


@pytest.mark.parametrize("text,rows,want", [
    ("남은 일", [{"id": "1", "text": "alpha#3 진행", "tags": []}], "attached"),
    ("어제 요약", [{"id": "1", "text": "alpha#3 진행", "tags": []}], "no-status-term"),
    ("남은 일", [{"id": "1", "text": "키 없는 기억", "tags": []}], "no-result-keys"),
])
def test_expected_attached_redoes_gateway_decision(text, rows, want):
    doc = {"query_timestamp": TS, "runs": [{"q": 1, "results": rows}], "_queries": [{"q": 1, "text": text}]}
    assert E6.expected_attached(doc, None, OT._defaults(), ALIASES) == {want: 1}


def test_expected_attached_uses_instruction_terms():
    """지시문에 상태 어휘가 있으면 문안에 없어도 붙는다(질의 = 지시문 + 줄바꿈 + 문안)."""
    doc = {"query_timestamp": TS, "runs": [{"q": 1, "results": [{"id": "1", "text": "alpha#3", "tags": []}]}],
           "_queries": [{"q": 1, "text": "요약"}]}
    assert E6.expected_attached(doc, "할 일 기준", OT._defaults(), ALIASES) == {"attached": 1}


def test_cmd_audit_end_to_end(tmp_path):
    log = tmp_path / "gateway.log"
    log.write_text("\n".join([line("attached"), line("not-attached", na="no-status-term")]) + "\n")
    rec = tmp_path / "rec"
    rec.mkdir()
    doc = {"t": T, "query_timestamp": TS,
           "runs": [{"q": 1, "results": [{"id": "1", "text": "alpha#3", "tags": []}], "open_table": new_tab(["alpha#3"])},
                    {"q": 2, "results": [{"id": "2", "text": "alpha#4", "tags": []}], "open_table": None}]}
    (rec / f"t-{T}.json").write_text(json.dumps(doc))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"per_t": {T: {"instruction": "지시", "query_timestamp": TS,
                                              "queries": [{"text": "남은 일"}, {"text": "어제 요약"}]}}}))
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps(ALIASES))
    win = tmp_path / "win.json"
    win.write_text(json.dumps({"dev-on": {"start_utc": W0, "end_utc": W1, "recalls": 2, "arm": "on",
                                          "recalls_dir": str(rec), "plan": str(plan)},
                               "dev-off": {"start_utc": "2026-11-02T07:00:00Z", "end_utc": "2026-11-02T07:01:00Z",
                                           "recalls": 2, "arm": "off"}}))
    out = tmp_path / "audit.json"
    assert E6.main(["audit", "--log", str(log), "--windows", str(win), "--aliases", str(aliases), "--out", str(out)]) == 0
    res = json.loads(out.read_text())
    assert res["windows"]["dev-on"]["check"] == {"ok": True, "fail": []}
    assert res["windows"]["dev-on"]["expected"] == {"attached": 1, "no-status-term": 1}
    assert res["windows"]["dev-off"]["lines"] == 0 and "check" not in res["windows"]["dev-off"]
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


# --- 짝 집계 ------------------------------------------------------------------------------------------

def jc_fake():
    """대조 4쌍(k1~k4, S4 열림 3), 양성 2쌍(p1, p2). 기준선 결과에 나온 쌍 = 전부."""
    ctl = {(f"a#{i}", T) for i in range(1, 5)}
    pos = {("a#11", T), ("a#12", T)}
    s4 = {("a#1", T): X.S_OPEN, ("a#2", T): X.S_OPEN, ("a#3", T): X.S_OPEN, ("a#4", T): X.S_DONE}
    return SimpleNamespace(spos=pos, sctl=ctl, present=ctl | pos, s4=s4, b374=dict(s4))


def states(open_keys):
    jc = jc_fake()
    return {p: ("open" if p[0] in open_keys else "not_open") for p in jc.spos | jc.sctl}


def test_paired_on_minus_off_direction_and_seed():
    jc = jc_fake()
    on, off = states({"a#1", "a#2", "a#3"}), states({"a#1"})
    p = E6.paired(on, off, jc)
    assert p["R_bank_s4"]["diff"] == pytest.approx(2 / 3) and p["R"]["diff"] == pytest.approx(0.5)
    assert p["R_bank_s4"]["seed"] == 20261101 and p["R_bank_s4"]["n"] <= 2000
    assert E6.paired(off, on, jc)["R_bank_s4"]["diff"] == pytest.approx(-2 / 3)


def test_arm_metrics_needs_reasons_for_p_bank():
    jc = jc_fake()
    st = states({"a#1", "a#11"})                                  # P 오답 1(a#11), R 누락 3
    m = E6.arm_metrics(st, jc, {})
    assert m["P_bank"] is None and m["unreasoned"] == {"P": 1, "R": 3}
    reasons = {(T, X.key_code("a#11"), "P"): "iv", **{(T, X.key_code(f"a#{i}"), "R"): "iv" for i in (2, 3, 4)}}
    m = E6.arm_metrics(st, jc, reasons)
    assert m["P_bank"] == pytest.approx(0.5) and m["R_bank_s4"]["R_bank"] == pytest.approx(1 / 3)


def test_miss_split_in_table_dropped_and_outside():
    misses = [("a#1", T), ("a#2", T), ("a#3", T)]
    union = {T: {"a#1"}}
    c1 = {T: {"a#1", "a#2"}}
    reasons = {(T, X.key_code("a#1"), "R"): "iv"}
    s = E6.miss_split(misses, union, c1, reasons)
    assert s == {"in_table": 1, "out_of_scope_dropped": 1, "out_of_table": 1, "in_table_by_reason": {"iv": 1}}
    assert E6.miss_split(misses, union, None, {})["out_of_table"] == 2


def test_dropped_controls_counts_denominator_pairs():
    jc = jc_fake()
    d = E6.dropped_controls(jc, {T: {"a#1", "z#9"}}, {T: {"a#1", "a#2", "a#4"}})
    assert d["dropped_keys"] == {T: 2} and d["extra_keys"] == {T: 1}
    assert d["dropped_R_den_pairs"] == 2 and d["dropped_R_bank_s4_den_pairs"] == 1     # a#4 는 S4 끝남


# --- 토큰 ---------------------------------------------------------------------------------------------

def test_tokens_joined_text_and_payload_max():
    t1, t2 = new_tab(["a#1"], "TEXT-ONE"), new_tab(["a#1", "a#2"], "TEXT-TWO-LONGER")
    doc = {"runs": [{"q": 1, "results": [row("m1", "가나다")], "open_table": t1},
                    {"q": 2, "results": [row("m2", "라마바")], "open_table": t2},
                    {"q": 3, "results": [row("m3")], "open_table": t1}]}
    out = E6.tokens(D, {T: doc}, [T], 200000, counter=len)
    v = out["per_t"][T]
    assert v["texts"] == 2 and v["attached"] == 3 and v["union_keys"] == 2 and v["keys_per_query"] == [1, 2]
    assert v["table_tokens"] == len("TEXT-ONE\nTEXT-TWO-LONGER")
    from bench import r5_tune as T5
    assert v["payload_tokens_max"] == len(T5.payload_json(t2))
    assert out["ratio_max"] == pytest.approx(v["table_tokens"] / v["input_tokens"])


# --- C7·손잡이 ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("pb,rb,ref,want", [
    ([0.96, 0.95], [0.70, 0.62], 0.65, "replace-recommended"),
    ([0.96, 0.94], [0.70, 0.70], 0.65, "keep-default"),          # 한 회 P_bank 미달
    ([0.97, 0.97], [0.60, 0.62], 0.65, "keep-default"),          # R_bank 평균 미달
    ([None, 0.97], [0.70, 0.70], 0.65, "undecidable"),           # 사유 없는 오답
])
def test_c7_verdict(pb, rb, ref, want):
    reps = [{"P_bank": p, "R_bank_s4": r} for p, r in zip(pb, rb)]
    assert E6.c7_verdict(reps, ref)["verdict"] == want


def test_c7_verdict_needs_two_reps():
    with pytest.raises(E6.Fail):
        E6.c7_verdict([{"P_bank": 1.0, "R_bank_s4": 1.0}], 0.5)


def test_knobs_default_file_matches_r6_1_canonical_sha(tmp_path, capsys):
    out = tmp_path / "k.json"
    assert E6.main(["knobs", "--out", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["canonical_sha256"] == E6.DEFAULT_KNOBS_SHA256
    values, warnings = OT.parse_knobs(out.read_text())
    assert warnings == [] and OT._canon_sha256(values) == E6.DEFAULT_KNOBS_SHA256
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_knobs_default_uses_r6_1_header_not_gateway_default():
    # R6-3(#391) 뒤 게이트웨이 기본 머리는 C7 이다. knobs 기본 경로는 R6-2 기록값을 재현하려고 HEADER_R6_1 을 명시한다.
    values = E6.knobs_values()
    assert values["header"] == OT.HEADER_R6_1 != OT.HEADER_DEFAULT
    assert OT._canon_sha256(values) == E6.DEFAULT_KNOBS_SHA256


def test_knobs_c7_path_equals_new_gateway_default(tmp_path, capsys):
    h = tmp_path / "h.txt"
    h.write_text(OT.HEADER_C7 + "\n", encoding="utf-8")            # R7-2(#396): C7 은 기본값이 아니라 상수
    out = tmp_path / "k.json"
    assert E6.main(["knobs", "--out", str(out), "--c7-header-file", str(h)]) == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["c7"] is True
    assert printed["canonical_sha256"] == OT.Knobs({**OT._defaults(), "header": OT.HEADER_C7}, "default").sha256 == \
        "24044afd737184e2303e35a75e94d4aee7a2702eb32999c1c79d855c0627dfb8"


def test_knobs_c7_rejects_header_with_other_sha(tmp_path):
    h = tmp_path / "h.txt"
    h.write_text(OT.HEADER_R6_1 + "\n", encoding="utf-8")
    assert E6.main(["knobs", "--out", str(tmp_path / "k.json"), "--c7-header-file", str(h)]) == 1
    assert not (tmp_path / "k.json").exists()
