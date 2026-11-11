"""issue #13 재생 하네스 판정기 - 판정 규칙을 합성 데이터로 고정한다.

하네스의 약속은 "무변경은 반드시 NO-CHANGE, 알려진 악화는 반드시 FAIL" 이다. 라이브 자기검증
(bench/fixtures)이 실환경에서 그걸 보이고, 여기는 규칙 자체가 흔들리지 않게 묶는다.
"""
import json
import math
import os
import pathlib

import pytest

from bench import judge as J


R1 = {"id": "a", "text": "t1", "tags": ["x"], "occurred_start": "2026-01-01", "mentioned_at": "2026-01-01",
      "occurred_end": None, "scores": {"final": 1.0}}
R2 = {"id": "b", "text": "t2", "tags": [], "occurred_start": "2026-01-02", "mentioned_at": "2026-01-03",
      "scores": {"final": 0.9}}
R3 = {"id": "c", "text": "t3", "scores": {"final": 0.5}}


# ---------------------------------------------------------------- 결과 1건

def test_identical():
    assert J.compare_result(R1, dict(R1)) == "identical"


def test_lossless_empty_and_twin_removal():
    pruned = {k: v for k, v in R1.items() if k not in ("occurred_end", "mentioned_at")}
    assert J.compare_result(R1, pruned) == "lossless"          # None 제거 + occurred_start 와 같은 값


def test_lossy_when_non_derivable_field_dropped():
    pruned = {k: v for k, v in R1.items() if k != "scores"}
    assert J.compare_result(R1, pruned) == "lossy"


def test_lossy_when_value_changes():
    assert J.compare_result(R1, {**R1, "text": "other"}) == "lossy"


def test_twin_must_survive_in_candidate():
    # mentioned_at 은 occurred_start 와 같지만 occurred_start 까지 빠지면 복원 불가
    pruned = {k: v for k, v in R1.items() if k not in ("occurred_start", "mentioned_at")}
    assert J.compare_result(R1, pruned) == "lossy"


def test_coincidental_equality_is_not_derivable():
    # 지운 text 가 우연히 id 와 같아도 선언된 파생 쌍이 아니면 손실이다 (리뷰 P1)
    base = {"id": "abc", "text": "abc", "x": 1}
    assert J.compare_result(base, {"id": "abc", "x": 1}) == "lossy"


def test_type_change_is_lossy():
    assert J.compare_result({"id": "a", "v": 1}, {"id": "a", "v": 1.0}) == "lossy"
    assert J.compare_result({"id": "a", "v": 1}, {"id": "a", "v": True}) == "lossy"


RC = {"id": "w", "text": "t", "document_id": "doc-1", "chunk_id": "bank-a_doc-1_0"}


def test_chunk_id_is_derivable_from_bank_and_document_id():
    # 형식 파생 규칙: chunk_id == f"{bank}_{document_id}_0" 이면 남은 document_id 와 bank 로 복원된다
    pruned = {k: v for k, v in RC.items() if k != "chunk_id"}
    assert J.compare_result(RC, pruned, bank="bank-a") == "lossless"


def test_chunk_id_rule_refuses_unrecoverable_drops():
    # 거짓 합격 가드: 복원 근거가 없으면 chunk_id 제거는 손실이다
    drop = lambda r: {k: v for k, v in r.items() if k != "chunk_id"}
    assert J.compare_result(RC, drop(RC)) == "lossy"                        # bank 모름
    assert J.compare_result(RC, drop(RC), bank="bank-b") == "lossy"         # 다른 bank
    nz = {**RC, "chunk_id": "bank-a_doc-1_3"}
    assert J.compare_result(nz, drop(nz), bank="bank-a") == "lossy"         # 0 번이 아닌 chunk
    no_doc = {k: v for k, v in RC.items() if k not in ("chunk_id", "document_id")}
    assert J.compare_result(RC, no_doc, bank="bank-a") == "lossy"           # document_id 까지 빠짐


def test_added_field_is_held_not_ok():
    # 정보 손실은 없지만 임의 텍스트를 끼워 넣을 수 있다 -> 보류 (재리뷰 P2-3)
    assert J.compare_result(R1, {**R1, "pad": "x"}) == "added"
    assert "added" in J.ACC_HOLD and "extended" in J.ACC_HOLD


# ---------------------------------------------------------------- 결과 목록

def test_list_classes():
    assert J.compare_results([R1, R2], [R1, R2]) == "identical"
    assert J.compare_results([R1, R2, R3], [R1, R2]) == "prefix-cut"
    assert J.compare_results([R1, R2], [R1, R2, R3]) == "extended"
    assert J.compare_results([R1, R2], [R2, R1]) == "changed"
    assert J.compare_results([R1, R2], [R1, R3]) == "changed"
    assert J.compare_results([R1, R2], [R1, {**R2, "text": "z"}]) == "lossy"


def test_extended_requires_lossless_common_prefix():
    assert J.compare_results([R1], [{**R1, "text": "z"}, R2]) == "lossy"


def test_empty_lists():
    assert J.compare_results([], []) == "identical"
    assert J.compare_results([R1], []) == "prefix-cut"


# ---------------------------------------------------------------- 쿼리 판정 (비결정 기준 허용)

def test_nondeterministic_baseline_accepts_any_observed_variant():
    base = [("ok", [R1, R2]), ("ok", [R2, R1])]
    assert J.accuracy_for_query(base, [("ok", [R2, R1])])["class"] == "identical"
    assert J.accuracy_for_query(base, [("ok", [R1, R3])])["class"] == "changed"


def test_worst_candidate_observation_wins():
    base = [("ok", [R1, R2])]
    assert J.accuracy_for_query(base, [("ok", [R1, R2]), ("ok", [R1])])["class"] == "prefix-cut"


def test_candidate_error_not_excused_by_flaky_baseline():
    base = [("ok", [R1]), ("tool-error", None)]
    assert J.accuracy_for_query(base, [("tool-error", None)])["class"] == "outcome-diff"


def test_outcome_difference():
    assert J.accuracy_for_query([("ok", [R1])], [("tool-error", None)])["class"] == "outcome-diff"
    assert J.accuracy_for_query([("tool-error", None)], [("tool-error", None)])["class"] == "identical"


# ---------------------------------------------------------------- 통계·축 판정

def test_speed_verdict_thresholds():
    assert J.SPEED_MARGIN == 0.01
    assert J.speed_verdict(0.95, 0.94, 0.985) == "improved"
    assert J.speed_verdict(1.00, 0.995, 1.005) == "non-inferior"
    assert J.speed_verdict(1.03, 1.015, 1.04) == "regressed"
    assert J.speed_verdict(1.005, 0.99, 1.02) == "inconclusive"     # 비열등성 미증명
    assert J.speed_verdict(math.nan, 0, 0) == "inconclusive"
    assert J.speed_verdict(1.00, 0.995, 1.007, excess=0.005) == "inconclusive"   # 초과분만큼 좁아짐
    assert J.speed_verdict(1.10, 1.05, 1.15, excess=0.5) == "regressed"          # 악화 문턱은 그대로


def test_cost_verdict_is_per_query_with_zero_tolerance():
    q = lambda inc, dec: {"inc": inc, "dec": dec}
    assert J.cost_verdict({"a": q(False, True), "b": q(False, False)}, 100, 90) == "improved"
    assert J.cost_verdict({"a": q(False, False)}, 100, 100) == "unchanged"
    # 합계가 줄어도 한 쿼리가 늘면 트레이드오프 -> 악화
    assert J.cost_verdict({"a": q(False, True), "b": q(True, False)}, 100, 80) == "regressed"


def test_bootstrap_is_seeded_and_brackets_stat():
    vals = [math.log(x) for x in (0.9, 1.0, 1.1, 0.95, 1.05, 1.02)]
    a = J.bootstrap_ci(vals, J.geo_mean_ratio)
    assert a == J.bootstrap_ci(vals, J.geo_mean_ratio)
    assert a[1] <= a[0] <= a[2]


def test_p95_interpolates():
    assert J.p95([1, 2, 3, 4, 5]) == pytest.approx(4.8)


# ---------------------------------------------------------------- 종단 판정 (합성 run)

def _write_run(tmp, arms, n_q=40, rounds=3, lat=None, tok=None, results=None, queue=None, meta=None,
               extra=None):
    run = tmp / "run"
    (run / "results").mkdir(parents=True)
    json.dump({"run_id": "t", "split": "dev", "rounds": rounds, "bench_clock": "2026-01-01T00:00:00Z",
               **(meta or {})}, open(run / "meta.json", "w"))
    with open(run / "calls.jsonl", "w") as fh:
        for r in range(rounds):
            for q in range(n_q):
                key = f"q{q:02d}"
                for arm in arms:
                    res = (results or {}).get(arm, lambda q: [R1, R2])(q)
                    digest = f"{arm}{q}"
                    json.dump(res, open(run / "results" / f"{arm}.{key}.{digest}.json", "w"))
                    base_lat = 5000 + 37 * q + 11 * r
                    lf = (lat or {}).get(arm, lambda b: b)
                    rec = {"phase": "measure", "arm": arm, "key": key, "round": r, "outcome": "ok",
                           "latency_ms": lf(base_lat) if lf.__code__.co_argcount == 1 else lf(base_lat, q, r),
                           "tokens": (tok or {}).get(arm, lambda q: 1000)(q) if callable((tok or {}).get(arm))
                           else (tok or {}).get(arm, 1000), "results_digest": digest,
                           "tei_queue_before": (queue or {}).get(arm, lambda q, r: 0)(q, r)}
                    rec.update((extra or {}).get(arm, lambda q, r: {})(q, r))
                    fh.write(json.dumps(rec) + "\n")
    return str(run)


