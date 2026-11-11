"""issue #297 bench/arm_compare.py - 한 run 안의 두 팔(base = D10 세트, cand = arm 세트)을 속도·비용으로 비교한다.

합성 run 디렉터리(meta.json + calls.jsonl)만 쓴다. docker·복제본에 닿지 않는다.
"""
import json
import math

import pytest

from bench import arm_compare as C

SHA_D10 = "8a4900ea" + "0" * 56
SHA_ARM = "a" * 64


def make_run(tmp_path, *, n=40, rounds=3, lat=lambda arm, k, r: 100.0 if arm == "base" else 110.0,
             tokens=lambda arm, k: 50, digest=lambda arm, k, r: f"d{k}", outcome=lambda arm, k, r: "ok",
             meta_extra=None, write_meta=True, drop=0, name="run1"):
    d = tmp_path / name
    d.mkdir()
    rows = []
    for r in range(rounds):
        for k in range(n):
            for arm in ("base", "cand"):
                rows.append({"arm": arm, "key": f"q{k:02d}", "phase": "measure", "round": r,
                             "latency_ms": lat(arm, k, r), "outcome": outcome(arm, k, r),
                             "tokens": tokens(arm, k), "results_digest": digest(arm, k, r),
                             "wall": "2026-10-30T00:00:00+00:00", "wall_end": "2026-10-30T00:00:00.1+00:00"})
    rows = rows[:len(rows) - drop]
    rows.insert(0, {"arm": "base", "key": "warmup-x", "phase": "warmup", "round": -1, "latency_ms": 1.0,
                    "outcome": "transport-error"})                     # 워밍업 오류는 측정이 아니다
    (d / "calls.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
    (d / "prod_events.jsonl").write_text("")
    meta = {"run_id": name, "arms": {"base": 18891, "cand": 18892}, "split": "dev", "rounds": rounds, "n_queries": n,
            "bench_clock": "2026-10-27T04:31:00Z", "guard": {"windows": [], "aborted": None},
            "freeze_before": {"s1": 1, "s2": 2}, "freeze_after": {"s1": 1, "s2": 2},
            "rewrite_failures_before": {"base": 0, "cand": 0}, "rewrite_failures_after": {"base": 0, "cand": 0},
            "provenance": {"arms": {"base": {"set": "s1"}, "cand": {"set": "s2"}},
                           "replicas": {"s1": {"backup_sha256": SHA_D10}, "s2": {"backup_sha256": SHA_ARM}}}}
    meta.update(meta_extra or {})
    if write_meta:
        (d / "meta.json").write_text(json.dumps(meta))
    return str(d)


def verify_files(tmp_path, base_pass=True, cand_pass=True):
    out = {}
    for arm, ok in (("base", base_pass), ("cand", cand_pass)):
        p = tmp_path / f"verify-{arm}.json"
        p.write_text(json.dumps({"pass": ok}))
        out[arm] = str(p)
    return out


def judge(run, tmp_path, **kw):
    v = kw.pop("verify", None) or verify_files(tmp_path)
    return C.compare(run, "base", "cand", expect_sha={"base": SHA_D10, "cand": SHA_ARM}, verify=v,
                     speed_threshold=kw.pop("speed_threshold", 0.02), p95_threshold=kw.pop("p95_threshold", 0.04), **kw)


def test_ratios_tokens_and_changed_share_from_a_synthetic_run(tmp_path):
    run = make_run(tmp_path, tokens=lambda arm, k: 50 + (5 if arm == "cand" and k < 10 else 0),
                   digest=lambda arm, k, r: f"d{k}" + ("x" if arm == "cand" and k % 4 == 0 else ""))
    rep = judge(run, tmp_path, speed_threshold=0.2, p95_threshold=0.2)
    assert rep["valid"] is True
    assert rep["speed"]["geo_mean_ratio"] == pytest.approx(1.1, abs=1e-6)
    assert rep["speed"]["ci95"][0] == pytest.approx(1.1, abs=1e-6) and rep["speed"]["n_queries"] == 40
    assert rep["p95"]["ratio"] == pytest.approx(1.1, abs=1e-6)
    assert rep["tokens"]["base_total"] == 2000 and rep["tokens"]["cand_total"] == 2050
    assert rep["tokens"]["queries_increased"] == 10 and rep["tokens"]["queries_decreased"] == 0
    assert rep["tokens"]["per_query_delta"]["max"] == 5 and rep["tokens"]["per_query_delta"]["min"] == 0
    assert rep["changed"]["queries"] == 10 and rep["changed"]["share"] == pytest.approx(0.25)
    assert rep["verdict"]["cost"] == "regressed" and rep["verdict"]["overall"] == "FAIL"


def test_per_query_ratio_is_the_round_median_and_the_mean_is_geometric(tmp_path):
    # 쿼리 절반은 1.21배, 절반은 1/1.21 배 -> 기하평균 1. 한 라운드의 이상치(5배)는 중앙값이 누른다
    def lat(arm, k, r):
        if arm == "base":
            return 100.0
        if r == 0 and k == 3:
            return 500.0
        return 121.0 if k % 2 else 100.0 / 1.21
    rep = judge(make_run(tmp_path, lat=lat), tmp_path)
    assert rep["speed"]["geo_mean_ratio"] == pytest.approx(1.0, abs=1e-6)
    assert rep["verdict"]["speed"] == "ok" and rep["verdict"]["cost"] == "ok"


@pytest.mark.parametrize("ratio, thr, want", [(1.01, 0.02, "ok"), (1.03, 0.02, "regressed"), (0.95, 0.02, "ok")])
def test_speed_threshold_is_a_point_estimate_bound(tmp_path, ratio, thr, want):
    rep = judge(make_run(tmp_path, lat=lambda a, k, r: 100.0 if a == "base" else 100.0 * ratio), tmp_path,
                speed_threshold=thr, p95_threshold=1.0)
    assert rep["verdict"]["speed"] == want


def test_aa_identical_arms_pass_with_zero_deltas(tmp_path):
    rep = judge(make_run(tmp_path, lat=lambda a, k, r: 100.0 + k), tmp_path)
    assert rep["speed"]["geo_mean_ratio"] == pytest.approx(1.0) and rep["p95"]["ratio"] == pytest.approx(1.0)
    assert rep["tokens"]["cand_total"] == rep["tokens"]["base_total"] and rep["changed"]["queries"] == 0
    assert rep["verdict"]["overall"] == "PASS"


@pytest.mark.parametrize("mk, reason", [
    (dict(write_meta=False), "aborted:no-meta"),                                         # 예외·Ctrl-C 로 끝난 run
    (dict(meta_extra={"guard": {"windows": [], "aborted": "swap-growth"}}), "aborted:guard:swap-growth"),
    (dict(drop=3), "incomplete"),                                                        # 측정 호출이 모자란다
    (dict(outcome=lambda a, k, r: "upstream-error" if (a, k, r) == ("cand", 7, 1) else "ok"), "error-responses"),
    (dict(outcome=lambda a, k, r: "ok-unstructured" if (a, k, r) == ("base", 2, 0) else "ok"), "outcome-diff"),
    (dict(meta_extra={"freeze_after": {"s1": 1, "s2": 3}}), "replica-drift"),
    (dict(meta_extra={"rewrite_failures_after": {"base": 0, "cand": 1}}), "rewrite-failures:cand"),
])
def test_invalid_runs_are_not_judged(tmp_path, mk, reason):
    rep = judge(make_run(tmp_path, **mk), tmp_path)
    assert rep["valid"] is False and rep["verdict"]["overall"] == "INVALID"
    assert any(x.startswith(reason) for x in rep["invalid_reasons"]), rep["invalid_reasons"]


def test_backup_sha_mismatch_is_not_judged(tmp_path):
    run = make_run(tmp_path)
    rep = C.compare(run, "base", "cand", expect_sha={"base": SHA_D10, "cand": "b" * 64}, verify=verify_files(tmp_path),
                    speed_threshold=0.02, p95_threshold=0.04)
    assert rep["valid"] is False and "backup-sha256:cand" in rep["invalid_reasons"]


@pytest.mark.parametrize("verify, reason", [
    ("fail-cand", "verify-not-pass:cand"),
    ("missing-base", "verify-missing:base"),
])
def test_verify_not_passing_is_not_judged(tmp_path, verify, reason):
    v = verify_files(tmp_path, cand_pass=verify != "fail-cand")
    if verify == "missing-base":
        v.pop("base")
    rep = judge(make_run(tmp_path), tmp_path, verify=v)
    assert rep["valid"] is False and reason in rep["invalid_reasons"]


def test_unknown_arm_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        C.compare(make_run(tmp_path), "base", "nope", expect_sha={}, verify={}, speed_threshold=0.02,
                  p95_threshold=0.04)


def test_cli_exit_codes_and_json(tmp_path, capsys):
    run = make_run(tmp_path, lat=lambda a, k, r: 100.0)
    v = verify_files(tmp_path)
    args = [run, "--base", "base", "--cand", "cand", "--expect-sha", f"base={SHA_D10}", "--expect-sha", f"cand={SHA_ARM}",
            "--verify", f"base={v['base']}", "--verify", f"cand={v['cand']}", "--speed-threshold", "0.01",
            "--p95-threshold", "0.04"]
    assert C.main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"]["overall"] == "PASS" and not math.isnan(out["speed"]["geo_mean_ratio"])
    run2 = make_run(tmp_path, write_meta=False, name="run2")
    assert C.main([run2] + args[1:]) == 2                         # 무효 run


# ---------------------------------------------------------------- rework #297-2 (final-review #297)

def test_warmup_abort_without_meta_or_measure_rows_is_invalid_exit_2(tmp_path, capsys):
    d = tmp_path / "aborted"
    d.mkdir()
    (d / "calls.jsonl").write_text(json.dumps({"arm": "base", "key": "w", "phase": "warmup", "round": -1,
                                               "latency_ms": 1.0, "outcome": "ok"}) + "\n")
    v = verify_files(tmp_path)
    rc = C.main([str(d), "--expect-sha", f"base={SHA_D10}", "--expect-sha", f"cand={SHA_ARM}", "--verify",
                 f"base={v['base']}", "--verify", f"cand={v['cand']}", "--speed-threshold", "0.03", "--p95-threshold", "0.06"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["verdict"]["overall"] == "INVALID" and "aborted:no-meta" in out["invalid_reasons"]


def test_pairs_dropped_because_both_arms_are_ok_unstructured_are_reported(tmp_path):
    run = make_run(tmp_path, outcome=lambda a, k, r: "ok-unstructured" if k < 3 else "ok")
    rep = judge(run, tmp_path)
    assert rep["speed"]["unstructured_pairs"] == 9 and rep["speed"]["pairs"] == 111
    assert rep["valid"] is True                                     # 판정은 그대로(보고만)


@pytest.mark.parametrize("mk, kw, reason", [
    (dict(rounds=1), {}, "too-few-rounds:1<3"),
    (dict(n=10), {}, "too-few-queries:10<40"),
    (dict(n=10), {"min_queries": 10}, None),
])
def test_sample_floor_makes_small_runs_invalid(tmp_path, mk, kw, reason):
    rep = judge(make_run(tmp_path, **mk), tmp_path, **kw)
    if reason is None:
        assert rep["valid"] is True
    else:
        assert rep["valid"] is False and reason in rep["invalid_reasons"]
