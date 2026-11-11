"""R6-1 오프라인 payload 판정 하네스(issue #387, `bench/r6_payload.py`). 네트워크 0, LLM 0, tiktoken 0(가짜 셈).

고정하는 것:
  * 질의 판단이 게이트웨이 요소 단계와 같다: 상태 어휘 없음 -> no-status-term, 결과 키 없음 -> no-result-keys,
    요청 project 태그 -> 그 repo, 아니면 결과 키 repo. 붙은 표는 payload 모양(`keys`, `rows` 없음).
  * 벤치: 부착률·사유·어휘 적중, G3 추정 = 추가 토큰 합 / base 합(질의 합, 라운드 곱 없음), 보정값.
  * dev: 질의 인자 재구성(지시문 + 줄바꿈 + 문안, 태그 없음), 키 합집합 vs C1 저장 표(자카드·빠진 키), 계획 불일치 거부.
  * 원래 증상: 문안 sha 가 저장 recall 과 다르면 거부.
  * 봉인 경로 거부, 출력 0600, 표준 출력에 원문 없음.
"""
import json
import os
import stat
from datetime import datetime, timezone

import pytest

from bench import r6_payload as P
from gateway import item_open_table as OT
from gateway import item_status

TS = "2026-10-20T14:59:59Z"
CUT = item_status._instant(TS)
COUNT = len                                            # 가짜 토큰 셈(글자 수)


def mention(key, role, day, mid=None):
    return OT.Mention(key, role, datetime(2026, 10, day, 1, tzinfo=timezone.utc), mid or f"m-{key}-{day}")


MENTIONS = [mention("alpha#1", "open", 1), mention("alpha#2", "open", 2), mention("beta#7", "open", 3),
            mention("beta#8", "done", 4), mention("gamma#9", "open", 5)]
KNOBS = OT._defaults()


def row(text, tags=()):
    return {"id": "x", "text": text, "tags": list(tags)}


ALPHA_ROW = row("인계 #5 남은 일", ("project:alpha",))
KEYLESS = row("키 없는 기억", ("kind:gotcha",))


@pytest.mark.parametrize("query,tags,rows,attached,reason,keys", [
    ("어제 회의 요약", ["project:beta"], [ALPHA_ROW], False, "no-status-term", None),
    ("남은 일 알려줘", ["project:beta"], [KEYLESS], True, "request", ["beta#7"]),
    ("남은 일 알려줘", None, [ALPHA_ROW], True, "result", ["alpha#1", "alpha#2"]),
    ("남은 일 알려줘", None, [KEYLESS], False, "no-result-keys", None),
    ("openapi 스키마", None, [ALPHA_ROW], False, "no-status-term", None),
])
def test_decide_matches_gateway_element_step(query, tags, rows, attached, reason, keys):
    d = P.decide({"query": query, "tags": tags, "query_timestamp": TS}, rows, MENTIONS, KNOBS, {})
    assert (d["attached"], d["reason"]) == (attached, reason)
    if keys is None:
        assert d["table"] is None
    else:
        assert d["table"]["keys"] == keys and "rows" not in d["table"]


def test_payload_tokens_counts_field_prefix_and_compact_json():
    obj = {"t": "2026-10-20", "fmt": "a", "keys": ["a#1"], "truncated": 0, "text": "줄"}
    expected = '"gateway_item_open_table":{"t":"2026-10-20","fmt":"a","keys":["a#1"],"truncated":0,"text":"줄"}'
    assert P.payload_tokens(obj, lambda s: s) == expected        # 셈 함수가 받는 문자열 그대로


def test_jaccard():
    assert P.jaccard(set(), set()) == 1.0
    assert P.jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)


# --- 벤치 -------------------------------------------------------------------------------------------

def bench_item(key, query, rows, tags=None, old_rows=0):
    old = {"t": "2026-10-20", "fmt": "a", "rows": [[f"z#{i}", "열림", "2026-10-01"] for i in range(old_rows)],
           "truncated": 0, "text": "x" * (10 * old_rows)} if old_rows else None
    return {"key": key, "args": {"query": query, "tags": tags, "query_timestamp": TS}, "rows": rows, "old_table": old}


