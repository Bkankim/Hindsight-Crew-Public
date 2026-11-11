"""issue #13 B 순환 교차 - 설정(팔)을 인스턴스(세트)에 단계마다 돌려 배정해 인스턴스 오프셋을 상쇄한다."""
import json
import os
import socket

import pytest

from bench import canary
from bench import crossover as X
from bench import judge as J

ARMS = ["base", "ctrl", "cand"]
SETS = ["x", "y", "z"]


def test_each_phase_is_a_bijection_and_every_arm_visits_every_set_once():
    phases = [X.plan(ARMS, SETS, k) for k in range(3)]
    for p in phases:
        assert sorted(p) == sorted(ARMS) and sorted(p.values()) == sorted(SETS)
    for arm in ARMS:
        assert sorted(p[arm] for p in phases) == sorted(SETS)


def test_plan_needs_as_many_sets_as_arms():
    with pytest.raises(SystemExit):
        X.plan(ARMS, ["x", "y"], 0)


CFG = {"base": {}, "ctrl": {}, "cand": {"HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE": "300"}}


def _phase(tmp, k, lat=lambda arm, q: 5000 + 37 * q, cfg=CFG, clock="C", sha="S", freeze=None,
           gw=None, sidecar=None, rf=None, image=None, pb=None, pb_set=None, kw=None, skw=None):
    d = tmp / f"p{k}"
    (d / "results").mkdir(parents=True)
    assign = X.plan(ARMS, SETS, k)
    prov = {"arms": {a: {"set": s, "commit": "c1", **(gw or {}).get(a, {})} for a, s in assign.items()},
            "replicas": {s: {"hs_env": cfg[a], "sidecar": (sidecar or {}).get(s, {"RERANK_MAX_TEXTS": "300"}),
                             "image": (image or {}).get(s, "sha256:i1")}
                         for a, s in assign.items()}}
    for a, s in assign.items():          # 세트가 그 단계에 가리킨 벤치 사이드카 포트·배치(replica state 가 provenance 로 실린다, issue #83)
        spec = (pb_set or {}).get(s, (pb or {}).get(a))
        if spec is not None:
            prov["replicas"][s].update({"sidecar_port": spec[0], "sidecar_batch_size": spec[1]})
    fz = freeze if freeze is not None else {"f": 1}
    rf_b, rf_a = rf or ({a: 0 for a in ARMS}, {a: 0 for a in ARMS})
    json.dump({"run_id": f"p{k}", "split": "dev", "bank": "b", "rounds": 1, "bench_clock": clock,
               "corpus_keys_sha256": sha, "arms": {a: 18891 + i for i, a in enumerate(ARMS)},
               "carryover_balanced": True, "provenance": prov, "freeze_before": fz, "freeze_after": fz,
               "rewrite_failures_before": rf_b, "rewrite_failures_after": rf_a,
               "prod_events": 0, **({} if kw is None else {"keepwarm": kw}),
               **({} if skw is None else {"prod_sidecar_keepwarm": skw})}, open(d / "meta.json", "w"))
    open(d / "prod_events.jsonl", "w").close()
    with open(d / "calls.jsonl", "w") as fh:
        for q in range(40):
            for arm in ARMS:
                digest = f"{arm}{q}"
                json.dump([{"id": "r1", "text": "t"}], open(d / "results" / f"{arm}.q{q:02d}.{digest}.json", "w"))
                fh.write(json.dumps({"phase": "measure", "arm": arm, "key": f"q{q:02d}", "round": 0,
                                     "outcome": "ok", "latency_ms": lat(arm, q), "tokens": 100,
                                     "results_digest": digest, "tei_queue_before": 0}) + "\n")
    return str(d)


def _merge(phases, out_dir, arms, sets, cfg, laps=None):
    """계획을 먼저 기록한 뒤 병합한다(issue #77: 계획 파일 없는 병합은 거부). 기본 계획 = 단계를 모두 거친 바퀴 수."""
    X.record_plan(out_dir, arms, sets, laps or max(1, len(phases) // len(arms)))
    return X.merge(phases, out_dir, arms, sets, cfg)


def test_merged_phases_form_one_judgeable_crossover_run(tmp_path):
    phases = [_phase(tmp_path, k) for k in range(3)]
    meta = _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)
    assert meta["crossover"] is True and meta["rounds"] == 3
    rounds = {json.loads(line)["round"] for line in open(tmp_path / "m" / "calls.jsonl")}
    assert rounds == {0, 1, 2}
    assert J.judge(str(tmp_path / "m"), "base", "ctrl")["verdict"] == "NO-CHANGE"


def test_merge_refuses_a_phase_that_ran_the_wrong_config(tmp_path):
    bad = {**CFG, "cand": {}}                                        # cand 세트가 cand 설정이 아니었다
    phases = [_phase(tmp_path, 0), _phase(tmp_path, 1, cfg=bad), _phase(tmp_path, 2)]
    with pytest.raises(SystemExit):
        _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)


@pytest.mark.parametrize("kw", [{"clock": "D"}, {"sha": "T"}])
def test_merge_refuses_phases_that_measured_different_things(tmp_path, kw):
    phases = [_phase(tmp_path, 0), _phase(tmp_path, 1, **kw), _phase(tmp_path, 2)]
    with pytest.raises(SystemExit):
        _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)


def test_freeze_drift_between_phases_invalidates_the_merged_run(tmp_path):
    phases = [_phase(tmp_path, 0), _phase(tmp_path, 1, freeze={"f": 2}), _phase(tmp_path, 2)]
    _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)
    assert "replica-drift" in J.judge(str(tmp_path / "m"), "base", "cand")["reasons"]


# ---------------------------------------------------------------- 하네스 결함 수정 (issue #13 티켓 3)

def test_merge_refuses_sets_whose_gateways_differ(tmp_path):
    """팔 차이는 upstream 설정뿐이어야 한다 - 한 세트 게이트웨이가 다른 커밋이면 병합하지 않는다."""
    phases = [_phase(tmp_path, 0), _phase(tmp_path, 1, gw={"cand": {"commit": "c2"}}), _phase(tmp_path, 2)]
    with pytest.raises(SystemExit):
        _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)


# issue #225 - 팔 게이트웨이 동일성에 local_policies.files(파일명 -> sha256)가 들어간다. 해시만 쓴다(정책 내용은 어디에도 없다).
H1, H2 = "a1" * 32, "b2" * 32


def _lp(files, d="/wt-a/policies/local", source="flag"):
    return {"source": source, "dir": d, "result": "복사" if files else "없음", "files": files}


def _policy_phases(tmp_path, per_arm):
    """per_arm: 팔 -> local_policies(없으면 그 팔 prov 에 키 자체가 없다). 모든 단계에 같은 값을 싣는다."""
    gw = {a: {"local_policies": v} for a, v in per_arm.items() if v is not None}
    return [_phase(tmp_path, k, gw=gw) for k in range(3)]


def _same(files, **kw):
    return {a: _lp(files, **kw) for a in ARMS}


def _raw(**fields):
    """files 를 일부러 빼거나 망가뜨린 local_policies(경로 필드는 정상)."""
    return {"source": "flag", "dir": "/wt-a/policies/local", "result": "복사", **fields}


# 분기마다 다른 메시지로 거부한다 - match 가 분기를 가른다(혼합 가드를 지우면 mixed 행이 실패한다)
MIXED = r"local_policies recorded for arms .* but not for"
NOT_A_MAP = r"local_policies\.files is missing or not a map for arms"
DIFFER = r"arms ran different local_policies\.files"