def test_aa_is_no_change(tmp_path):
    run = _write_run(tmp_path, ["a", "b"])
    assert J.judge(run, "a", "b")["verdict"] == "NO-CHANGE"


def test_slower_candidate_fails(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 1.10})
    rep = J.judge(run, "a", "b")
    assert rep["speed"]["verdict"] == "regressed" and rep["verdict"] == "FAIL"


def test_faster_candidate_passes(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.85})
    rep = J.judge(run, "a", "b")
    assert rep["speed"]["verdict"] == "improved" and rep["verdict"] == "PASS"


def test_cheaper_lossless_candidate_passes(tmp_path):
    pruned = lambda q: [{k: v for k, v in R1.items() if k not in ("occurred_end", "mentioned_at")}, R2]
    run = _write_run(tmp_path, ["a", "b"], tok={"b": 800}, results={"b": pruned})
    rep = J.judge(run, "a", "b")
    assert rep["accuracy"]["verdict"] == "invariant"
    assert rep["cost"]["verdict"] == "improved" and rep["verdict"] == "PASS"


def test_cheaper_but_lossy_is_a_tradeoff_and_fails(tmp_path):
    lossy = lambda q: [{k: v for k, v in R1.items() if k != "scores"}, R2]
    run = _write_run(tmp_path, ["a", "b"], tok={"b": 800}, results={"b": lossy})
    assert J.judge(run, "a", "b")["verdict"] == "FAIL"


def test_run_bank_flows_into_accuracy(tmp_path):
    pruned = lambda q: [{k: v for k, v in RC.items() if k != "chunk_id"}]
    run = _write_run(tmp_path, ["a", "b"], tok={"b": 900}, results={"a": lambda q: [RC], "b": pruned},
                     meta={"bank": "bank-a"})
    rep = J.judge(run, "a", "b")
    assert rep["accuracy"]["verdict"] == "invariant" and rep["verdict"] == "PASS"
    old = _write_run(tmp_path / "old", ["a", "b"], tok={"b": 900}, results={"a": lambda q: [RC], "b": pruned})
    assert J.judge(old, "a", "b")["verdict"] == "FAIL"        # 구 run(meta 에 bank 없음)은 규칙 미적용


def test_tail_cut_is_held(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], tok={"b": 700}, results={"b": lambda q: [R1]})
    rep = J.judge(run, "a", "b")
    assert rep["accuracy"]["verdict"] == "hold" and rep["verdict"] == "INCONCLUSIVE"


def test_faster_but_costlier_fails(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.8}, tok={"b": 1100})
    assert J.judge(run, "a", "b")["verdict"] == "FAIL"


def test_contamination_makes_speed_inconclusive(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.8},
                     queue={"b": lambda q, r: 1 if q % 3 == 0 else 0})
    rep = J.judge(run, "a", "b")
    assert rep["speed"]["contamination_rate"] > J.MAX_CONTAMINATED
    assert rep["speed"]["verdict"] == "inconclusive" and rep["verdict"] == "INCONCLUSIVE"


def test_tail_regression_overrides_mean(tmp_path):
    # 대부분 빨라졌지만 일부 쿼리가 크게 느려지면(꼬리 악화) 평균이 좋아도 FAIL
    run = _write_run(tmp_path, ["a", "b"], n_q=40,
                     lat={"b": lambda x: x * (0.97 if x < 6300 else 1.6)})
    rep = J.judge(run, "a", "b")
    assert rep["speed"]["tail_regressed"] and rep["verdict"] == "FAIL"


# ---------------------------------------------------------------- 측정 배치 (runner.arm_order)

def test_arm_order_balances_positions():
    import collections
    from bench.runner import arm_order
    for arms in (["a", "b"], ["a", "b", "c"], ["a", "b", "c", "d", "e"]):
        n = len(arms)
        seen = collections.Counter()
        for i in range(2 * n * 10):
            seq = arm_order(arms, 0, i)
            assert sorted(seq) == sorted(arms)
            seen.update((arm, p) for p, arm in enumerate(seq))
        assert len(set(seen.values())) == 1          # 모든 (팔, 자리) 조합이 같은 빈도


def test_arm_order_two_arms_is_abba():
    from bench.runner import arm_order
    assert [arm_order(["a", "b"], 0, i) for i in range(4)] == [["a", "b"], ["b", "a"], ["b", "a"], ["a", "b"]]


def test_aa_calibration_refuses_non_twin(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], tok={"b": 800})
    with pytest.raises(SystemExit):
        J.check_aa_twin(J.judge(run, "a", "b"))
    J.check_aa_twin(J.judge(_write_run(tmp_path / "x", ["a", "b"]), "a", "b"))   # 진짜 쌍둥이는 통과


# ---------------------------------------------------------------- 재리뷰(2026-09-23) 누수 차단

def test_exit_codes_separate_no_change_from_pass(tmp_path, capsys):
    assert J.EXIT == {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "NO-CHANGE": 3}
    run = _write_run(tmp_path, ["a", "b"])
    cold = _cold_file(tmp_path)                     # 속도 축에 cold 가 들어가므로 합격·무변화는 cold 가 있어야 말한다
    assert J.main([run, "--baseline", "a", "--candidate", "b", "--cold-report", cold]) == 3
    run = _write_run(tmp_path / "p", ["a", "b"], lat={"b": lambda x: x * 0.9})
    assert J.main([run, "--baseline", "a", "--candidate", "b", "--cold-report", cold]) == 0


def test_small_slowdown_with_cost_gain_is_not_pass(tmp_path):
    # 재리뷰 P2-1: m=2% 아래 1.5% 저하 + 비용 개선이 PASS 되던 트레이드오프
    pruned = lambda q: [{k: v for k, v in R1.items() if k not in ("occurred_end", "mentioned_at")}, R2]
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 1.015}, tok={"b": 900}, results={"b": pruned})
    rep = J.judge(run, "a", "b")
    assert rep["cost"]["verdict"] == "improved" and rep["verdict"] == "FAIL"


def test_per_query_cost_increase_hidden_by_total_fails(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], tok={"b": lambda q: 500 if q % 2 else 1010})
    rep = J.judge(run, "a", "b")
    assert rep["cost"]["rel_change"] < 0 and rep["cost"]["verdict"] == "regressed" and rep["verdict"] == "FAIL"


def test_extended_and_added_are_held(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], results={"b": lambda q: [R1, R2, R3]})
    assert J.judge(run, "a", "b")["verdict"] == "INCONCLUSIVE"
    run = _write_run(tmp_path / "x", ["a", "b"], results={"b": lambda q: [{**R1, "summary": "x"}, R2]})
    rep = J.judge(run, "a", "b")
    assert rep["accuracy"]["verdict"] == "hold" and rep["verdict"] == "INCONCLUSIVE"


def test_payload_rest_blocks_and_structured_are_compared():
    base = {"results": [R1], "payload_rest": {"chunks": {"c1": "x"}, "trace": None}, "blocks": [],
            "structured": "consistent"}
    assert J.compare_stored(base, {**base, "payload_rest": {"trace": None}}) == "lossy"       # chunks 제거
    assert J.compare_stored(base, {**base, "payload_rest": {"chunks": {"c1": "x"}}}) == "identical" or \
        J.compare_stored(base, {**base, "payload_rest": {"chunks": {"c1": "x"}}}) == "lossless"
    assert J.compare_stored(base, {**base, "blocks": ["note"]}) == "changed"
    assert J.compare_stored(base, {**base, "structured": "inconsistent"}) == "changed"
    assert J.compare_stored([R1], {**base}) in ("identical", "lossless")                        # 구형 저장


def test_upstream_error_is_outcome_diff():
    from bench import runner as RN
    msg = {"result": {"content": [{"type": "text", "text": json.dumps({"error": "boom", "results": []})}]}}
    assert RN.classify({"msg": msg, "latency_ms": 1})["outcome"] == "upstream-error"
    assert J.accuracy_for_query([("ok", [R1])], [("upstream-error", None)])["class"] == "outcome-diff"


def test_cache_like_state_dependent_latency_is_not_pass(tmp_path):
    # 재리뷰 P1-1: 첫 라운드만 미스(10% 느림)이고 이후 라운드가 적중(5%)하면 예전엔 PASS 100%
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x, q, r: x * (1.10 if r == 0 else 0.05)})
    rep = J.judge(run, "a", "b")
    assert rep["speed"]["order_effect"]["state_dependent"]
    assert rep["verdict"] != "PASS" and "state-dependent-latency(cache?)" in rep["reasons"]


def test_prod_overlap_and_unknown_queue_are_contamination(tmp_path):
    wall = lambda q, r: {"wall": f"2026-01-01T00:{q:02d}:{r * 10:02d}+00:00",
                         "wall_end": f"2026-01-01T00:{q:02d}:{r * 10 + 5:02d}+00:00"}
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.8}, meta={"prod_events": 30},
                     extra={"a": wall, "b": wall})
    with open(f"{run}/prod_events.jsonl", "w") as fh:
        for q in range(30):
            fh.write(json.dumps({"ts": f"2026-01-01T00:{q:02d}:02Z", "kind": "recall"}) + "\n")
    rep = J.judge(run, "a", "b")
    assert rep["speed"]["contamination_rate"] > J.MAX_CONTAMINATED and rep["verdict"] == "INCONCLUSIVE"
    assert J._contaminated({"tei_queue_before": None}, [])
    assert not J._contaminated({}, [])


def test_missing_prod_signal_blocks_speed(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.8}, meta={"prod_events": None})
    rep = J.judge(run, "a", "b")
    assert rep["verdict"] == "INCONCLUSIVE" and "no-prod-interference-signal" in rep["reasons"]