def test_bench_report_rate_reasons_and_g3_estimate():
    items = [bench_item("k1", "어제 회의 요약", [ALPHA_ROW], old_rows=3),
             bench_item("k2", "미완료 작업", [ALPHA_ROW], old_rows=3),
             bench_item("k3", "진행 상황 정리", [KEYLESS], old_rows=3),
             bench_item("k4", "일반 질의", [KEYLESS], tags=["project:beta"], old_rows=3)]
    compare = {"tokens": {"base_total": 10_000, "rel_change": 0.4, "per_query_delta": {"p50": 1}}}
    out = P.bench_report(items, MENTIONS, KNOBS, {}, COUNT, COUNT, compare)
    assert out["queries"] == 4 and out["attached"] == 1 and out["attach_rate"] == 0.25
    assert out["reasons"] == {"no-result-keys": 1, "no-status-term": 2, "result": 1}
    assert out["term_hits"] == {"미완": 1, "진행 상황": 1}
    assert out["tagged_queries"] == 1
    added = out["added_o200k_per_attached"][0]
    assert out["added_o200k_sum"] == added and out["g3_estimate"] == added / 10_000      # 라운드 곱 없음
    assert out["calibration"]["old_estimate"] == sum(
        P.payload_tokens(it["old_table"], COUNT) for it in items) / 10_000
    assert out["calibration"]["old_table_rows"] == [3]


def test_bench_inputs_reads_cand_results_and_fills_bench_clock(tmp_path):
    run = tmp_path / "run"
    (run / "results").mkdir(parents=True)
    (run / "meta.json").write_text(json.dumps({"run_id": "r", "n_queries": 2, "bench_clock": TS}))
    corpus = tmp_path / "dev.jsonl"
    corpus.write_text("\n".join(json.dumps({"key": k, "args": {"query": "q"}}) for k in ("a1", "b2")) + "\n")
    for arm, key in (("cand", "a1"), ("cand", "b2"), ("base", "a1")):
        (run / "results" / f"{arm}.{key}.d.json").write_text(json.dumps({"results": [ALPHA_ROW], "payload_rest": {}}))
    items, meta = P.bench_inputs(str(run), str(corpus))
    assert [i["key"] for i in items] == ["a1", "b2"]
    assert all(i["args"]["query_timestamp"] == TS and i["old_table"] is None for i in items)
    (run / "results" / "cand.a1.e.json").write_text(json.dumps({"results": []}))
    with pytest.raises(P.Fail, match="more than one"):
        P.bench_inputs(str(run), str(corpus))


# --- dev ------------------------------------------------------------------------------------------

def dev_fixture(c1_keys=("alpha#1", "alpha#2", "beta#7", "gamma#9")):
    spec = {"instruction": " 지시문 상태 ", "query_timestamp": TS,
            "queries": [{"qid": "F1", "text": "남은 일 목록"}, {"qid": "F2", "text": "보류 중인 일"}]}
    old = {"t": "2026-10-20", "fmt": "a", "rows": [[k, "열림", "2026-10-01"] for k in c1_keys], "truncated": 0,
           "text": "C1"}
    doc = {"query_timestamp": TS, "runs": [
        {"q": 1, "qid": "F1", "results": [ALPHA_ROW], "open_table": old},
        {"q": 2, "qid": "F2", "results": [row("x #7", ("project:beta",))], "open_table": old}]}
    return spec, doc


def test_dev_t_union_vs_c1():
    spec, doc = dev_fixture()
    out = P.dev_t(spec, doc, MENTIONS, KNOBS, {}, COUNT, 1000)
    assert out["queries"] == 2 and out["attached"] == 2 and out["text_only_attached"] == 2
    assert out["reasons"] == {"result": 2}
    assert (out["c1_keys"], out["new_keys"], out["missing"], out["extra"]) == (4, 3, 1, 0)     # gamma 빠짐
    assert out["jaccard"] == pytest.approx(3 / 4)
    assert out["c1_reproduced"] is True and out["c1_tables_consistent"] is True
    assert out["distinct_texts"] == 2 and out["c1_distinct_texts"] == 1
    assert out["text_rate"] == out["text_tokens"] / 1000