REFUSED = [
    pytest.param({"base": _lp({"x.md": H1}), "ctrl": None, "cand": None}, MIXED, id="only-one-arm-has-it"),
    pytest.param({"base": None, "ctrl": _lp({"x.md": H1}), "cand": _lp({"x.md": H1})}, MIXED, id="only-one-arm-lacks-it"),
    pytest.param({**_same({"x.md": H1}), "cand": _lp({"x.md": H2})}, DIFFER, id="all-have-it-one-hash-differs"),
    pytest.param({**_same({"x.md": H1}), "cand": _lp({"x.md": H1, "y.md": H2})}, DIFFER, id="all-have-it-one-file-extra"),
    pytest.param({**_same({"x.md": H1}), "cand": _lp({"y.md": H1})}, DIFFER, id="all-have-it-same-hash-other-file-name"),
    pytest.param({**_same({}), "cand": _lp({"x.md": H1})}, DIFFER, id="all-have-it-empty-map-vs-files"),
    # issue #225 final-review #1: 맵이 아닌 files 는 "같음"으로 통과시키지 않는다(전 팔이 같은 모양이어도)
    pytest.param({**_same({"x.md": H1}), "cand": _raw()}, NOT_A_MAP, id="one-arm-has-no-files-key"),
    pytest.param({**_same({"x.md": H1}), "cand": _raw(files=None)}, NOT_A_MAP, id="one-arm-files-null"),
    pytest.param({**_same({"x.md": H1}), "cand": _raw(files=[H1])}, NOT_A_MAP, id="one-arm-files-list"),
    pytest.param({a: _raw() for a in ARMS}, NOT_A_MAP, id="every-arm-has-no-files-key"),
    pytest.param({a: _raw(files=None) for a in ARMS}, NOT_A_MAP, id="every-arm-files-null"),
    pytest.param({a: {} for a in ARMS}, NOT_A_MAP, id="every-arm-local-policies-empty-dict"),
    pytest.param({**_same({"x.md": H1}), "cand": "corrupt"}, NOT_A_MAP, id="one-arm-local-policies-not-a-dict"),
]

ACCEPTED = [
    pytest.param({a: None for a in ARMS}, id="no-arm-has-it-pre-217-prov"),
    pytest.param(_same({"x.md": H1, "y.md": H2}), id="all-have-it-same-hashes"),
    pytest.param(_same({}), id="all-have-it-empty-maps-equal"),
    pytest.param({"base": _lp({"x.md": H1}, d="/wt-a/pol", source="flag"),
                  "ctrl": _lp({"x.md": H1}, d="/wt-b/pol", source="env"),
                  "cand": _lp({"x.md": H1}, d="/wt-c/pol", source="repo")}, id="path-fields-are-not-compared"),
]


@pytest.mark.parametrize("per_arm", ACCEPTED)
def test_merge_accepts_arms_with_equivalent_local_policies(tmp_path, per_arm):
    _merge(_policy_phases(tmp_path, per_arm), str(tmp_path / "m"), ARMS, SETS, CFG)


@pytest.mark.parametrize("per_arm,branch", REFUSED)
def test_merge_refuses_arms_whose_local_policies_differ_or_cannot_be_confirmed(tmp_path, per_arm, branch):
    with pytest.raises(SystemExit, match=branch):
        _merge(_policy_phases(tmp_path, per_arm), str(tmp_path / "m"), ARMS, SETS, CFG)


@pytest.mark.parametrize("per_arm,branch", REFUSED)
def test_policy_refusal_names_the_phase_and_never_prints_a_hash_or_path(tmp_path, per_arm, branch):
    with pytest.raises(SystemExit) as e:
        _merge(_policy_phases(tmp_path, per_arm), str(tmp_path / "m"), ARMS, SETS, CFG)
    msg = str(e.value)
    assert "phase 0" in msg and "local_policies" in msg
    assert H1 not in msg and H2 not in msg and "/wt-" not in msg and "policies/local" not in msg


def test_merge_refuses_sets_on_different_bench_sidecar_overrides(tmp_path):
    phases = [_phase(tmp_path, 0), _phase(tmp_path, 1, sidecar={"y": {"RERANK_BATCH_SIZE": "16"}}), _phase(tmp_path, 2)]
    with pytest.raises(SystemExit):
        _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)


def test_rewrite_failure_in_one_phase_is_not_cancelled_by_a_counter_reset_in_another(tmp_path):
    """단계 도중 게이트웨이 재시작으로 카운터가 줄면(-2) 다른 단계의 실제 실패(+2)가 합 0 으로 숨는다 - 알 수 없음으로 둔다."""
    zero = {a: 0 for a in ARMS}
    phases = [_phase(tmp_path, 0, rf=({**zero, "cand": 5}, {**zero, "cand": 3})),
              _phase(tmp_path, 1, rf=(zero, {**zero, "cand": 2})), _phase(tmp_path, 2)]
    _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)
    assert "rewrite-failures:cand" in J.judge(str(tmp_path / "m"), "base", "cand")["reasons"]


def test_aa_control_with_a_different_config_is_not_a_twin(tmp_path):
    """결과를 바꾸지 않는 설정(B1 처럼)이 ctrl 에 붙으면 결과·비용 쌍둥이 검사를 통과한다 - 설정으로 잡는다."""
    cfg = {**CFG, "ctrl": {"HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE": "300"}}
    phases = [_phase(tmp_path, k, cfg=cfg) for k in range(3)]
    _merge(phases, str(tmp_path / "m"), ARMS, SETS, cfg)
    assert "aa-not-twin" in J.aa_gate(J.judge(str(tmp_path / "m"), "base", "ctrl"))


def test_aa_control_with_the_same_config_passes_the_config_check(tmp_path):
    phases = [_phase(tmp_path, k) for k in range(3)]
    _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)
    assert "aa-not-twin" not in J.aa_gate(J.judge(str(tmp_path / "m"), "base", "ctrl"))


def test_merge_refuses_sets_on_different_upstream_images(tmp_path):
    phases = [_phase(tmp_path, 0), _phase(tmp_path, 1, image={"z": "sha256:i2"}), _phase(tmp_path, 2)]
    with pytest.raises(SystemExit):
        _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)


def test_merge_carries_every_phases_guard_windows_and_sidecar_errors(tmp_path):
    dirs = [_phase(tmp_path, k) for k in range(3)]
    for k, d in enumerate(dirs):
        m = json.load(open(f"{d}/meta.json"))
        m["guard"] = {"windows": [[k, k + 0.5]], "pauses": [], "aborted": "swap-growth" if k == 2 else None}
        m["prod_sidecar"] = None if k == 1 else {"http_503": k, "deadline": 1, "queue_full": 0}
        json.dump(m, open(f"{d}/meta.json", "w"))
    meta = _merge(dirs, str(tmp_path / "m"), ARMS, SETS, CFG)
    assert meta["guard"]["windows"] == [[0, 0.5], [1, 1.5], [2, 2.5]]
    assert meta["guard"]["aborted"] == "swap-growth"
    assert meta["prod_sidecar"] is None                      # 한 단계라도 못 셌으면 합계를 말하지 않는다


def test_merge_sums_sidecar_errors_when_every_phase_counted(tmp_path):
    dirs = [_phase(tmp_path, k) for k in range(3)]
    for k, d in enumerate(dirs):
        m = json.load(open(f"{d}/meta.json"))
        m["prod_sidecar"] = {"http_503": k, "deadline": 1, "queue_full": 0}
        json.dump(m, open(f"{d}/meta.json", "w"))
    meta = _merge(dirs, str(tmp_path / "m"), ARMS, SETS, CFG)
    assert meta["prod_sidecar"] == {"http_503": 3, "deadline": 3, "queue_full": 0}
    assert meta["guard"] == {"limits": None, "windows": [], "pauses": [], "aborted": None}


def test_two_full_laps_merge_into_one_run_where_every_arm_visits_every_set_twice(tmp_path):
    # issue #21: warm 라운드를 늘리려면 순환을 여러 바퀴 돈다 - 바퀴마다 오프셋이 상쇄되니 평균도 상쇄된다
    phases = [_phase(tmp_path, k) for k in range(6)]
    meta = _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)
    assert meta["rounds"] == 6
    for arm in ARMS:
        assert sorted(p["plan"][arm] for p in meta["phases"]) == sorted(SETS * 2)
    assert J.judge(str(tmp_path / "m"), "base", "ctrl")["verdict"] == "NO-CHANGE"


@pytest.mark.parametrize("n", [2, 4, 5])
def test_merge_refuses_a_partial_lap(tmp_path, n):
    # 바퀴가 덜 끝나면 어떤 팔은 어떤 세트를 한 번 더 거쳐 그 세트의 오프셋이 평균에 남는다
    phases = [_phase(tmp_path, k) for k in range(n)]
    with pytest.raises(SystemExit):
        _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)