def test_too_few_queries_or_rounds_block_speed(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], n_q=10, lat={"b": lambda x: x * 0.8})
    assert J.judge(run, "a", "b")["verdict"] == "INCONCLUSIVE"
    run = _write_run(tmp_path / "r", ["a", "b"], rounds=1, lat={"b": lambda x: x * 0.8})
    assert J.judge(run, "a", "b")["verdict"] == "INCONCLUSIVE"


def test_replica_drift_and_rewrite_failures_invalidate(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.8},
                     meta={"freeze_before": {"x": 1}, "freeze_after": {"x": 2}})
    assert "replica-drift" in J.judge(run, "a", "b")["reasons"]
    run = _write_run(tmp_path / "w", ["a", "b"], lat={"b": lambda x: x * 0.8},
                     meta={"rewrite_failures_before": {"a": 0, "b": 0}, "rewrite_failures_after": {"a": 0, "b": 3}})
    rep = J.judge(run, "a", "b")
    assert rep["verdict"] == "INCONCLUSIVE" and "rewrite-failures:b" in rep["reasons"]


def test_carryover_unbalanced_blocks_speed(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.8}, meta={"carryover_balanced": False})
    assert J.judge(run, "a", "b")["verdict"] == "INCONCLUSIVE"


def test_aa_too_noisy_invalidates_run(tmp_path, capsys):
    # 대조가 기준보다 3% 느리면(초과 2% > m) run 무효 - 후보가 10% 빨라도 PASS 못 한다
    run = _write_run(tmp_path, ["a", "b", "c"], lat={"b": lambda x: x * 0.9, "c": lambda x: x * 1.03})
    assert J.main([run, "--baseline", "a", "--candidate", "b", "--aa-control", "c"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["speed"]["margin"] == J.SPEED_MARGIN and "aa-invalid" in out["reasons"]


def test_aa_excess_only_tightens(tmp_path, capsys):
    # 대조가 1.5% 흔들리면(초과 0.5%) 불악화 문턱이 1.01 -> 1.005 로 좁아진다: 0.7% 느린 후보는 불악화가 아니다
    pruned = lambda q: [{k: v for k, v in R1.items() if k not in ("occurred_end", "mentioned_at")}, R2]
    run = _write_run(tmp_path, ["a", "b", "c"], lat={"b": lambda x: x * 1.007, "c": lambda x: x * 1.015},
                     tok={"b": 900}, results={"b": pruned})
    assert J.judge(run, "a", "b")["verdict"] == "PASS"                        # 대조 없이는 통과
    assert J.main([run, "--baseline", "a", "--candidate", "b", "--aa-control", "c"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["aa_control"]["excess"] == pytest.approx(0.005, abs=1e-3) and out["speed"]["verdict"] == "inconclusive"
    # 뚜렷한 개선은 좁아진 문턱도 넘는다
    run = _write_run(tmp_path / "f", ["a", "b", "c"], lat={"b": lambda x: x * 0.9, "c": lambda x: x * 1.015})
    assert J.main([run, "--baseline", "a", "--candidate", "b", "--aa-control", "c",
                   "--cold-report", _cold_file(tmp_path)]) == 0


def test_holdout_verdict_requires_aa_control(tmp_path, capsys):
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.9}, meta={"split": "holdout"})
    assert J.main([run, "--baseline", "a", "--candidate", "b"]) == 2


# ---------------------------------------------------------------- final-review(2026-09-23) 수정

def test_non_ok_text_change_is_changed():
    assert J.compare_stored({"text": "RULE:x long message"}, {"text": "RULE:x long message"}) == "identical"
    assert J.compare_stored({"text": "RULE:x long message"}, {"text": "RULE:x"}) == "changed"
    assert J.accuracy_for_query([("tool-error", {"text": "a"})], [("tool-error", {"text": "b"})])["class"] == "changed"


def test_structured_drop_is_changed():
    base = {"results": [R1], "payload_rest": {}, "blocks": [], "structured": "consistent"}
    assert J.compare_stored(base, {**base, "structured": "absent"}) == "changed"
    assert J.compare_stored({**base, "structured": "absent"}, base) == "added"


def test_non_twin_aa_is_inconclusive_not_fail(tmp_path, capsys):
    run = _write_run(tmp_path, ["a", "b", "c"], lat={"b": lambda x: x * 0.9}, tok={"c": 900})
    assert J.main([run, "--baseline", "a", "--candidate", "b", "--aa-control", "c"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert any(p.startswith("aa-not-twin") for p in out["aa_control"]["problems"])


def test_accuracy_fail_survives_invalid_aa(tmp_path, capsys):
    lossy = lambda q: [{k: v for k, v in R1.items() if k != "scores"}, R2]
    run = _write_run(tmp_path, ["a", "b", "c"], results={"b": lossy}, tok={"c": 900})
    assert J.main([run, "--baseline", "a", "--candidate", "b", "--aa-control", "c"]) == 1


def test_prod_window_covers_both_sides_of_ts(tmp_path):
    (tmp_path / "prod_events.jsonl").write_text(json.dumps({"ts": "2026-01-01T00:00:10Z", "kind": "context-triggers"}) + "\n")
    (w,) = J.load_prod_windows(str(tmp_path), {"prod_events": 1})
    call = {"wall": "2026-01-01T00:00:00.500+00:00", "wall_end": "2026-01-01T00:00:01.500+00:00", "latency_ms": 1000}
    assert J._overlaps_prod(call, [w])                          # ts 이전 구간도 겹침으로 본다


def test_prod_window_mental_models_covers_async_refresh_tail(tmp_path):
    # refresh·create 는 upstream 비동기 operation 이라 도착 후 120s 를 넘겨 끝나는 경우가 18% (issue #68, 실측 p90 151s)
    (tmp_path / "prod_events.jsonl").write_text(json.dumps({"ts": "2026-01-01T00:00:00Z", "kind": "mental-models"}) + "\n")
    (w,) = J.load_prod_windows(str(tmp_path), {"prod_events": 1})
    call = {"wall": "2026-01-01T00:03:20+00:00", "wall_end": "2026-01-01T00:03:21+00:00", "latency_ms": 1000}
    assert J._overlaps_prod(call, [w])
    assert J.PROD_WINDOW_S["reflect"] == 120 and J.PROD_WINDOW_S["recall"] == 15   # 나머지 종류는 그대로


# ---------------------------------------------------------------- 순환 교차 run (issue #13 B)

def test_crossover_run_uses_mean_so_instance_offsets_cancel(tmp_path):
    # 팔 b 가 라운드(=교차 단계)마다 다른 인스턴스에서 +20%, -10%, -10% 오프셋을 받는다: 평균 로그는 ~0 이지만
    # 중앙값은 -10% 쪽으로 쏠린다. 교차 run 은 평균으로 내야 인스턴스 효과가 상쇄된다.
    off = {0: 1.2, 1: 0.9, 2: 1 / (1.2 * 0.9)}
    lat = {"b": lambda x, q, r: x * off[r]}
    plain = _write_run(tmp_path / "p", ["a", "b"], lat=lat)
    cross = _write_run(tmp_path / "c", ["a", "b"], lat=lat, meta={"crossover": True})
    assert J.judge(plain, "a", "b")["speed"]["geo_mean_ratio"] < 0.95        # 중앙값 = 0.9 쪽
    rep = J.judge(cross, "a", "b")
    assert abs(rep["speed"]["geo_mean_ratio"] - 1.0) < 1e-6 and rep["verdict"] == "NO-CHANGE"


@pytest.mark.parametrize("effect,verdict", [(1.10, "FAIL"), (0.90, "PASS")])
def test_crossover_still_detects_real_effects(tmp_path, effect, verdict):
    off = {0: 1.2, 1: 0.9, 2: 1 / (1.2 * 0.9)}
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x, q, r: x * off[r] * effect}, meta={"crossover": True})
    assert J.judge(run, "a", "b")["verdict"] == verdict


def test_crossover_speed_uses_only_queries_that_kept_every_phase(tmp_path):
    """단계가 빠진 쿼리는 인스턴스 오프셋이 상쇄되지 않는다(issue #13 결함 02) - 속도 판정에서 빼고 개수를 싣는다."""
    off = {0: 1.2, 1: 0.9, 2: 1 / (1.2 * 0.9)}
    lost = {"b": lambda q, r: {"outcome": "error"} if q < 20 and r == 0 else {}}
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x, q, r: x * off[r]}, meta={"crossover": True}, extra=lost)
    sp = J.judge(run, "a", "b")["speed"]
    assert abs(sp["geo_mean_ratio"] - 1.0) < 1e-6
    assert sp["n_queries"] == 20 and sp["incomplete_phase_queries"] == 20


# ---------------------------------------------------------------- 교차 run 계획 대비 실제 (issue #77)

OFF3 = {0: 1.2, 1: 0.9, 2: 1 / (1.2 * 0.9)}
TRUNC = {"at": "2026-10-02T01:00:00+00:00", "reason": "ops-protection: 503 spike", "laps": 1, "planned_phases": 3}


def _planned_run(tmp, effect, planned, truncs=(), rounds=3, with_plan=True, plan_edit=None):
    """3팔 교차 run 하나(쿼리 40개, 단계 rounds 개 = 1바퀴, c 는 a 의 쌍둥이). planned = 계획 단계 수,
    truncs = 계획 파일에 덧붙은 축소 기록. 병합처럼 바퀴가 온전한 모양이라 바퀴 집계(issue #82)에서도 OFF3 이 상쇄된다."""
    plan = {"version": 1, "arms": [{"name": "a"}, {"name": "b"}, {"name": "c"}], "sets": ["s1", "s2", "s3"],
            "laps": planned // 3, "planned_phases": planned, "recorded_at": "2026-10-02T00:00:00+00:00",
            "truncations": list(truncs), "actual_phases": rounds}
    if plan_edit is not None:
        plan = plan_edit(plan)
    return _write_run(tmp, ["a", "b", "c"], rounds=rounds, lat={"b": lambda x, q, r: x * OFF3[r] * effect},
                      meta={"crossover": True, "arms": {"a": 1, "b": 2, "c": 3},
                            **({"plan": plan} if with_plan else {})})