def test_dev_t_query_args_are_instruction_newline_text_without_tags(monkeypatch):
    seen = []
    real = P.decide

    def spy(args, rows, mentions, knobs, aliases):
        seen.append(dict(args))
        return real(args, rows, mentions, knobs, aliases)
    monkeypatch.setattr(P, "decide", spy)
    spec, doc = dev_fixture()
    P.dev_t(spec, doc, MENTIONS, KNOBS, {}, COUNT, None)
    assert seen[0] == {"query": "지시문 상태\n남은 일 목록", "query_timestamp": TS}


def test_dev_t_text_only_rate_ignores_instruction():
    spec, doc = dev_fixture()
    spec = {**spec, "queries": [{"qid": "F1", "text": "회의 요약"}, {"qid": "F2", "text": "보류 중인 일"}]}
    out = P.dev_t(spec, doc, MENTIONS, KNOBS, {}, COUNT, None)
    assert out["attached"] == 2 and out["text_only_attached"] == 1        # 지시문의 `상태` 가 첫 질의를 붙인다


@pytest.mark.parametrize("change,match", [
    (lambda s, d: s["queries"].reverse(), "order"),
    (lambda s, d: d.update(query_timestamp="2026-10-21T00:00:00Z"), "query_timestamp"),
])
def test_dev_t_rejects_mismatched_recall(change, match):
    spec, doc = dev_fixture()
    change(spec, doc)
    with pytest.raises(P.Fail, match=match):
        P.dev_t(spec, doc, MENTIONS, KNOBS, {}, COUNT, None)


def test_dev_t_detects_c1_not_reproduced():
    spec, doc = dev_fixture(c1_keys=("alpha#1",))
    assert P.dev_t(spec, doc, MENTIONS, KNOBS, {}, COUNT, None)["c1_reproduced"] is False


# --- 원래 증상 ------------------------------------------------------------------------------------

def test_symptom_report_and_sha_guard():
    text = "미뤄 둔 일 목록"
    old = {"t": "2026-10-20", "fmt": "a", "rows": [[k, "열림", "d"] for k in ("alpha#1", "alpha#2", "beta#7", "gamma#9")],
           "truncated": 0, "text": "C1"}
    doc = {"symptom_sha256": P.sha256_text(text), "query_timestamp": TS,
           "runs": [{"results": [ALPHA_ROW], "open_table": old}]}
    out = P.symptom_report(doc, text, MENTIONS, KNOBS, {}, COUNT)
    assert out["attached"] is True and out["reason"] == "result"
    assert (out["keys"], out["c1_keys"], out["missing"]) == (2, 4, 2) and out["c1_reproduced"] is True
    with pytest.raises(P.Fail, match="symptom text"):
        P.symptom_report(doc, text + "!", MENTIONS, KNOBS, {}, COUNT)


# --- CLI 가드 ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/x/rec-sealed-on", "/x/sealed-plan.json", "/x/SEALED-bodies.jsonl", "/x/rec-sealed-on/"])
def test_refuse_sealed(path):
    with pytest.raises(P.Fail, match="sealed"):
        P.refuse_sealed("/x/ok", path)


def test_cli_refuses_sealed_input_with_exit_2(tmp_path, capsys):
    args = []
    for name in ("store", "aliases", "bench-run", "bench-corpus", "bench-compare", "plan", "dev-recalls",
                 "symptom-file", "out"):
        args += [f"--{name}", str(tmp_path / name)]
    args += ["--symptom-recall", str(tmp_path / "rec-sealed-on")]
    assert P.main(args) == 2
    assert "sealed" in capsys.readouterr().err


def test_write_private_is_0600(tmp_path):
    out = tmp_path / "o.json"
    P._write_private(str(out), {"a": 1})
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600 and json.loads(out.read_text()) == {"a": 1}


def test_dev_denominators_cover_six_dev_t():
    assert sorted(P.DEV_DENOMINATORS) == ["2026-09-28", "2026-10-01", "2026-10-04", "2026-10-07", "2026-10-10",
                                          "2026-10-13"]