def test_merge_refuses_the_same_phase_twice(tmp_path):
    # final-review #21: plan(k) == plan(k+n) 라 같은 단계를 두 바퀴 자리에 넣으면 배정 검사를 통과해 호출·간섭이 두 번 셈해진다
    phases = [_phase(tmp_path, k) for k in range(3)]
    with pytest.raises(SystemExit, match="phase dirs repeat"):
        _merge(phases + phases, str(tmp_path / "m"), ARMS, SETS, CFG)


def test_merge_refuses_a_copied_phase_under_another_directory(tmp_path):
    phases = [_phase(tmp_path, k) for k in range(6)]
    meta = json.load(open(os.path.join(phases[3], "meta.json")))
    json.dump(dict(meta, run_id="p0"), open(os.path.join(phases[3], "meta.json"), "w"))
    with pytest.raises(SystemExit, match="run_ids repeat"):
        _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)


# ---------------------------------------------------------------- 계획 기록 (issue #77)

def test_merge_refuses_a_run_without_a_recorded_plan(tmp_path):
    """결과를 보고 멈추는 선택적 중단을 잡으려면 계획이 첫 단계 전에 있어야 한다 - 계획 파일 없는 병합은 거부하고 아무것도 쓰지 않는다."""
    phases = [_phase(tmp_path, k) for k in range(3)]
    with pytest.raises(SystemExit, match="plan"):
        X.merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)
    assert not (tmp_path / "m" / "results").exists() and not (tmp_path / "m" / "meta.json").exists()


T0 = "2026-10-02T00:00:00+00:00"                                # 테스트가 주입하는 계획 기록 시각(시계 대신)


def _plan_dir(tmp_path, laps=3, now=T0):
    out = str(tmp_path / "m")
    X.record_plan(out, ARMS, SETS, laps, now=now)
    return out


def _plan_file(out):
    return json.load(open(os.path.join(out, "crossover-plan.json")))


def test_record_plan_writes_arms_sets_laps_planned_phases_and_time_before_any_phase(tmp_path):
    out = _plan_dir(tmp_path, laps=3)
    doc = _plan_file(out)
    assert [a["name"] for a in doc["arms"]] == ARMS and doc["sets"] == SETS      # 팔은 객체 - 팔별 항목을 덧붙일 수 있다
    assert doc["laps"] == 3 and doc["planned_phases"] == 9                        # 바퀴 = 팔 수만큼의 연속 단계
    assert doc["recorded_at"] == T0 and doc["truncations"] == []


@pytest.mark.parametrize("arms,sets,laps", [(ARMS, SETS, 0), (ARMS, SETS, -2), (ARMS, ["x", "y"], 2),
                                            (ARMS, ["x", "x", "y"], 2)])
def test_record_plan_refuses_an_invalid_plan_and_writes_nothing(tmp_path, arms, sets, laps):
    with pytest.raises(SystemExit):
        X.record_plan(str(tmp_path / "m"), arms, sets, laps)
    assert not (tmp_path / "m" / "crossover-plan.json").exists()


def test_record_plan_cannot_overwrite_a_recorded_plan(tmp_path):
    out = _plan_dir(tmp_path, laps=3)
    with pytest.raises(SystemExit, match="already recorded"):
        X.record_plan(out, ARMS, SETS, 1)
    assert _plan_file(out)["laps"] == 3                                           # 계획을 다시 써서 축소를 지울 수 없다


def test_truncate_appends_reason_and_time_and_keeps_the_original_plan(tmp_path):
    out = _plan_dir(tmp_path, laps=3)
    doc = X.truncate_plan(out, 2, "  ops-protection: 503 spike  ", now="2026-10-02T01:00:00+00:00")
    assert doc == _plan_file(out)
    assert doc["laps"] == 3 and doc["planned_phases"] == 9                        # 처음 계획은 그대로
    assert doc["truncations"] == [{"at": "2026-10-02T01:00:00+00:00", "reason": "ops-protection: 503 spike",
                                   "laps": 2, "planned_phases": 6}]
    X.truncate_plan(out, 1, "again", now="2026-10-02T02:00:00+00:00")             # 덧붙임 - 앞 기록은 남는다
    assert [t["laps"] for t in _plan_file(out)["truncations"]] == [2, 1]


@pytest.mark.parametrize("laps,reason", [(3, "x"), (4, "x"), (0, "x"), (2, ""), (2, "   ")])
def test_truncate_refuses_a_non_reduction_or_a_missing_reason(tmp_path, laps, reason):
    out = _plan_dir(tmp_path, laps=3)
    before = _plan_file(out)
    with pytest.raises(SystemExit):
        X.truncate_plan(out, laps, reason)
    assert _plan_file(out) == before


def test_truncate_without_a_plan_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="no crossover plan"):
        X.truncate_plan(str(tmp_path / "m"), 1, "x")


def test_truncate_after_merge_is_refused_and_leaves_the_plan_untouched(tmp_path):
    out = _plan_dir(tmp_path, laps=2)
    X.merge([_phase(tmp_path, k) for k in range(6)], out, ARMS, SETS, CFG)
    before = _plan_file(out)
    with pytest.raises(SystemExit, match="already has merge outputs"):
        X.truncate_plan(out, 1, "결과를 보니 느리다")
    assert _plan_file(out) == before and json.load(open(os.path.join(out, "meta.json")))["plan"]["laps"] == 2


def test_record_plan_after_merge_outputs_exist_is_refused(tmp_path):
    out = _plan_dir(tmp_path, laps=1)
    X.merge([_phase(tmp_path, k) for k in range(3)], out, ARMS, SETS, CFG)
    os.remove(os.path.join(out, "crossover-plan.json"))                           # 계획을 지우고 다시 쓰는 우회
    with pytest.raises(SystemExit, match="before the first phase"):
        X.record_plan(out, ARMS, SETS, 1)


def test_merge_meta_carries_planned_and_actual_phases_and_the_truncation_record(tmp_path):
    out = _plan_dir(tmp_path, laps=3)
    X.truncate_plan(out, 2, "ops-protection: 503 spike", now="2026-10-02T01:00:00+00:00")
    meta = X.merge([_phase(tmp_path, k) for k in range(6)], out, ARMS, SETS, CFG)
    plan = json.load(open(os.path.join(out, "meta.json")))["plan"]
    assert plan == meta["plan"]
    assert plan["planned_phases"] == 9 and plan["actual_phases"] == 6 and plan["laps"] == 3
    assert [(t["reason"], t["laps"], t["planned_phases"]) for t in plan["truncations"]] == \
        [("ops-protection: 503 spike", 2, 6)]
    assert len(plan["plan_file_sha256"]) == 64


def test_merge_refuses_arms_or_sets_that_differ_from_the_recorded_plan(tmp_path):
    out = _plan_dir(tmp_path, laps=1)
    phases = [_phase(tmp_path, k) for k in range(3)]
    for arms, sets in ((["base", "cand", "ctrl"], SETS), (ARMS, ["y", "x", "z"])):
        with pytest.raises(SystemExit, match="differ from the recorded plan"):
            X.merge(phases, out, arms, sets, CFG)
    assert not os.path.exists(os.path.join(out, "results"))


def test_merge_refuses_a_plan_recorded_after_a_phase_started(tmp_path):
    """계획이 결과 뒤에 쓰였으면 선택적 중단을 잡을 수 없다 - 단계 meta.started 가 계획 기록 시각보다 이르면 거부한다."""
    out = _plan_dir(tmp_path, laps=1, now=T0)
    phases = [_phase(tmp_path, k) for k in range(3)]
    for k, started in enumerate(["2026-10-02T00:30:00+00:00", "2026-10-01T23:59:59+00:00", "2026-10-02T00:45:00+00:00"]):
        m = json.load(open(f"{phases[k]}/meta.json"))
        json.dump(dict(m, started=started), open(f"{phases[k]}/meta.json", "w"))
    with pytest.raises(SystemExit, match="phase 1 started .* before the plan was recorded"):
        X.merge(phases, out, ARMS, SETS, CFG)
    assert not os.path.exists(os.path.join(out, "results"))