# 계획 상태 x 효과(후보 b 가 a 보다 10% 빠름 / 같음 / 10% 느림). 기대값은 손으로 정했다:
#   계획대로(3 = 3) 와 사유 있는 축소(6 -> 3 기록, 실제 3)는 기존 판정(PASS / NO-CHANGE / FAIL) 그대로,
#   사유 없는 축소(6, 기록 없음, 실제 3)와 기록보다도 짧은 실제(9 -> 6 기록, 실제 3)는 효과와 무관하게 INCONCLUSIVE.
RECORDED_TO_3 = [dict(TRUNC, planned_phases=3)]
RECORDED_TO_6 = [dict(TRUNC, planned_phases=6)]


@pytest.mark.parametrize("planned,truncs,held", [(3, [], False), (6, [], True), (6, RECORDED_TO_3, False),
                                                 (9, RECORDED_TO_6, True)],
                         ids=["as-planned", "short-unrecorded", "short-recorded", "shorter-than-recorded"])
@pytest.mark.parametrize("effect,usual", [(0.9, "PASS"), (1.0, "NO-CHANGE"), (1.1, "FAIL")])
def test_crossover_run_shorter_than_its_plan_is_held_unless_the_shortfall_is_recorded(
        tmp_path, effect, usual, planned, truncs, held):
    rep = J.judge(_planned_run(tmp_path, effect, planned, truncs), "a", "b")
    assert any(r.startswith("plan-truncated:") for r in rep["reasons"]) is held
    assert rep["verdict"] == ("INCONCLUSIVE" if held else usual)
    if held:
        assert rep["speed"]["verdict"] == "inconclusive"


def test_plan_truncated_reason_names_actual_and_effective_planned_phases(tmp_path):
    assert "plan-truncated:3<6" in J.judge(_planned_run(tmp_path / "u", 1.0, 6), "a", "b")["reasons"]
    assert "plan-truncated:3<6" in J.judge(_planned_run(tmp_path / "r", 1.0, 9, RECORDED_TO_6), "a", "b")["reasons"]


def test_result_reports_actual_against_plan_and_the_truncation_reason(tmp_path):
    rep = J.judge(_planned_run(tmp_path / "r", 0.9, 6, RECORDED_TO_3), "a", "b")
    assert rep["plan"] == {"planned_phases": 6, "effective_planned_phases": 3, "actual_phases": 3, "truncated": True,
                           "truncations": RECORDED_TO_3, "unexplained_shortfall": 0}
    assert rep["plan"]["truncations"][0]["reason"] == "ops-protection: 503 spike"
    rep = J.judge(_planned_run(tmp_path / "u", 0.9, 6), "a", "b")
    assert rep["plan"]["truncated"] and rep["plan"]["truncations"] == [] and rep["plan"]["unexplained_shortfall"] == 3
    assert J.judge(_planned_run(tmp_path / "f", 0.9, 3), "a", "b")["plan"]["truncated"] is False


@pytest.mark.parametrize("effect", [0.9, 1.0, 1.1])
def test_a_run_that_matches_its_plan_is_judged_exactly_like_one_without_a_plan(tmp_path, effect):
    with_plan = J.judge(_planned_run(tmp_path / "p", effect, planned=3), "a", "b")
    without = J.judge(_planned_run(tmp_path / "n", effect, planned=3, with_plan=False), "a", "b")
    assert with_plan.pop("plan")["unexplained_shortfall"] == 0
    # 1바퀴 run 의 바퀴 집계는 전 단계 평균과 같은 값이다 - 다른 것은 바퀴 보고 키뿐
    assert with_plan["speed"].pop("laps") == {"size": 3, "complete_lap_phases": 3, "queries_with_dropped_laps": 0}
    assert with_plan == without


@pytest.mark.parametrize("effect", [0.9, 1.0, 1.1])
def test_a_run_without_plan_meta_skips_the_plan_check_and_adds_no_result_key(tmp_path, effect):
    """계획 기록 이전 형식 run(meta.plan 없음)은 판정 규칙·판정 JSON 이 그대로다 - 키 하나도 늘지 않는다."""
    rep = J.judge(_planned_run(tmp_path, effect, planned=6, with_plan=False), "a", "b")
    assert "plan" not in rep and not any(r.startswith("plan-truncated") for r in rep["reasons"])
    assert set(rep) == {"run_id", "baseline", "candidate", "split", "n_queries", "rounds", "bench_clock", "verdict",
                        "reasons", "accuracy", "cost", "speed", "provenance", "configs"}


def test_an_aa_control_held_by_plan_truncated_is_reported_as_aa_blocked(tmp_path):
    """final-review C4: 계획보다 짧은 run 의 A/A 대조는 다른 속도 보류 사유처럼 aa-blocked 로 보고된다."""
    held = J.aa_gate(J.judge(_planned_run(tmp_path / "h", 1.0, 6), "a", "b"))
    assert any(p.startswith("aa-blocked:plan-truncated:3<6") for p in held)
    ok = J.aa_gate(J.judge(_planned_run(tmp_path / "o", 1.0, 3), "a", "b"))
    assert not any("plan-" in p for p in ok)


def _drop(key):
    return lambda plan: {k: v for k, v in plan.items() if k != key}


def _trunc_without_planned_phases(plan):
    return dict(plan, truncations=[{"at": TRUNC["at"], "reason": "r", "laps": 1}])


def _trunc_with(**edit):
    """유효한 축소 기록(TRUNC)에서 필드 몇 개만 망가뜨린다. 값이 _MISSING 이면 그 키를 뺀다(issue #100)."""
    def apply(plan):
        t = {k: v for k, v in dict(TRUNC, **edit).items() if v is not _MISSING}
        return dict(plan, truncations=[t])
    return apply


_MISSING = object()


# 해석할 수 없는 meta.plan(손으로 고침·다른 버전·필드 누락) x 효과: 판정기는 KeyError 로 죽지 않고 plan-invalid 로 보류한다
INVALID_PLANS = {
    "version-2": lambda plan: dict(plan, version=2),
    "version-missing": _drop("version"),
    "version-bool": lambda plan: dict(plan, version=True),
    "planned-phases-missing": _drop("planned_phases"),
    "planned-phases-string": lambda plan: dict(plan, planned_phases="6"),
    "planned-phases-zero": lambda plan: dict(plan, planned_phases=0),
    "truncations-missing": _drop("truncations"),
    "truncations-not-list": lambda plan: dict(plan, truncations="x"),
    "truncation-without-target": _trunc_without_planned_phases,
    "plan-not-object": lambda plan: ["not", "an", "object"],
    # plan.arms 가 병합 meta.arms 와 개수·이름이 다르면(손으로 고침) 바퀴 경계가 어긋난다(issue #106)
    "arms-renamed": lambda plan: dict(plan, arms=[{"name": "a"}, {"name": "b"}, {"name": "z"}]),
    "arms-fewer": lambda plan: dict(plan, arms=[{"name": "a"}, {"name": "b"}]),
    "arms-more": lambda plan: dict(plan, arms=[{"name": n} for n in "abcd"]),
    "arms-duplicate": lambda plan: dict(plan, arms=[{"name": "a"}, {"name": "a"}, {"name": "b"}]),
    "arms-missing": _drop("arms"),
    "arms-empty": lambda plan: dict(plan, arms=[]),
    "arms-not-list": lambda plan: dict(plan, arms="abc"),
    "arms-element-not-object": lambda plan: dict(plan, arms=["a", "b", "c"]),
    "arms-name-not-string": lambda plan: dict(plan, arms=[{"name": 1}, {"name": 2}, {"name": 3}]),
    # 사유·시각 없는 축소는 인정하지 않는다(issue #100) - 손으로 고친 meta 가 plan-truncated 보류를 우회하지 못하게
    "truncation-reason-missing": _trunc_with(reason=_MISSING),
    "truncation-reason-blank": _trunc_with(reason="   "),
    "truncation-reason-empty": _trunc_with(reason=""),
    "truncation-reason-not-string": _trunc_with(reason=503),
    "truncation-at-missing": _trunc_with(at=_MISSING),
    "truncation-at-malformed": _trunc_with(at="yesterday"),
    "truncation-at-naive": _trunc_with(at="2026-10-02T01:00:00"),
    "truncation-at-not-string": _trunc_with(at=20261002),
}


@pytest.mark.parametrize("effect,usual", [(0.9, "PASS"), (1.1, "FAIL")])
@pytest.mark.parametrize("shape", sorted(INVALID_PLANS))
def test_an_unreadable_plan_holds_the_speed_verdict_instead_of_crashing(tmp_path, shape, effect, usual):
    ok = J.judge(_planned_run(tmp_path / "ok", effect, planned=3), "a", "b")
    assert ok["verdict"] == usual                                   # 같은 데이터가 계획대로였으면 이 판정이다
    rep = J.judge(_planned_run(tmp_path / "bad", effect, planned=3, plan_edit=INVALID_PLANS[shape]), "a", "b")
    assert rep["verdict"] == "INCONCLUSIVE" and rep["speed"]["verdict"] == "inconclusive"
    assert any(r.startswith("plan-invalid:") for r in rep["reasons"])
    assert set(rep["plan"]) == {"invalid"}


def test_plan_arms_in_a_different_order_than_meta_arms_is_still_valid(tmp_path):
    """meta.arms 는 dict 라 순서가 의미 없다 - 개수·이름만 같으면 plan-invalid 가 아니다(issue #106)."""
    rep = J.judge(_planned_run(tmp_path, 0.9, 3, plan_edit=lambda plan: dict(plan, arms=[{"name": n} for n in "cba"])),
                  "a", "b")
    assert "invalid" not in rep["plan"] and rep["verdict"] == "PASS"