# --- rework #387-1: 봉인 경로 성분·dev t 전부·입력 오류 exit 2 ------------------------------------------

@pytest.mark.parametrize("path", ["/x/rec-sealed-on/t-2026-10-22.json", "/x/sealed-work/rec-dev-on",
                                  "/x/rec-sealed-on/sub/"])
def test_refuse_sealed_checks_every_path_component(path):
    with pytest.raises(P.Fail, match="sealed"):
        P.refuse_sealed("/x/ok", path)


def _cli_args(tmp_path, **override):
    names = ("store", "aliases", "bench-run", "bench-corpus", "bench-compare", "plan", "dev-recalls",
             "symptom-recall", "symptom-file", "out")
    vals = {n: str(tmp_path / n) for n in names}
    vals.update({k.replace("_", "-"): v for k, v in override.items()})
    args = []
    for n in names:
        args += [f"--{n}", vals[n]]
    return args


def test_cli_refuses_sealed_bench_corpus(tmp_path, capsys):
    assert P.main(_cli_args(tmp_path, bench_corpus=str(tmp_path / "sealed-split" / "dev.jsonl"))) == 2
    assert "sealed" in capsys.readouterr().err


def test_cli_bad_store_exits_2_without_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(P, "_encoders", lambda: (COUNT, COUNT))
    (tmp_path / "aliases").write_text("{}")
    (tmp_path / "store").write_bytes(b"not a sqlite database" * 10)
    assert P.main(_cli_args(tmp_path)) == 2
    assert "StoreError" in capsys.readouterr().err


def dev_dir(tmp_path, ts, plan_sha="p"):
    d = tmp_path / "rec-dev-on"
    d.mkdir()
    for t in ts:
        (d / f"t-{t}.json").write_text(json.dumps({"t": t, "plan_sha256": plan_sha, "query_timestamp": TS,
                                                   "runs": []}))
    return d


def test_dev_all_requires_every_dev_t(tmp_path):
    ts = sorted(P.DEV_DENOMINATORS)
    d = dev_dir(tmp_path, ts[:-1])
    with pytest.raises(P.Fail, match="cover all dev t"):
        P.dev_all(str(d), "p", {"per_t": {t: {"instruction": "i", "queries": [], "query_timestamp": TS} for t in ts}},
                  MENTIONS, KNOBS, {}, COUNT)


@pytest.mark.parametrize("t,name", [("2026-10-22", "t-2026-10-22.json"),        # dev 밖(봉인·간격) t
                                    ("2026-09-28", "t-2026-10-01.json"),        # 파일명과 t 불일치
                                    (["2026-09-28"], "t-2026-09-28.json")])     # 문자열 아님
def test_dev_all_rejects_non_dev_or_mismatched_t(tmp_path, t, name):
    d = tmp_path / "rec-dev-on"
    d.mkdir()
    (d / name).write_text(json.dumps({"t": t, "plan_sha256": "p", "runs": []}))
    with pytest.raises(P.Fail, match="not one of the dev t"):
        P.dev_all(str(d), "p", {"per_t": {}}, MENTIONS, KNOBS, {}, COUNT)


def test_dev_all_rejects_other_plan(tmp_path):
    d = dev_dir(tmp_path, sorted(P.DEV_DENOMINATORS), plan_sha="other")
    with pytest.raises(P.Fail, match="another plan"):
        P.dev_all(str(d), "p", {"per_t": {}}, MENTIONS, KNOBS, {}, COUNT)


def test_r6_1_knobs_reproduce_r6_1_canonical_sha():
    # #391-1: 기본 머리가 C7 로 바뀐 뒤에도 R6-1 하네스는 R6-1 기본 손잡이(머리 HEADER_R6_1)로 잰다(r6-1-payload.md 재현).
    knobs = P.r6_1_knobs()
    assert knobs["header"] == OT.HEADER_R6_1 != OT.HEADER_DEFAULT
    assert OT.Knobs(knobs, "default").sha256 == "0ca334da8e68e695b1248da30df7616e7818c97ead3041e4d8cb9026c01883b7"