def test_merge_accepts_phases_that_started_after_the_plan(tmp_path):
    out = _plan_dir(tmp_path, laps=1, now=T0)
    phases = [_phase(tmp_path, k) for k in range(3)]
    for k, d in enumerate(phases):
        m = json.load(open(f"{d}/meta.json"))
        json.dump(dict(m, started=f"2026-10-02T0{k + 1}:00:00+00:00"), open(f"{d}/meta.json", "w"))
    assert X.merge(phases, out, ARMS, SETS, CFG)["plan"]["actual_phases"] == 3


# 계획 x 실제 단계 수 -> 병합 run 을 판정기에 넣은 결과(손으로 정한 기대값: 같은 데이터, 팔 라벨만 다른 NO-CHANGE 쌍)
@pytest.mark.parametrize("laps,truncate_to,phases,held,verdict", [
    (2, None, 6, False, "NO-CHANGE"),         # 계획대로
    (3, None, 6, True, "INCONCLUSIVE"),       # 계획 9 단계인데 6 단계, 기록 없음
    (3, 2, 6, False, "NO-CHANGE"),            # 같은 6 단계지만 사유와 함께 계획을 6 으로 줄였다
    (3, 2, 3, True, "INCONCLUSIVE"),          # 6 으로 줄여 놓고 3 단계만 돌았다 - 기록이 못 덮는 부족분
])
def test_merged_run_is_judged_against_its_recorded_plan(tmp_path, laps, truncate_to, phases, held, verdict):
    out = _plan_dir(tmp_path, laps=laps)
    if truncate_to:
        X.truncate_plan(out, truncate_to, "ops-protection: 503 spike")
    X.merge([_phase(tmp_path, k) for k in range(phases)], out, ARMS, SETS, CFG)
    rep = J.judge(out, "base", "ctrl")
    assert any(r.startswith("plan-truncated:") for r in rep["reasons"]) is held
    assert rep["verdict"] == verdict
    assert rep["plan"]["actual_phases"] == phases and rep["plan"]["planned_phases"] == laps * 3
    assert [t["reason"] for t in rep["plan"]["truncations"]] == (["ops-protection: 503 spike"] if truncate_to else [])