def test_meta_arms_edited_away_from_the_plan_is_plan_invalid(tmp_path):
    run = _planned_run(tmp_path, 0.9, 3)
    meta = json.load(open(run + "/meta.json"))
    meta["arms"] = {"a": 1, "b": 2}                          # 계획은 3팔
    json.dump(meta, open(run + "/meta.json", "w"))
    rep = J.judge(run, "a", "b")
    assert rep["verdict"] == "INCONCLUSIVE" and "plan-invalid:arms" in rep["reasons"]


@pytest.mark.parametrize("effect", [0.9, 1.0, 1.1])
@pytest.mark.parametrize("plan_edit", [None, lambda plan: dict(plan, arms=[{"name": "x"}])], ids=["intact", "arms-edited"])
def test_a_run_without_plan_meta_ignores_plan_arms_validation(tmp_path, effect, plan_edit):
    """meta.plan 이 없는 과거 run 은 arms 검증 대상이 아니다 - 같은 데이터의 판정이 그대로다(issue #106)."""
    base = J.judge(_planned_run(tmp_path / "n", effect, planned=3, with_plan=False), "a", "b")
    again = J.judge(_planned_run(tmp_path / "m", effect, planned=3, with_plan=False, plan_edit=plan_edit), "a", "b")
    assert base == again and not any(r.startswith("plan-invalid") for r in again["reasons"])


@pytest.mark.parametrize("shape", [s for s in sorted(INVALID_PLANS) if s.startswith("truncation-") and s != "truncation-without-target"])
def test_a_truncation_without_reason_or_time_cannot_hide_a_short_run(tmp_path, shape):
    """issue #100 의 우회 시나리오: 계획 6, 실제 3 인데 meta 의 축소 기록을 손으로 고쳐 사유·시각을 지웠다 -
    예전엔 정상 축소로 인정돼 plan-truncated 보류가 사라졌다. 이제 plan-invalid 로 보류한다."""
    rep = J.judge(_planned_run(tmp_path, 0.9, 6, plan_edit=INVALID_PLANS[shape]), "a", "b")
    assert rep["verdict"] == "INCONCLUSIVE" and rep["speed"]["verdict"] == "inconclusive"
    assert any(r.startswith("plan-invalid:truncations") for r in rep["reasons"])


@pytest.mark.parametrize("now", [None,"2026-10-02T01:00:00+00:00"], ids=["now-omitted", "now-given"])
def test_a_truncation_written_by_crossover_truncate_plan_is_accepted(tmp_path, now):
    """issue #100: 정상 경로의 유일한 작성자(crossover.truncate_plan, now 생략 포함)가 쓴 기록은 plan-invalid 가 아니다 -
    검증 기준이 작성자와 어긋나면 모든 축소가 보류된다."""
    from bench import crossover as X
    out = str(tmp_path / "plan")
    X.record_plan(out, ["a", "b", "c"], ["s1", "s2", "s3"], laps=2)
    doc = X.truncate_plan(out, 1, "  ops-protection: 503 spike  ", now=now)
    written = doc["truncations"]
    assert len(written) == 1 and written[0]["planned_phases"] == 3
    rep = J.judge(_planned_run(tmp_path / "r", 0.9, 6, plan_edit=lambda plan: dict(plan, truncations=written)), "a", "b")
    assert rep["plan"]["truncations"] == written and "invalid" not in rep["plan"]
    assert not any(r.startswith(("plan-invalid:", "plan-truncated:")) for r in rep["reasons"])
    assert rep["verdict"] == "PASS"


def test_an_aa_control_with_an_unreadable_plan_is_reported_as_aa_blocked(tmp_path):
    run = _planned_run(tmp_path, 1.0, planned=3, plan_edit=INVALID_PLANS["version-2"])
    assert any(p.startswith("aa-blocked:plan-invalid:") for p in J.aa_gate(J.judge(run, "a", "b")))


# ---------------------------------------------------------------- 새 채택 기준: 꼬리 지표 비열등 (issue #13 티켓 6)

@pytest.mark.parametrize("ratio,lo,hi,noise,want", [
    (0.98, 0.96, 1.00, 0.0, "ok"),            # 상한이 1 이하
    (1.004, 1.001, 1.006, 0.007, "ok"),       # 상한이 허용 폭 안
    (1.03, 1.02, 1.05, 0.007, "regressed"),   # 하한이 잡음폭 밖
    (1.008, 0.99, 1.03, 0.007, "hold"),       # 가를 수 없다
    (math.nan, math.nan, math.nan, 0.0, "hold"),
])
def test_noninferiority(ratio, lo, hi, noise, want):
    assert J.noninferiority(ratio, lo, hi, noise) == want


def test_aa_noise_never_exceeds_the_old_p95_allowance():
    """A/A 잡음폭은 문턱을 조금 넓히지만 옛 p95 규칙(2m)보다 넓히지 않는다."""
    assert J.aa_noise_of({"speed": {"tail_metrics": {"p95": {"ratio": 1.2}, "p99": {"ratio": 0.993}}}}) == \
        {"p95": 2 * J.SPEED_MARGIN, "p99": pytest.approx(0.007)}


def test_warm_drops_the_first_measured_call_of_each_arm_in_each_round(tmp_path):
    """idle 뒤 첫 호출(cold)은 warm 꼬리에 넣지 않는다 - 단계마다 팔별 첫 측정 호출을 뺀다."""
    spike = {"b": lambda x, q, r: x * (1.5 if q == 0 else 0.85)}      # q00 = 라운드마다 b 의 첫 호출
    rep = J.judge(_write_run(tmp_path, ["a", "b"], lat=spike), "a", "b")
    tm = rep["speed"]["tail_metrics"]
    assert tm["p99"]["verdict"] == "ok" and tm["p99"]["n"] == 39 * 3
    assert rep["verdict"] == "PASS"


def test_p99_regression_fails_even_when_the_mean_improves(tmp_path):
    slow_top = {"b": lambda x, q, r: x * (1.3 if q >= 38 else 0.97)}
    rep = J.judge(_write_run(tmp_path, ["a", "b"], lat=slow_top), "a", "b")
    assert rep["speed"]["tail_metrics"]["p99"]["verdict"] == "regressed"
    assert rep["verdict"] == "FAIL"


def test_p99_increase_inside_aa_noise_passes_but_not_without_it(tmp_path):
    """허용 폭은 평균과 같은 m(1%)이고, A/A 대조가 더 흔들렸으면 그 잡음폭(최대 2m)까지 넓어진다."""
    small = {"b": lambda x, q, r: x * (1.015 if q >= 34 else 0.95)}
    run = _write_run(tmp_path, ["a", "b"], lat=small)
    assert J.judge(run, "a", "b")["speed"]["tail_metrics"]["p99"]["verdict"] == "regressed"
    rep = J.judge(run, "a", "b", aa_noise={"p95": 0.02, "p99": 0.02})
    assert rep["speed"]["tail_metrics"]["p99"]["verdict"] == "ok" and rep["verdict"] == "PASS"


def test_undecidable_tail_is_a_hold_with_the_metric_and_sample_size(tmp_path):
    jitter = {"b": lambda x, q, r: x * 0.95 * (1.12 if (q, r) == (39, 0) else 1.0)}
    rep = J.judge(_write_run(tmp_path, ["a", "b"], lat=jitter), "a", "b")
    assert rep["speed"]["tail_metrics"]["p99"]["verdict"] == "hold"
    assert rep["verdict"] == "INCONCLUSIVE"
    assert any(r.startswith("tail-hold:p99") and "n=" in r for r in rep["reasons"])


def test_main_uses_the_aa_control_tail_noise(tmp_path, capsys):
    run = _write_run(tmp_path, ["a", "ctrl", "b"], lat={"ctrl": lambda x, q, r: x * (1.004 if q >= 38 else 1.0),
                                                        "b": lambda x, q, r: x * (1.004 if q >= 38 else 0.95)})
    J.main([run, "--baseline", "a", "--candidate", "b", "--aa-control", "ctrl"])
    rep = json.loads(capsys.readouterr().out)
    p99 = rep["speed"]["tail_metrics"]["p99"]
    assert p99["aa_noise"] == pytest.approx(0.004, abs=1e-4) and p99["tolerance"] == J.SPEED_MARGIN
    assert p99["verdict"] == "ok"


# ---------------------------------------------------------------- 벤치 간섭 가드 (issue #19)

def _ep(s):
    return J._ts(s)


def _guarded_run(tmp_path, guard):
    wall = lambda q, r: {"wall": f"2026-01-01T00:{q:02d}:{r * 10:02d}+00:00",
                         "wall_end": f"2026-01-01T00:{q:02d}:{r * 10 + 5:02d}+00:00"}
    # 후보는 가드가 멈춘 구간(쿼리 0~4)에서만 느리다 - 스왑 압박이 만든 지연이지 후보 설정의 성질이 아니다
    return _write_run(tmp_path, ["a", "b"], lat={"b": lambda x, q, r: x * 1.5 if q < 5 else x},
                      meta={} if guard is None else {"guard": guard}, extra={"a": wall, "b": wall})


def test_calls_inside_guard_windows_are_excluded_from_speed(tmp_path):
    win = [_ep("2026-01-01T00:00:00+00:00") - 1, _ep("2026-01-01T00:04:59+00:00")]
    rep = J.judge(_guarded_run(tmp_path, {"windows": [win], "aborted": None}), "a", "b")
    assert rep["speed"]["contaminated_pairs"] == 15 and rep["verdict"] == "NO-CHANGE"


def test_without_guard_windows_the_same_slowdown_fails(tmp_path):
    rep = J.judge(_guarded_run(tmp_path, None), "a", "b")
    assert rep["verdict"] == "FAIL"


def test_guard_aborted_run_is_invalid(tmp_path):
    run = _write_run(tmp_path, ["a", "b"], meta={"guard": {"windows": [], "aborted": "swap-growth"}})
    rep = J.judge(run, "a", "b")
    assert rep["verdict"] == "INCONCLUSIVE" and "guard-aborted:swap-growth" in rep["reasons"]


# ---------------------------------------------------------------- cold 재생 합성 (issue #21)

def _warm(verdict="PASS"):
    return {"verdict": verdict, "reasons": [], "accuracy": {"verdict": "invariant"}, "cost": {"verdict": "unchanged"}}


def _cold_file(tmp_path, **kw):
    path = tmp_path / "cold-report.json"
    path.write_text(json.dumps(_cold(**kw)))
    return str(path)


def _cold(p50=(0.97, 0.95, 0.99), p95=(0.98, 0.95, 1.005), n=40, fb=0, fc=0, **over):
    """cold_replay analyze 의 report.json 모양. verdict·tolerance 는 analyze 의 참고값이라 합성이 다시 매긴다."""
    metric = lambda r: {"ratio": r[0], "ci95": [r[1], r[2]], "n": n, "short": max(0, 20 - n),
                        "tolerance": 0.01, "verdict": "ok"}
    rep = {"run_id": "cold-x", "baseline": "128x3", "candidate": "300x1", "idle_s": 300.0, "cold_definition_met": True,
           "aborted": None, "metrics": {"cold_p50": metric(p50), "cold_p95": metric(p95)},
           "failures": {"baseline": fb, "candidate": fc, "verdict": "regressed" if fc > fb else "ok"},
           "excluded": [], "prod_sidecar": {"503": 0}, "guard_windows": 0}
    rep.update(over)
    return rep


def test_cold_tail_regression_fails_a_warm_pass():
    rep = J.combine_cold(_warm("PASS"), _cold(p95=(1.06, 1.03, 1.09)))
    assert rep["verdict"] == "FAIL"
    assert rep["cold"]["metrics"]["cold_p95"]["verdict"] == "regressed"
    assert "cold-regressed:cold_p95" in rep["reasons"]


def test_too_few_clean_cold_pairs_hold_a_warm_pass_with_the_shortfall():
    """쌍 12개면 CI 가 좁아 보여도 판정하지 않는다(MIN_PAIRS 20) - 모자란 쌍 수를 사유에 싣는다."""
    rep = J.combine_cold(_warm("PASS"), _cold(p50=(0.97, 0.96, 0.98), p95=(0.98, 0.97, 0.99), n=12))
    assert rep["verdict"] == "INCONCLUSIVE"
    assert "cold-hold:cold_p50(n=12,short=8)" in rep["reasons"]
    assert "cold-hold:cold_p95(n=12,short=8)" in rep["reasons"]


def test_undecidable_cold_tail_holds_a_warm_pass():
    rep = J.combine_cold(_warm("PASS"), _cold(p95=(1.0, 0.96, 1.04)))
    assert rep["verdict"] == "INCONCLUSIVE" and "cold-hold:cold_p95(n=40,short=0)" in rep["reasons"]


def test_more_cold_recall_failures_fail_even_with_ok_ratios():
    """분위수 비는 40 쌍 중 1건 실패를 가린다 - 실패 수는 따로 본다."""
    rep = J.combine_cold(_warm("PASS"), _cold(fb=0, fc=1))
    assert rep["verdict"] == "FAIL" and "cold-failures:0->1" in rep["reasons"]
    assert J.combine_cold(_warm("PASS"), _cold(fb=1, fc=1))["verdict"] == "PASS"


@pytest.mark.parametrize("over, reason", [
    ({"aborted": "error:KeyboardInterrupt"}, "cold-invalid:aborted:error:KeyboardInterrupt"),
    ({"cold_definition_met": False, "idle_s": 30.0}, "cold-invalid:idle-30s<300s"),
])
def test_invalid_cold_run_makes_no_measured_claim(over, reason):
    """끊긴 run·시험 실행(idle 300s 미만)은 합격도 cold 악화도 말하지 않는다. warm 쪽 FAIL 은 그대로 둔다."""
    rep = J.combine_cold(_warm("PASS"), _cold(**over))
    assert rep["verdict"] == "INCONCLUSIVE" and reason in rep["reasons"]
    assert J.combine_cold(_warm("PASS"), _cold(p95=(1.06, 1.03, 1.09), **over))["verdict"] == "INCONCLUSIVE"
    assert J.combine_cold(_warm("FAIL"), _cold(**over))["verdict"] == "FAIL"


def test_main_combines_the_cold_report_and_exits_on_the_combined_verdict(tmp_path, capsys):
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.9})       # warm 단독 = PASS(0)
    cold = _cold(p95=(1.06, 1.03, 1.09), excluded=[{"probe": 3, "pair": 1, "mode": "300x1", "reasons": ["interference"]},
                                                   {"probe": 9, "pair": 4, "mode": "128x3", "reasons": ["idle-broken", "interference"]}])
    path = tmp_path / "report.json"
    path.write_text(json.dumps(cold))
    assert J.main([run, "--baseline", "a", "--candidate", "b", "--cold-report", str(path)]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["verdict"] == "FAIL" and rep["cold"]["metrics"]["cold_p95"]["verdict"] == "regressed"
    assert rep["cold"]["excluded_by_reason"] == {"interference": 2, "idle-broken": 1}
    assert rep["cold"]["failures"] == {"baseline": 0, "candidate": 0, "verdict": "ok"}
    assert rep["cold"]["prod_sidecar"] == {"503": 0} and rep["cold"]["guard_windows"] == 0


def test_unmeasurable_cold_holds_the_overall_verdict_but_keeps_the_warm_verdict(tmp_path, capsys):
    """로컬에서 cold 를 잴 수 없으면(#21: 벤치 사이드카 swapout) 전체 판정은 보류, warm 판정은 따로 남긴다."""
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.9})       # warm 단독 = PASS(0)
    code = J.main([run, "--baseline", "a", "--candidate", "b", "--cold-unmeasured", "bench-sidecar-swapout"])
    rep = json.loads(capsys.readouterr().out)
    assert code == 2 and rep["verdict"] == "INCONCLUSIVE" and rep["warm_verdict"] == "PASS"
    assert "cold-unmeasured:bench-sidecar-swapout" in rep["reasons"]
    assert rep["cold"] == {"verdict": "unmeasured", "reason": "bench-sidecar-swapout"}


def test_without_cold_input_a_warm_pass_is_held_as_cold_unmeasured(tmp_path, capsys):
    """cold 입력이 없으면 cold 를 모른다(#21 운영자 결정: 'cold 가 없거나 측정 불가면' 보류) - 플래그를 잊어도 합격이 새지 않는다."""
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.9})
    assert J.main([run, "--baseline", "a", "--candidate", "b"]) == 2
    rep = json.loads(capsys.readouterr().out)
    assert rep["warm_verdict"] == "PASS" and "cold-unmeasured:no-cold-input" in rep["reasons"]
    assert rep["cold"] == {"verdict": "unmeasured", "reason": "no-cold-input"}


def test_cold_report_and_cold_unmeasured_are_exclusive(tmp_path):
    run = _write_run(tmp_path, ["a", "b"])
    with pytest.raises(SystemExit):
        J.main([run, "--baseline", "a", "--candidate", "b", "--cold-report", "r.json", "--cold-unmeasured", "x"])


def test_empty_cold_flags_are_not_mistaken_for_no_flag(tmp_path, capsys):
    """final-review #21: 빈 사유도 '잴 수 없었다'는 주장이고, 빈 report 경로는 조용히 버려지지 않고 실패한다."""
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.9})
    assert J.main([run, "--baseline", "a", "--candidate", "b", "--cold-unmeasured", ""]) == 2
    assert json.loads(capsys.readouterr().out)["cold"] == {"verdict": "unmeasured", "reason": ""}
    with pytest.raises(OSError):
        J.main([run, "--baseline", "a", "--candidate", "b", "--cold-report", ""])


# 4개 warm verdict 를 만드는 run 모양. 골든 fixture(tests/fixtures/judge-golden/)는 이 모양으로 수정 전 코드(0ead79d)의
# main() 에 cold 인자 없이 돌려 캡처한 stdout 바이트·exit code 다. stdout 에 tmp 경로 같은 비결정 값이 없고 부트스트랩이 시드 고정이라 치환 없이 그대로 비교한다.
_GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "judge-golden"
_WARM_RUNS = {
    "pass": ("PASS", dict(lat={"b": lambda x: x * 0.85})),
    "no-change": ("NO-CHANGE", dict()),
    "fail": ("FAIL", dict(lat={"b": lambda x: x * 1.10})),
    "inconclusive": ("INCONCLUSIVE", dict(n_q=10)),       # 쿼리 < MIN_QUERIES -> 속도 미결
}


def _judge_main(tmp_path, capsys, name, *cold_args):
    run = _write_run(tmp_path, ["a", "b"], **_WARM_RUNS[name][1])
    code = J.main([run, "--baseline", "a", "--candidate", "b", *cold_args])
    return code, capsys.readouterr().out


@pytest.mark.parametrize("name", list(_WARM_RUNS))
def test_without_cold_input_output_is_byte_identical_to_the_golden(tmp_path, capsys, name):
    """#169: 면제 인자가 없으면 stdout 문자열과 exit code 가 수정 전과 같다 - 키 추가·순서 변경도 잡으려고 문자열 전체를 비교한다."""
    code, out = _judge_main(tmp_path, capsys, name)
    assert out == (_GOLDEN / f"{name}.stdout").read_text()
    assert code == int((_GOLDEN / f"{name}.exit").read_text())
    assert "cold-unmeasured:no-cold-input" in out