def test_cli_records_truncates_and_merges_a_plan(tmp_path, capsys):
    out = str(tmp_path / "m")
    cfg = json.dumps(CFG)
    flags = [x for a in ARMS for x in ("--arm", a)] + [x for s in SETS for x in ("--set", s)]
    assert X.main(["record-plan", out, *flags, "--laps", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["planned_phases"] == 6
    assert X.main(["truncate", out, "--laps", "1", "--reason", "ops-protection"]) == 0
    assert json.loads(capsys.readouterr().out)["truncations"][0]["planned_phases"] == 3
    phases = [x for k in range(3) for x in ("--phase-dir", _phase(tmp_path, k))]
    assert X.main(["merge", out, *flags, *phases, "--configs", cfg]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["plan"]["actual_phases"] == 3 and summary["plan"]["truncations"][0]["reason"] == "ops-protection"
    with pytest.raises(SystemExit, match="already has merge outputs"):                    # 병합 뒤 축소 거부(CLI 경계)
        X.main(["truncate", out, "--laps", "1", "--reason", "late"])


# ---------------------------------------------------------------- final-review 반영 (issue #77)

def _stamp(phases, started):
    for d, s in zip(phases, started):
        m = json.load(open(f"{d}/meta.json"))
        json.dump(dict(m, started=s), open(f"{d}/meta.json", "w"))


# 계획 기록 시각 x 단계 started 의 시간대 표기 x 앞뒤. 시간대 없는 값은 UTC 로 본다(기준 2026-10-02T00:00 UTC)
@pytest.mark.parametrize("recorded", ["2026-10-02T00:00:00+00:00", "2026-10-02T00:00:00"], ids=["rec-aware", "rec-naive"])
@pytest.mark.parametrize("fmt", ["{}+00:00", "{}"], ids=["start-aware", "start-naive"])
@pytest.mark.parametrize("when,refused", [("2026-10-01T23:59:59", True), ("2026-10-02T00:30:00", False)],
                         ids=["before-plan", "after-plan"])
def test_plan_time_check_handles_timezone_less_stamps_without_a_type_error(tmp_path, recorded, fmt, when, refused):
    out = _plan_dir(tmp_path, laps=1, now=recorded)
    phases = [_phase(tmp_path, k) for k in range(3)]
    _stamp(phases, ["2026-10-02T01:00:00+00:00", fmt.format(when), "2026-10-02T02:00:00+00:00"])
    if refused:
        with pytest.raises(SystemExit, match="phase 1 started .* before the plan was recorded"):
            X.merge(phases, out, ARMS, SETS, CFG)
        assert not os.path.exists(os.path.join(out, "results"))
    else:
        assert X.merge(phases, out, ARMS, SETS, CFG)["plan"]["actual_phases"] == 3


def test_a_phase_meta_without_started_is_accepted_not_crashed(tmp_path):
    """started 가 없는 단계 meta(합성·구버전)는 시각 검사만 건너뛴다 - 나머지 검사는 그대로다."""
    out = _plan_dir(tmp_path, laps=1)
    assert X.merge([_phase(tmp_path, k) for k in range(3)], out, ARMS, SETS, CFG)["rounds"] == 3


# 계획 초과: 유효 계획(마지막 축소 목표, 없으면 처음 계획)보다 단계가 많으면 거부
@pytest.mark.parametrize("laps,truncate_to,phases,ok", [
    (1, None, 3, True), (1, None, 6, False), (2, None, 6, True), (2, None, 9, False),
    (3, 2, 6, True), (3, 2, 9, False),          # 줄여 기록하고도 처음 계획만큼 돌렸다 - 더 돌린 것
])
def test_merge_refuses_more_phases_than_the_effective_plan(tmp_path, laps, truncate_to, phases, ok):
    out = _plan_dir(tmp_path, laps=laps)
    if truncate_to:
        X.truncate_plan(out, truncate_to, "ops-protection")
    dirs = [_phase(tmp_path, k) for k in range(phases)]
    if ok:
        assert X.merge(dirs, out, ARMS, SETS, CFG)["plan"]["actual_phases"] == phases
    else:
        with pytest.raises(SystemExit, match="allows"):
            X.merge(dirs, out, ARMS, SETS, CFG)
        assert not os.path.exists(os.path.join(out, "results")) and not os.path.exists(os.path.join(out, "meta.json"))


def test_a_merge_that_dies_midway_removes_only_its_own_outputs_and_can_be_retried(tmp_path, monkeypatch):
    out = _plan_dir(tmp_path, laps=2)
    X.truncate_plan(out, 1, "ops-protection")
    phases = [_phase(tmp_path, k) for k in range(3)]
    plan_before = open(os.path.join(out, "crossover-plan.json")).read()

    def boom(*a, **kw):
        raise OSError("disk full")
    with monkeypatch.context() as mp:
        mp.setattr(X.shutil, "copy2", boom)
        with pytest.raises(OSError, match="disk full"):
            X.merge(phases, out, ARMS, SETS, CFG)
    assert sorted(os.listdir(out)) == ["crossover-plan.json"]                     # 계획 파일만 남는다
    assert open(os.path.join(out, "crossover-plan.json")).read() == plan_before
    meta = X.merge(phases, out, ARMS, SETS, CFG)                                  # 같은 계획으로 재병합
    assert meta["rounds"] == 3 and meta["plan"]["truncations"][0]["reason"] == "ops-protection"
    assert os.path.exists(os.path.join(out, "meta.json"))


def test_a_failed_merge_leaves_the_plan_truncatable(tmp_path, monkeypatch):
    out = _plan_dir(tmp_path, laps=3)
    phases = [_phase(tmp_path, k) for k in range(3)]
    monkeypatch.setattr(X.shutil, "copy2", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        X.merge(phases, out, ARMS, SETS, CFG)
    assert X.truncate_plan(out, 1, "ops-protection")["truncations"][0]["laps"] == 1


def test_a_merge_refused_before_writing_does_not_touch_existing_outputs(tmp_path):
    """results/ 가 이미 있어 병합이 시작도 못 한 경우의 정리는 다른 병합의 산출물을 지우면 안 된다."""
    out = _plan_dir(tmp_path, laps=1)
    os.makedirs(os.path.join(out, "results"))
    open(os.path.join(out, "results", "keep.json"), "w").write("{}")
    with pytest.raises(FileExistsError):
        X.merge([_phase(tmp_path, k) for k in range(3)], out, ARMS, SETS, CFG)
    assert os.path.exists(os.path.join(out, "results", "keep.json"))


def test_merge_meta_hash_and_plan_come_from_the_same_bytes(tmp_path):
    import hashlib
    out = _plan_dir(tmp_path, laps=1)
    raw = open(os.path.join(out, "crossover-plan.json"), "rb").read()
    plan = X.merge([_phase(tmp_path, k) for k in range(3)], out, ARMS, SETS, CFG)["plan"]
    assert plan["plan_file_sha256"] == hashlib.sha256(raw).hexdigest()
    assert {k: plan[k] for k in json.loads(raw)} == json.loads(raw)


# ---------------------------------------------------------------- B2 팔: 포트·배치 provenance 대조 (issue #83)

B2 = {"base": {"port": 18781, "batch_size": 32}, "ctrl": {"port": 18781, "batch_size": 32},
      "cand": {"port": 18782, "batch_size": 16}}
B2_PB = {a: (v["port"], v["batch_size"]) for a, v in B2.items()}


def _b2_plan_dir(tmp_path, laps=1, specs=B2):
    out = str(tmp_path / "m")
    X.record_plan(out, ARMS, SETS, laps, arm_sidecars=specs)
    return out


def test_record_plan_stores_each_arms_sidecar_port_and_batch_on_the_arm_entry(tmp_path):
    doc = _plan_file(_b2_plan_dir(tmp_path))
    assert doc["arms"] == [{"name": "base", "sidecar": {"port": 18781, "batch_size": 32}},
                           {"name": "ctrl", "sidecar": {"port": 18781, "batch_size": 32}},
                           {"name": "cand", "sidecar": {"port": 18782, "batch_size": 16}}]


def test_record_plan_without_arm_sidecars_keeps_the_arm_entries_as_before(tmp_path):
    assert _plan_file(_plan_dir(tmp_path))["arms"] == [{"name": a} for a in ARMS]


@pytest.mark.parametrize("specs", [
    {"cand": {"port": 18782, "batch_size": 16}},                                       # 일부 팔만 - 나머지 팔의 포트가 단계마다 흘러간다
    {**B2, "cand": {"port": 8787, "batch_size": 16}},                                  # 운영 사이드카 포트
    {**B2, "cand": {"port": 80, "batch_size": 16}},                                    # 벤치 포트 대역 밖
    {**B2, "cand": {"port": 18782, "batch_size": 0}},
    {**B2, "cand": {"port": 18782}},                                                   # 배치 없음
    {**B2, "cand": {"batch_size": 16}},                                                # 포트 없음
    {**B2, "cand": {"port": "18782", "batch_size": 16}},                               # 정수가 아니다
    {**B2, "other": {"port": 18783, "batch_size": 8}},                                 # 계획에 없는 팔
])
def test_record_plan_refuses_a_bad_arm_sidecar_and_writes_nothing(tmp_path, specs):
    with pytest.raises(SystemExit):
        X.record_plan(str(tmp_path / "m"), ARMS, SETS, 1, arm_sidecars=specs)
    assert not (tmp_path / "m" / "crossover-plan.json").exists()


def test_merge_accepts_phases_whose_sets_pointed_at_the_planned_port_and_batch(tmp_path):
    out = _b2_plan_dir(tmp_path, laps=2)
    meta = X.merge([_phase(tmp_path, k, pb=B2_PB) for k in range(6)], out, ARMS, SETS, CFG)
    assert meta["rounds"] == 6
    assert [a["sidecar"]["port"] for a in meta["plan"]["arms"]] == [18781, 18781, 18782]    # 계획 파일 그대로 meta 에 실린다


# 어긋난 단계 위치 x 어긋난 값. cand 가 단계 k 에 쓴 세트의 state 만 바뀐다
@pytest.mark.parametrize("bad_k", [0, 1, 2, 4])
@pytest.mark.parametrize("wrong", [(18781, 16), (18782, 32), (18781, 32), (None, None)],
                         ids=["port", "batch", "both", "missing"])
def test_merge_refuses_a_phase_whose_set_pointed_at_another_port_or_batch_than_the_arm_needs(tmp_path, bad_k, wrong):
    out = _b2_plan_dir(tmp_path, laps=2)
    phases = []
    for k in range(6):
        pb_set = {X.plan(ARMS, SETS, k)["cand"]: wrong} if k == bad_k else None
        phases.append(_phase(tmp_path, k, pb=B2_PB, pb_set=pb_set))
    with pytest.raises(SystemExit, match=rf"phase {bad_k}: .*sidecar"):
        X.merge(phases, out, ARMS, SETS, CFG)
    assert not os.path.exists(os.path.join(out, "results")) and not os.path.exists(os.path.join(out, "meta.json"))


def test_merge_refuses_a_control_arm_that_pointed_at_the_aux_port(tmp_path):
    """A/A 대조(ctrl)가 B2 포트에 붙으면 두 팔의 차이가 배치 하나가 아니게 된다."""
    out = _b2_plan_dir(tmp_path)
    ctrl_set = X.plan(ARMS, SETS, 1)["ctrl"]
    phases = [_phase(tmp_path, 0, pb=B2_PB), _phase(tmp_path, 1, pb=B2_PB, pb_set={ctrl_set: (18782, 16)}),
              _phase(tmp_path, 2, pb=B2_PB)]
    with pytest.raises(SystemExit, match="phase 1"):
        X.merge(phases, out, ARMS, SETS, CFG)


def test_merge_without_planned_sidecars_ignores_which_port_the_sets_share(tmp_path):
    """계획에 팔 사이드카 지정이 없으면 기존 동작 - 세트들이 같은 포트(주 또는 보조)를 공유하는 한 어느 포트인지는 보지 않는다."""
    out = _plan_dir(tmp_path, laps=1)
    assert X.merge([_phase(tmp_path, k, pb={a: (18782, 16) for a in ARMS}) for k in range(3)], out, ARMS, SETS, CFG)["rounds"] == 3


def test_cli_records_arm_sidecars_and_merges_against_them(tmp_path, capsys):
    out = str(tmp_path / "m")
    flags = [x for a in ARMS for x in ("--arm", a)] + [x for s in SETS for x in ("--set", s)]
    side = ["--arm-sidecar", "base=18781:32", "--arm-sidecar", "ctrl=18781:32", "--arm-sidecar", "cand=18782:16"]
    assert X.main(["record-plan", out, *flags, "--laps", "1", *side]) == 0
    assert [a["sidecar"] for a in json.loads(capsys.readouterr().out)["arms"]][2] == {"port": 18782, "batch_size": 16}
    phases = [x for k in range(3) for x in ("--phase-dir", _phase(tmp_path, k, pb=B2_PB))]
    assert X.main(["merge", out, *flags, *phases, "--configs", json.dumps(CFG)]) == 0
    bad = [x for k in range(3) for x in ("--phase-dir", _phase(tmp_path, k + 3, pb=dict(B2_PB, cand=(18781, 32))))]
    os.remove(os.path.join(out, "meta.json"))
    import shutil
    shutil.rmtree(os.path.join(out, "results"))
    with pytest.raises(SystemExit, match="sidecar"):
        X.main(["merge", out, *flags, *bad, "--configs", json.dumps(CFG)])


@pytest.mark.parametrize("flag", ["cand", "cand=18782", "cand=x:16", "cand=18782:", "=18782:16"])
def test_cli_refuses_a_malformed_arm_sidecar_flag(tmp_path, flag):
    flags = [x for a in ARMS for x in ("--arm", a)] + [x for s in SETS for x in ("--set", s)]
    with pytest.raises(SystemExit):
        X.main(["record-plan", str(tmp_path / "m"), *flags, "--laps", "1", "--arm-sidecar", flag])


def _run_phase_stubs(monkeypatch):
    from bench import replica, runner
    calls = []
    monkeypatch.setattr(replica, "reconf", lambda *a, **k: calls.append(("reconf", a, k)))
    monkeypatch.setattr(replica, "verify", lambda s: {"pass": True})
    monkeypatch.setattr(runner, "run", lambda *a, **k: calls.append(("run", a)) or "/runs/x")
    monkeypatch.setattr(runner, "read_token", lambda: "t")
    return calls


def test_run_phase_points_each_set_at_the_planned_port_and_batch_of_the_arm_it_hosts(tmp_path, monkeypatch):
    calls = _run_phase_stubs(monkeypatch)
    out = _b2_plan_dir(tmp_path)
    X.main(["run-phase", *[x for a in ARMS for x in ("--arm", a)], *[x for s in SETS for x in ("--set", s)],
            "--gw-port", "x=1", "--gw-port", "y=2", "--gw-port", "z=3", "--configs", json.dumps(CFG), "--phase", "1",
            "--run-id", "r", "--plan-dir", out])
    recs = {c[1][0]: c[2] for c in calls if c[0] == "reconf"}
    assign = X.plan(ARMS, SETS, 1)
    assert {s: (k["sidecar_port"], k["sidecar_batch_size"]) for s, k in recs.items()} == \
        {assign[a]: B2_PB[a] for a in ARMS}


def test_run_phase_without_a_plan_dir_keeps_the_old_reconf_call(tmp_path, monkeypatch):
    from bench import replica
    calls = _run_phase_stubs(monkeypatch)
    monkeypatch.setattr(replica, "STATE_DIR", str(tmp_path))          # 세트 state 를 읽는다 - 실제 벤치 state 를 보지 않게
    X.main(["run-phase", *[x for a in ARMS for x in ("--arm", a)], *[x for s in SETS for x in ("--set", s)],
            "--gw-port", "x=1", "--gw-port", "y=2", "--gw-port", "z=3", "--configs", json.dumps(CFG), "--phase", "0",
            "--run-id", "r"])
    assert all(c[2] == {} for c in calls if c[0] == "reconf") and len([c for c in calls if c[0] == "reconf"]) == 3


def test_run_phase_refuses_a_plan_dir_whose_arms_or_sets_differ(tmp_path, monkeypatch):
    calls = _run_phase_stubs(monkeypatch)
    out = _b2_plan_dir(tmp_path)
    with pytest.raises(SystemExit, match="differ from the recorded plan"):
        X.main(["run-phase", *[x for a in ARMS for x in ("--arm", a)], "--set", "y", "--set", "x", "--set", "z",
                "--gw-port", "x=1", "--configs", "{}", "--phase", "0", "--run-id", "r", "--plan-dir", out])
    assert not calls


@pytest.mark.parametrize("mutate", [
    lambda doc: doc["arms"][0].pop("sidecar"),                                  # 일부 팔만 남았다(손으로 고친 계획)
    lambda doc: doc["arms"][2]["sidecar"].update(port=8787),                    # 운영 사이드카 포트
])
def test_a_hand_edited_plan_with_bad_arm_sidecars_is_refused_with_a_reason_at_merge_and_run_phase(tmp_path, monkeypatch, mutate):
    calls = _run_phase_stubs(monkeypatch)
    out = _b2_plan_dir(tmp_path)
    path = os.path.join(out, "crossover-plan.json")
    doc = json.load(open(path))
    mutate(doc)
    json.dump(doc, open(path, "w"))
    with pytest.raises(SystemExit, match="arm"):
        X.merge([_phase(tmp_path, k, pb=B2_PB) for k in range(3)], out, ARMS, SETS, CFG)
    with pytest.raises(SystemExit, match="arm"):
        X.main(["run-phase", *[x for a in ARMS for x in ("--arm", a)], *[x for s in SETS for x in ("--set", s)],
                "--gw-port", "x=1", "--configs", "{}", "--phase", "0", "--run-id", "r", "--plan-dir", out])
    assert not calls


# final-review: 팔 사이드카 지정이 없는 계획에서도 한 단계의 세트들은 같은 사이드카 포트·배치여야 한다(주 포트와 보조 포트는 overrides 가 같다)
@pytest.mark.parametrize("bad_set_pb", [(18782, 16), (18781, 16), (18782, 32), (None, None)],
                         ids=["aux", "batch-only", "port-only", "missing-while-others-set"])
def test_merge_without_planned_sidecars_refuses_sets_pointing_at_different_ports_or_batches(tmp_path, bad_set_pb):
    out = _plan_dir(tmp_path, laps=1)
    same = {a: (18781, 32) for a in ARMS}
    odd_set = X.plan(ARMS, SETS, 1)["cand"]
    phases = [_phase(tmp_path, 0, pb=same), _phase(tmp_path, 1, pb=same, pb_set={odd_set: bad_set_pb}),
              _phase(tmp_path, 2, pb=same)]
    with pytest.raises(SystemExit, match="phase 1: sets pointed at different sidecar ports"):
        X.merge(phases, out, ARMS, SETS, CFG)


def test_merge_without_planned_sidecars_accepts_sets_on_one_port(tmp_path):
    out = _plan_dir(tmp_path, laps=1)
    same = {a: (18782, 16) for a in ARMS}
    assert X.merge([_phase(tmp_path, k, pb=same) for k in range(3)], out, ARMS, SETS, CFG)["rounds"] == 3


def test_cli_refuses_the_same_arm_sidecar_twice_instead_of_letting_the_last_win(tmp_path):
    flags = [x for a in ARMS for x in ("--arm", a)] + [x for s in SETS for x in ("--set", s)]
    side = ["--arm-sidecar", "base=18781:32", "--arm-sidecar", "ctrl=18781:32", "--arm-sidecar", "cand=18782:16",
            "--arm-sidecar", "cand=18781:32"]
    with pytest.raises(SystemExit, match="more than once"):
        X.main(["record-plan", str(tmp_path / "m"), *flags, "--laps", "1", *side])
    assert not (tmp_path / "m" / "crossover-plan.json").exists()


@pytest.mark.parametrize("carrying", [True, False])
def test_run_phase_without_a_plan_dir_refuses_when_a_set_carries_an_expected_sidecar_batch(tmp_path, monkeypatch, carrying):
    """--plan-dir 을 빠뜨리면 세트가 앞 포트에 남은 채 단계 전체(holdout 1회분)가 측정된다 - reconf 전에 막는다. 기대 배치가 없는 세트는 기존 동작."""
    from bench import replica
    calls = _run_phase_stubs(monkeypatch)
    monkeypatch.setattr(replica, "STATE_DIR", str(tmp_path))
    for s in SETS:
        (tmp_path / f"replica-{s}.json").write_text(json.dumps({"sidecar_port": 18781, **({"sidecar_batch_size": 32} if carrying else {})}))
    argv = ["run-phase", *[x for a in ARMS for x in ("--arm", a)], *[x for s in SETS for x in ("--set", s)],
            "--gw-port", "x=1", "--gw-port", "y=2", "--gw-port", "z=3", "--configs", json.dumps(CFG), "--phase", "0", "--run-id", "r"]
    if carrying:
        with pytest.raises(SystemExit, match="--plan-dir"):
            X.main(argv)
        assert not calls
    else:
        X.main(argv)
        assert len([c for c in calls if c[0] == "reconf"]) == 3


def test_run_phase_of_a_plan_with_arm_sidecars_refuses_a_missing_plan_dir_but_runs_with_it(tmp_path, monkeypatch):
    """지정이 있는 계획(B2): 세트 state 에 기대 배치가 있는데 --plan-dir 을 빠뜨리면 reconf 전에 거부, 주면 계획의 값으로 진행."""
    from bench import replica
    calls = _run_phase_stubs(monkeypatch)
    monkeypatch.setattr(replica, "STATE_DIR", str(tmp_path))
    out = _b2_plan_dir(tmp_path)
    for s in SETS:
        (tmp_path / f"replica-{s}.json").write_text(json.dumps({"sidecar_port": 18781, "sidecar_batch_size": 32}))
    argv = ["run-phase", *[x for a in ARMS for x in ("--arm", a)], *[x for s in SETS for x in ("--set", s)],
            "--gw-port", "x=1", "--gw-port", "y=2", "--gw-port", "z=3", "--configs", json.dumps(CFG), "--phase", "1",
            "--run-id", "r"]
    with pytest.raises(SystemExit, match="--plan-dir"):
        X.main(argv)
    assert not calls
    X.main([*argv, "--plan-dir", out])
    assert sorted(c[2]["sidecar_port"] for c in calls if c[0] == "reconf") == [18781, 18781, 18782]


# ---------------------------------------------------------------- 교차 단계 keep-warm 연결과 병합 meta 제외 줄 (issue #90)
# 실제 송신은 어디에도 없다: 송신기를 주입하고, 아래 autouse 픽스처가 이 파일 모든 테스트의 소켓 연결을 막는다.

T_KW = 1_790_000_000.0


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    attempts = []

    def blocked(self, address, *a, **k):
        attempts.append(address)
        raise ConnectionRefusedError("blocked in tests: no real network send")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, address, *a: attempts.append(address) or 111)
    return attempts


class _World:
    """가짜 시계 + 송신기. recall 한 번마다 시계가 5초 가고, 송신·호출 순서를 events 에 남긴다."""
    def __init__(self):
        self.t, self.events = T_KW, []

    def now(self):
        return self.t

    def send(self):
        self.events.append("kw")
        return 200


def _real_runner_in_a_fake_world(tmp_path, monkeypatch, world):
    """replica(docker)는 막고, runner.run 은 진짜로 돌린다 - 게이트웨이 클라이언트·코퍼스·가드 등 바깥 세계만 가짜."""
    from bench import replica, runner
    def reconf(*a, **k):                                   # reconf·verify 는 시간이 걸린다 - 이 구간에 keep-warm 을 보내면 events 와 started 에 드러난다
        world.events.append("reconf")
        world.t += 10.0

    def verify(s):
        world.events.append("verify")
        world.t += 10.0
        return {"pass": True}

    monkeypatch.setattr(replica, "reconf", reconf)
    monkeypatch.setattr(replica, "verify", verify)

    class Client:
        def __init__(self, port, token):
            pass

        def open(self):
            pass

        def recall(self, args):
            world.events.append("recall")
            world.t += 5.0
            return {"latency_ms": 1.0, "outcome": "transport-error", "detail": "fake"}

    class Tok:
        def count(self, text):
            return len(text)

    class Guard:
        def check(self):
            pass

        def meta(self):
            return {"windows": [], "pauses": [], "aborted": None}

    corpus = [{"key": f"k{i}", "args": {"query": f"q{i}"}} for i in range(3)]
    monkeypatch.setattr(runner, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(runner, "McpClient", Client)
    monkeypatch.setattr(runner, "Tokenizer", Tok)
    monkeypatch.setattr(runner, "load_corpus", lambda split, limit, seed: corpus)
    monkeypatch.setattr(runner, "warmup_pool", lambda corpus, seed: [])
    monkeypatch.setattr(runner, "provenance", lambda arms: {"replicas": {"": {}}, "arms": {}})
    monkeypatch.setattr(runner, "common_bench_clock", lambda reps: "2026-09-26T00:00:00Z")
    monkeypatch.setattr(runner, "tei_queue_depth", lambda url: 0)
    monkeypatch.setattr(runner, "rewrite_failures", lambda port: 0)
    monkeypatch.setattr(runner, "prod_audit_offset", lambda: None)
    monkeypatch.setattr(runner, "sidecar_log_offset", lambda: 0)
    monkeypatch.setattr(runner, "sidecar_errors_since", lambda off: {"http_503": 0, "deadline": 0, "queue_full": 0})
    monkeypatch.setattr(runner, "read_token", lambda: "t")
    real_run = runner.run
    monkeypatch.setattr(runner, "run", lambda *a, **k: real_run(
        *a, fingerprint=lambda sets: {}, check_ports=lambda arms: None, gap_ms=0, selfcheck=lambda: None,
        guard=Guard(), **k))


def _phase_args(run_id):
    return {"arms": ARMS, "sets": SETS, "gw_ports": {"x": 1, "y": 2, "z": 3}, "configs": CFG, "split": "dev",
            "limit": None, "run_id": run_id}


def test_run_phase_sends_keepwarm_only_while_the_phase_runs_and_records_it_in_the_phase_meta(tmp_path, monkeypatch, no_real_network):
    from bench.keepwarm import KeepWarm
    w = _World()
    _real_runner_in_a_fake_world(tmp_path, monkeypatch, w)
    kw = KeepWarm(w.send, now=w.now, interval_s=10.0)
    out = X.run_phase(0, *_phase_args("kw0").values(), keepwarm=kw)
    # 호출 9개(3쿼리 x 3팔), 호출마다 시계 +5s, 호출 직전 tick, 간격 10s: 손계산 - t=0,10,20,30,40 에서 송신, 45에서 끝
    assert w.events == ["reconf"] * 3 + ["verify"] * 3 + ["kw", "recall", "recall"] * 4 + ["kw", "recall"]    # reconf·verify 구간(60초)에는 안 보낸다
    assert w.events.count("kw") == 5 and w.events.count("recall") == 9
    n = len(w.events)
    w.t += 1000
    kw.tick()                                              # 단계가 끝난 뒤에는 간격이 지나도 보내지 않는다
    assert len(w.events) == n
    meta = json.load(open(os.path.join(out, "meta.json")))["keepwarm"]
    assert meta["sent"] == 5 and meta["failed"] == 0
    assert meta["started"] == T_KW + 60 and meta["stopped"] == T_KW + 105
    assert len(canary.parse_exclude(meta["exclude_line"])) == 1          # 제외 파서를 그대로 통과한다
    assert not no_real_network


def test_run_phase_without_a_keepwarm_sends_nothing_and_records_it_as_off(tmp_path, monkeypatch, no_real_network):
    w = _World()
    _real_runner_in_a_fake_world(tmp_path, monkeypatch, w)
    out = X.run_phase(0, *_phase_args("kw-off").values())
    assert w.events == ["reconf"] * 3 + ["verify"] * 3 + ["recall"] * 9
    assert json.load(open(os.path.join(out, "meta.json")))["keepwarm"] is None
    assert not no_real_network


def test_cli_run_phase_turns_on_the_production_keepwarm(tmp_path, monkeypatch):
    from bench import replica, runner
    from bench.keepwarm import PROD_RERANK_URL, KeepWarm
    got = {}
    monkeypatch.setattr(replica, "reconf", lambda *a, **k: None)
    monkeypatch.setattr(replica, "verify", lambda s: {"pass": True})
    monkeypatch.setattr(replica, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(runner, "read_token", lambda: "t")
    monkeypatch.setattr(runner, "run", lambda *a, **k: got.update(k) or "/runs/x")
    X.main(["run-phase", *[x for a in ARMS for x in ("--arm", a)], *[x for s in SETS for x in ("--set", s)],
            "--gw-port", "x=1", "--gw-port", "y=2", "--gw-port", "z=3", "--configs", json.dumps(CFG), "--phase", "0",
            "--run-id", "r"])
    assert isinstance(got["keepwarm"], KeepWarm) and got["keepwarm"].url == PROD_RERANK_URL


def test_cli_run_phase_sends_through_the_production_keepwarm_during_the_phase(tmp_path, monkeypatch, no_real_network):
    """CLI 경로 전체(runner.run 은 진짜): KeepWarm.production() 이 만드는 객체의 송신기만 주입으로 바꿔도 단계 meta 에 keepwarm 이 남는다."""
    from bench.keepwarm import KeepWarm
    w = _World()
    _real_runner_in_a_fake_world(tmp_path, monkeypatch, w)
    monkeypatch.setattr(KeepWarm, "production", classmethod(lambda cls: cls(w.send, now=w.now, interval_s=10.0)))
    from bench import replica
    monkeypatch.setattr(replica, "STATE_DIR", str(tmp_path))
    X.main(["run-phase", *[x for a in ARMS for x in ("--arm", a)], *[x for s in SETS for x in ("--set", s)],
            "--gw-port", "x=1", "--gw-port", "y=2", "--gw-port", "z=3", "--configs", json.dumps(CFG), "--phase", "0",
            "--run-id", "kwcli"])
    meta = json.load(open(tmp_path / "runs" / "kwcli" / "meta.json"))["keepwarm"]
    assert meta["sent"] == 5 and w.events.count("kw") == 5
    assert not no_real_network


# 병합: 단계 meta 의 keep-warm 구간을 단계 순서대로 싣는다. 줄은 손으로 적은 카나리 제외 형식(KST)이다.
L0 = "2026-09-21 12:00:00 2026-09-21 12:35:00  # 벤치 keep-warm 구간 p0 (끝 +300s: cold 판정 꼬리)"
L1 = "2026-09-21 12:40:00 2026-09-21 13:20:00  # 벤치 keep-warm 구간 p1 (끝 +300s: cold 판정 꼬리)"
L2 = "2026-09-21 13:25:00 2026-09-21 14:05:00  # 벤치 keep-warm 구간 p2 (끝 +300s: cold 판정 꼬리)"


def _kw_meta(line, sent=4, failed=0):
    return {"url": "http://127.0.0.1:8787/rerank", "interval_s": 30.0, "started": 1.0, "stopped": 2.0, "sent": sent,
            "failed": failed, "sent_at": [], "failures": [], "exclude_line": line}


def test_merge_meta_lists_each_phases_keepwarm_window_and_exclude_line_in_phase_order(tmp_path):
    kws = [_kw_meta(L0, sent=4), {**_kw_meta(L1, sent=5, failed=1),
                                  "failures": [{"t": 1790000100.0, "error": "rerank_queue_timeout", "status": 503}]}, _kw_meta(L2, sent=6)]
    phases = [_phase(tmp_path, k, kw=kws[k]) for k in range(3)]
    meta = _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)
    assert meta["keepwarm"]["exclude_lines"] == [L0, L1, L2]
    assert [(p["phase"], p["run_id"], p["sent"], p["failed"]) for p in meta["keepwarm"]["phases"]] == \
        [(0, "p0", 4, 0), (1, "p1", 5, 1), (2, "p2", 6, 0)]
    assert [p["exclude_line"] for p in meta["keepwarm"]["phases"]] == [L0, L1, L2]
    assert [p["failures"] for p in meta["keepwarm"]["phases"]] == [[], [{"t": 1790000100.0, "error": "rerank_queue_timeout", "status": 503}], []]
    assert meta["keepwarm"]["unmerged"] == []
    assert json.load(open(tmp_path / "m" / "meta.json"))["keepwarm"] == meta["keepwarm"]
    # 줄을 그대로 이어 붙인 파일이 카나리 제외 파서를 통과하고 단계 3개가 구간 3개가 된다
    parsed = canary.parse_exclude("\n".join(meta["keepwarm"]["exclude_lines"]))
    assert len(parsed) == 3 and [r[2].split()[3] for r in parsed] == ["p0", "p1", "p2"] and parsed[0][0] < parsed[0][1] < parsed[1][0]


@pytest.mark.parametrize("present, expect", [
    ([True, False, True], [L0, L2]),            # 가운데 단계만 keep-warm 기록 없음(옛 단계 meta)
    ([False, False, False], []),                 # 전부 옛 단계 meta
    ([True, True, True], [L0, L1, L2]),
], ids=["middle-missing", "all-missing", "all-present"])
def test_merge_passes_phases_without_a_keepwarm_record_and_skips_their_lines(tmp_path, present, expect):
    lines = [L0, L1, L2]
    phases = [_phase(tmp_path, k, kw=_kw_meta(lines[k]) if present[k] else None) for k in range(3)]
    meta = _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)
    assert meta["keepwarm"]["exclude_lines"] == expect
    assert [p["exclude_line"] for p in meta["keepwarm"]["phases"]] == [l if on else None for l, on in zip(lines, present)]
    assert [p["sent"] for p in meta["keepwarm"]["phases"]] == [4 if on else None for on in present]
    assert meta["rounds"] == 3 and J.judge(str(tmp_path / "m"), "base", "ctrl")["verdict"] == "NO-CHANGE"


def test_merge_skips_the_line_of_a_phase_whose_keepwarm_never_started(tmp_path):
    """keep-warm 이 켜졌지만 시작 전에 끝난 단계(가드 거부 등)는 exclude_line 이 null 이다 - 목록에 null 을 싣지 않는다."""
    phases = [_phase(tmp_path, 0, kw=_kw_meta(L0)), _phase(tmp_path, 1, kw=_kw_meta(None, sent=0)),
              _phase(tmp_path, 2, kw=_kw_meta(L2))]
    meta = _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)
    assert meta["keepwarm"]["exclude_lines"] == [L0, L2]
    assert [p["sent"] for p in meta["keepwarm"]["phases"]] == [4, 0, 4]


def test_cli_merge_prints_the_exclude_lines(tmp_path, capsys):
    phases = [_phase(tmp_path, k, kw=_kw_meta([L0, L1, L2][k])) for k in range(3)]
    out_dir = str(tmp_path / "m")
    X.record_plan(out_dir, ARMS, SETS, 1)
    flags = [x for a in ARMS for x in ("--arm", a)] + [x for s in SETS for x in ("--set", s)]
    X.main(["merge", out_dir, *flags, *[x for p in phases for x in ("--phase-dir", p)], "--configs", json.dumps(CFG)])
    assert json.loads(capsys.readouterr().out)["keepwarm"]["exclude_lines"] == [L0, L1, L2]


@pytest.mark.parametrize("skw, expect", [
    ([{"http_503": 1, "deadline": 1, "queue_full": 0}, None, {"http_503": 2, "deadline": 0, "queue_full": 1}],
     {"http_503": 3, "deadline": 1, "queue_full": 1}),                      # 꺼진(옛) 단계는 0 으로 센다
    ([None, None, None], None),                                              # 전부 꺼짐 = 병합도 꺼짐
], ids=["sum-skipping-off-phases", "all-off"])
def test_merge_sums_the_keepwarms_own_sidecar_errors_so_they_can_be_subtracted_from_prod_sidecar(tmp_path, skw, expect):
    """prod_sidecar 합에는 keep-warm 자신의 503 도 들어 있다(runner meta 주석) - 병합 meta 에서도 같은 키로 뺄 수 있어야 한다."""
    phases = [_phase(tmp_path, k, skw=skw[k]) for k in range(3)]
    assert _merge(phases, str(tmp_path / "m"), ARMS, SETS, CFG)["prod_sidecar_keepwarm"] == expect


def test_merge_reports_a_crashed_attempts_keepwarm_window_that_is_not_in_the_merged_phases(tmp_path, capsys):
    """단계 시도가 크래시해 keepwarm.json 만 남으면(runner) 병합 입력에 없어 exclude_lines 에서 빠진다 - 따로 싣고 경고한다."""
    phases = [_phase(tmp_path, k, kw=_kw_meta([L0, L1, L2][k])) for k in range(3)]
    crashed = tmp_path / "p1-crashed"
    crashed.mkdir()
    json.dump(_kw_meta("2026-09-21 12:36:00 2026-09-21 12:39:00  # 벤치 keep-warm 구간 p1-crashed (끝 +300s: cold 판정 꼬리)"),
              open(crashed / "keepwarm.json", "w"))
    (tmp_path / "unrelated").mkdir()                                       # keepwarm.json 도 meta.json 도 없는 디렉터리는 무시한다
    out_dir = str(tmp_path / "m")
    X.record_plan(out_dir, ARMS, SETS, 1)
    flags = [x for a in ARMS for x in ("--arm", a)] + [x for s in SETS for x in ("--set", s)]
    X.main(["merge", out_dir, *flags, *[x for p in phases for x in ("--phase-dir", p)], "--configs", json.dumps(CFG)])
    cap = capsys.readouterr()
    kw = json.loads(cap.out)["keepwarm"]
    assert kw["unmerged"] == [{"run_id": "p1-crashed",
                               "exclude_line": "2026-09-21 12:36:00 2026-09-21 12:39:00  # 벤치 keep-warm 구간 p1-crashed (끝 +300s: cold 판정 꼬리)"}]
    assert kw["exclude_lines"] == [L0, L1, L2]                              # 병합된 단계의 줄만. 크래시 시도의 줄은 사람이 확인해 붙인다
    assert "1 unmerged keep-warm window" in cap.err and "병합되지 않은 keep-warm 구간 1개" in cap.err