@pytest.mark.parametrize("name", list(_WARM_RUNS))
def test_cold_exempt_makes_the_warm_verdict_final_and_records_the_reason(tmp_path, capsys, name):
    warm = _WARM_RUNS[name][0]
    code, out = _judge_main(tmp_path, capsys, name, "--cold-exempt", "request-shape-unchanged")
    rep = json.loads(out)
    assert rep["verdict"] == warm and rep["warm_verdict"] == warm and code == J.EXIT[warm]
    assert rep["cold"] == {"verdict": "exempt", "reason": "request-shape-unchanged"}
    assert rep["reasons"][-1] == "cold-exempt:request-shape-unchanged"
    assert not any(r.startswith("cold-unmeasured") for r in rep["reasons"])
    # 면제는 cold 항목만 바꾼다: 인자 없는 출력과 비교해 verdict 가 달라지는 곳은 최종 verdict·reasons·cold 뿐이다
    base = json.loads((_GOLDEN / f"{name}.stdout").read_text())
    assert {k: v for k, v in rep.items() if k not in ("verdict", "reasons", "cold")} == \
        {k: v for k, v in base.items() if k not in ("verdict", "reasons", "cold")}


@pytest.mark.parametrize("other", [["--cold-report", "r.json"], ["--cold-unmeasured", "x"]])
def test_cold_exempt_is_exclusive_with_the_other_cold_inputs(tmp_path, capsys, other):
    run = _write_run(tmp_path, ["a", "b"])
    with pytest.raises(SystemExit) as e:
        J.main([run, "--baseline", "a", "--candidate", "b", "--cold-exempt", "y", *other])
    assert e.value.code == 2
    cap = capsys.readouterr()
    assert cap.out == "" and "not allowed with argument" in cap.err      # 미지원 인자 오류가 아니라 상호배타 거부여야 한다


def test_empty_cold_exempt_reason_is_an_exemption_not_no_flag(tmp_path, capsys):
    """빈 사유도 면제 선언이다 - no-cold-input 보류로 새지 않는다(빈 --cold-unmeasured 테스트와 같은 원칙)."""
    run = _write_run(tmp_path, ["a", "b"], lat={"b": lambda x: x * 0.9})
    assert J.main([run, "--baseline", "a", "--candidate", "b", "--cold-exempt", ""]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["verdict"] == "PASS" and rep["cold"] == {"verdict": "exempt", "reason": ""}
    assert "cold-exempt:" in rep["reasons"] and "cold-unmeasured:no-cold-input" not in rep["reasons"]


def test_invalid_cold_run_makes_no_per_metric_claim_either():
    rep = J.combine_cold(_warm("PASS"), _cold(p95=(1.06, 1.03, 1.09), aborted="error:KeyboardInterrupt"))
    assert {m["verdict"] for m in rep["cold"]["metrics"].values()} == {"not-judged"}


def test_cold_metrics_without_clean_pairs_print_as_strict_json():
    """쌍 0 이면 부트스트랩 비가 NaN 이다 - 판정기 출력은 jq·JSON.parse 가 읽을 수 있어야 한다(NaN -> null)."""
    nan = float("nan")
    rep = J.combine_cold(_warm("PASS"), _cold(p50=(nan, nan, nan), p95=(nan, nan, nan), n=0))
    json.dumps(rep, allow_nan=False)
    assert rep["cold"]["metrics"]["cold_p95"]["ratio"] is None and rep["verdict"] == "INCONCLUSIVE"


@pytest.mark.parametrize("blocker", ["too-few-queries:29<30", "contaminated:0.25", "no-prod-interference-signal",
                                     "guard-aborted:swap-growth"])
def test_aa_control_that_cannot_be_judged_itself_invalidates_the_run(blocker):
    """final-review #21(운영자: 사후 예외 없음): A/A 대조가 판정기 자신의 조건을 못 채우면 그 초과분·꼬리 잡음도 못 믿는다.
    xho-b1w 의 대조는 완전 쿼리 29(<30)였다."""
    aa = {"baseline": "a", "candidate": "c", "speed": {"ci95": [0.995, 1.004]}, "accuracy": {"verdict": "invariant"},
          "cost": {"verdict": "unchanged"}, "reasons": [blocker]}
    assert J.aa_gate(aa) == [f"aa-blocked:{blocker}"]
    assert J.aa_gate(dict(aa, reasons=["tail-hold:p99(n=29)"])) == []     # 꼬리 보류는 대조를 무효로 하지 않는다


# ---------------------------------------------------------------- 교차 run 바퀴 단위 집계 (issue #82)
# 바퀴 = 모든 팔이 모든 세트를 정확히 한 번씩 거치는 연속 단계 n개(n = 팔 수). 단계 k 는 바퀴 k // n 에 속하고,
# 단계 k 에서 팔 i 는 세트 (i + k) mod n 을 쓴다(bench/crossover.plan). 기대값은 아래 오프셋표에서 손으로 계산했다.
#   2세트: 바퀴 0 은 세트 오프셋 (1.0, 1.3), 바퀴 1 은 (1.0, 1.6) - 바퀴마다 오프셋이 달라도 바퀴 안에서 상쇄된다.
#     바퀴 0: 단계 0 의 b/a = 1.3e/1.0, 단계 1 의 b/a = 1.0e/1.3 -> 로그 평균 = log e
#   3세트(팔 a, c, b): 바퀴 0 은 (1.0, 1.3, 0.8), 바퀴 1 은 (1.0, 0.7, 1.25).
#     바퀴 0: b/a = 0.8/1.0, 1.0/1.3, 1.3/0.8 (곱 1) -> 로그 평균 = log e
SET_OFF = {2: [(1.0, 1.3), (1.0, 1.6)], 3: [(1.0, 1.3, 0.8), (1.0, 0.7, 1.25)]}


def _lap_run(tmp, n_sets, laps, effect=1.0, spike=None, queue=None, plan=True, rounds=None, drop_arm=None):
    """교차 병합 run 모양(meta.crossover + meta.arms + meta.plan) 하나. 팔 b 가 후보(지연 x effect), a 가 기준,
    3세트면 c 는 a 의 쌍둥이다. spike = {단계: 배수} - 그 단계의 b 지연만 모든 쿼리에서 튄다."""
    arms = ["a", "b"] if n_sets == 2 else ["a", "c", "b"]
    rounds = laps * n_sets if rounds is None else rounds

    def lat(arm):
        i = arms.index(arm)
        eff = effect if arm == "b" else 1.0
        return lambda x, q, r: x * SET_OFF[n_sets][(r // n_sets) % 2][(i + r) % n_sets] * eff \
            * ((spike or {}).get(r, 1.0) if arm == "b" else 1.0)
    pl = {"version": 1, "arms": [{"name": a} for a in arms], "sets": [f"s{i}" for i in range(n_sets)],
          "laps": laps, "planned_phases": rounds, "recorded_at": "2026-10-02T00:00:00+00:00", "truncations": [],
          "actual_phases": rounds}
    meta = {"crossover": True, "arms": {a: 18891 + i for i, a in enumerate(arms)}, **({"plan": pl} if plan else {})}
    if drop_arm:                    # 한 팔의 호출이 통째로 빠지고 meta.arms 도 남지 않은 run(계획에는 그 팔이 있다)
        del meta["arms"]
    return _write_run(tmp, [a for a in arms if a != drop_arm], rounds=rounds, lat={a: lat(a) for a in arms}, meta=meta,
                      queue=queue)


@pytest.mark.parametrize("n_sets", [2, 3])
@pytest.mark.parametrize("effect,verdict", [(0.9, "PASS"), (1.0, "NO-CHANGE"), (1.1, "FAIL")])
def test_lap_mean_cancels_set_offsets_within_each_lap(tmp_path, n_sets, effect, verdict):
    rep = J.judge(_lap_run(tmp_path, n_sets, laps=2, effect=effect), "a", "b")
    assert rep["speed"]["geo_mean_ratio"] == pytest.approx(effect, abs=1e-4)
    assert rep["verdict"] == verdict
    assert rep["speed"]["laps"] == {"size": n_sets, "complete_lap_phases": 2 * n_sets, "queries_with_dropped_laps": 0}


def test_one_spiking_phase_in_three_laps_does_not_drag_the_query_value(tmp_path):
    """2세트 3바퀴, 단계 4(바퀴 2)에서 b 가 3배로 튄다. 바퀴 값 = 0.9, 0.9, 0.9*sqrt(3) -> 중앙값 0.9.
    전 단계 평균이었다면 0.9 * 3^(1/6) = 1.0808 로 끌려가 FAIL 이 된다."""
    rep = J.judge(_lap_run(tmp_path, 2, laps=3, effect=0.9, spike={4: 3.0}), "a", "b")
    assert rep["speed"]["geo_mean_ratio"] == pytest.approx(0.9, abs=1e-4)
    assert rep["speed"]["tail"]["queries_over"] == 0
    assert rep["verdict"] == "PASS"


def test_fewer_than_three_complete_laps_use_the_mean_of_lap_values(tmp_path):
    """2세트 2바퀴, 단계 2(바퀴 1)에서 b 가 3배: 바퀴 값 0.9, 0.9*sqrt(3) -> 평균(로그) = 0.9 * 3^(1/4) = 1.1844."""
    rep = J.judge(_lap_run(tmp_path, 2, laps=2, effect=0.9, spike={2: 3.0}), "a", "b")
    assert rep["speed"]["geo_mean_ratio"] == pytest.approx(0.9 * 3 ** 0.25, abs=1e-4)


def test_a_query_missing_some_phases_stays_with_its_complete_laps(tmp_path):
    """2세트 2바퀴(단계 0~3). q00~q14 는 단계 3 이 오염(바퀴 1 깨짐) -> 바퀴 0 만으로 남는다.
    q38·q39 는 단계 1 과 3 이 오염(두 바퀴 다 깨짐) -> 뺀다. 남는 쿼리 38, 빠진 쿼리 2, 바퀴를 잃은 쿼리 17."""
    lost = {"b": lambda q, r: 1 if (q < 15 and r == 3) or (q >= 38 and r in (1, 3)) else 0}
    rep = J.judge(_lap_run(tmp_path, 2, laps=2, effect=0.9, queue=lost), "a", "b")
    sp = rep["speed"]
    assert sp["n_queries"] == 38 and sp["incomplete_phase_queries"] == 2
    assert sp["laps"]["queries_with_dropped_laps"] == 17
    assert sp["geo_mean_ratio"] == pytest.approx(0.9, abs=1e-4)
    assert rep["verdict"] == "PASS"


@pytest.mark.parametrize("laps,rounds,blocked", [(1, 2, "too-few-rounds:2<3"), (2, 4, None), (2, 3, "too-few-rounds:2<3")],
                         ids=["two-arms-one-lap", "two-arms-two-laps", "two-arms-dangling-half-lap"])
def test_min_rounds_counts_phases_in_complete_laps(tmp_path, laps, rounds, blocked):
    """MIN_ROUNDS(3) 는 교차 run 에서 완전한 바퀴에 속한 단계 수로 센다. 2팔 1바퀴 = 2단계라 막히고 2바퀴(4단계)는
    통과한다. 바퀴를 다 못 채운 단계(3단계 중 단계 2)는 세지 않는다 - meta.rounds 3 이어도 완전한 바퀴의 단계는 2다."""
    rep = J.judge(_lap_run(tmp_path, 2, laps=laps, effect=0.9, rounds=rounds), "a", "b")
    rounds_blockers = [r for r in rep["reasons"] if r.startswith("too-few-rounds")]
    assert rounds_blockers == ([blocked] if blocked else [])
    assert rep["verdict"] == ("INCONCLUSIVE" if blocked else "PASS")


# 과거 교차 run(meta.plan 없음 = 계획 기록 이전 병합): 새 집계로 다시 판정하지 않는다 - 전 단계 평균, 단계가 빠진 쿼리는
# 통째로 빼고, MIN_ROUNDS 는 meta.rounds 로 본다. 위와 같은 데이터에서 옛 규칙의 손계산 값이 그대로 나와야 한다.
def test_past_crossover_run_keeps_the_all_phase_mean(tmp_path):
    rep = J.judge(_lap_run(tmp_path, 2, laps=3, effect=0.9, spike={4: 3.0}, plan=False), "a", "b")
    assert rep["speed"]["geo_mean_ratio"] == pytest.approx(0.9 * 3 ** (1 / 6), abs=1e-4)   # 1.0808
    assert rep["verdict"] == "FAIL" and "laps" not in rep["speed"]


def test_past_crossover_run_still_drops_queries_missing_a_phase(tmp_path):
    lost = {"b": lambda q, r: 1 if (q < 15 and r == 3) or (q >= 38 and r in (1, 3)) else 0}
    rep = J.judge(_lap_run(tmp_path, 2, laps=2, effect=0.9, queue=lost, plan=False), "a", "b")
    assert rep["speed"]["n_queries"] == 23 and rep["speed"]["incomplete_phase_queries"] == 17
    assert "too-few-queries:23<30" in rep["reasons"] and "laps" not in rep["speed"]


def test_past_crossover_run_counts_min_rounds_by_meta_rounds(tmp_path):
    rep = J.judge(_lap_run(tmp_path, 2, laps=2, effect=0.9, rounds=3, plan=False), "a", "b")
    assert not any(r.startswith("too-few-rounds") for r in rep["reasons"])


def test_tail_levels_come_from_the_same_lap_for_baseline_and_candidate(tmp_path):
    """2팔 3바퀴, 세트 오프셋 없음. 바퀴별 지연(ms): 기준 a = (2000, 1000, 1000), 후보 b = (2000, 1000, 3000).
    바퀴 비 = (1, 1, 3) -> 중앙값 1. 꼬리 수준도 비가 가운데인 바퀴(1000, 1000)에서 고르므로 p95·p99 비 = 1.
    팔마다 따로 중앙값을 내면 b 2000 / a 1000 = 2 가 되어 꼬리만 악화로 보인다."""
    lap_ms = {"a": (2000, 1000, 1000), "b": (2000, 1000, 3000)}
    pl = {"version": 1, "arms": [{"name": "a"}, {"name": "b"}], "sets": ["s0", "s1"], "laps": 3, "planned_phases": 6,
          "recorded_at": "2026-10-02T00:00:00+00:00", "truncations": [], "actual_phases": 6}
    run = _write_run(tmp_path, ["a", "b"], rounds=6, lat={arm: (lambda x, q, r, arm=arm: lap_ms[arm][r // 2]) for arm in "ab"},
                     meta={"crossover": True, "arms": {"a": 1, "b": 2}, "plan": pl})
    sp = J.judge(run, "a", "b")["speed"]
    assert sp["geo_mean_ratio"] == pytest.approx(1.0, abs=1e-4)
    assert sp["p95_ratio"] == pytest.approx(1.0, abs=1e-4)
    assert sp["tail_metrics"]["p95"]["ratio"] == pytest.approx(1.0, abs=1e-4)
    assert sp["tail_metrics"]["p99"]["ratio"] == pytest.approx(1.0, abs=1e-4)


def test_a_query_that_lost_every_phase_is_counted_as_having_dropped_laps(tmp_path):
    """2세트 2바퀴, q39 는 4단계 모두 오염 -> 쓸 단계가 없어도 바퀴를 잃은 쿼리 1개로 센다. 남는 쿼리 39."""
    lost = {"b": lambda q, r: 1 if q == 39 else 0}
    sp = J.judge(_lap_run(tmp_path, 2, laps=2, effect=0.9, queue=lost), "a", "b")["speed"]
    assert sp["n_queries"] == 39 and sp["laps"]["queries_with_dropped_laps"] == 1


def test_a_query_with_every_phase_contaminated_counts_as_incomplete_phase_query(tmp_path):
    """issue #106: q39 는 4단계 모두 오염 - pairs 에 흔적이 없어도 완전한 바퀴가 없는 쿼리다. incomplete_phase_queries 가
    1 이고 n_queries(39) + 그 수 = 전체 쿼리 40 이다. 같은 쿼리는 queries_with_dropped_laps 에도 잡힌다."""
    lost = {"b": lambda q, r: 1 if q == 39 else 0}
    sp = J.judge(_lap_run(tmp_path, 2, laps=2, effect=0.9, queue=lost), "a", "b")["speed"]
    assert sp["incomplete_phase_queries"] == 1 and sp["n_queries"] == 39
    assert sp["n_queries"] + sp["incomplete_phase_queries"] == 40
    assert sp["laps"]["queries_with_dropped_laps"] == 1


def test_incomplete_phase_queries_adds_fully_contaminated_to_partially_contaminated(tmp_path):
    """q00~q14 는 단계 3 만 오염(바퀴 0 이 남는다 -> 불완전 아님), q38 은 단계 1·3 오염(남는 바퀴 없음),
    q39 는 전 단계 오염. 불완전 = q38 + q39 = 2, 바퀴를 잃은 쿼리 = 15 + 2 = 17."""
    lost = {"b": lambda q, r: 1 if (q < 15 and r == 3) or (q == 38 and r in (1, 3)) or q == 39 else 0}
    sp = J.judge(_lap_run(tmp_path, 2, laps=2, effect=0.9, queue=lost), "a", "b")["speed"]
    assert sp["incomplete_phase_queries"] == 2 and sp["n_queries"] == 38
    assert sp["laps"]["queries_with_dropped_laps"] == 17


def test_past_crossover_run_keeps_incomplete_phase_queries_as_queries_missing_some_phase(tmp_path):
    """meta.plan 없는 과거 교차 run 은 옛 정의 그대로다: 단계가 하나라도 빠졌지만 하나는 남은 쿼리만 센다(issue #106 전후 불변)."""
    lost = {"b": lambda q, r: 1 if q == 39 else 0}
    sp = J.judge(_lap_run(tmp_path, 2, laps=2, effect=0.9, queue=lost, plan=False), "a", "b")["speed"]
    assert sp["incomplete_phase_queries"] == 0 and sp["n_queries"] == 39 and "laps" not in sp


def test_a_trailing_half_lap_is_not_counted_as_a_dropped_lap(tmp_path):
    """2팔 3단계(계획 축소·중단으로 바퀴 1 이 반만 남음): run 에 온전한 바퀴는 1개뿐이고 모든 쿼리가 그 바퀴를 지녔다 -> 0."""
    sp = J.judge(_lap_run(tmp_path, 2, laps=2, effect=0.9, rounds=3), "a", "b")["speed"]
    assert sp["laps"] == {"size": 2, "complete_lap_phases": 2, "queries_with_dropped_laps": 0}


def test_lap_size_comes_from_the_plan_even_when_an_arm_is_missing(tmp_path):
    """3세트 2바퀴 계획에서 쌍둥이 팔 c 의 호출이 통째로 없고 meta.arms 도 없다. 바퀴는 여전히 3단계라 오프셋이 상쇄돼
    비 = 0.9 그대로다. 측정 호출에서 본 팔 수(2)로 바퀴를 자르면 단계 [0,1],[2,3],[4,5] 로 묶여 3세트 오프셋이 남는다."""
    rep = J.judge(_lap_run(tmp_path, 3, laps=2, effect=0.9, drop_arm="c"), "a", "b")
    assert rep["speed"]["laps"]["size"] == 3
    assert rep["speed"]["geo_mean_ratio"] == pytest.approx(0.9, abs=1e-4)
    assert rep["verdict"] == "PASS"
