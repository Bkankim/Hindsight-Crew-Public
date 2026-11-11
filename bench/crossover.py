"""recall 재생 하네스 - 순환 교차 run (issue #13 B).

세트(복제본 인스턴스)마다 고정 지연 오프셋이 있다(selftest5-aa3: 159ms 쿼리에서 한 인스턴스가 3 라운드 내내 +30%).
upstream 설정이 다른 후보는 설정이 곧 인스턴스라 이 오프셋이 후보 효과와 섞인다. 그래서 설정(팔)을 단계마다
인스턴스에 돌려 배정한다: 단계 k 에서 팔 i 는 세트 (i + k) mod n 을 쓴다. n 단계를 돌면 모든 팔이 모든 세트를
정확히 한 번씩 거치므로, 쿼리별 로그 비를 단계에 걸쳐 평균하면(judge 의 crossover 모드) 인스턴스 오프셋이 상쇄된다.

단계마다 runner 를 --rounds 1 로 한 번 돌리고, merge 가 단계 run 들을 라운드 = 단계인 run 하나로 합친다.
merge 는 각 단계에서 팔 라벨이 계획한 세트에, 그 세트가 그 팔의 설정으로 떠 있었는지를 provenance 로 확인한다.

계획 기록(issue #77): 결과를 보고 중간에 멈추는 선택적 중단을 판정기가 잡으려면 계획한 단계 수가 첫 단계 전에 남아 있어야 한다.
`record-plan` 이 병합 대상 run 디렉터리(<runs>/<id>)에 crossover-plan.json 을 쓰고(팔, 세트, 바퀴 수, 계획 단계 수,
기록 시각), 계획을 줄이는 길은 `truncate`(사유·시각을 덧붙임)뿐이며 병합이 시작된 뒤에는 거부한다.
merge 는 계획 파일이 없으면 거부하고, 계획·실제 단계 수와 축소 기록을 meta.plan 에 싣는다(판정기가 plan-truncated 로 본다).

팔 사이드카 지정(issue #83): B2 처럼 팔이 사이드카 배치로 갈리면 팔마다 세트가 붙을 벤치 사이드카 포트(주 또는 보조)와 기대 배치를
계획 파일의 팔 항목(`{"name", "sidecar": {"port", "batch_size"}}`)에 함께 기록한다(모든 팔 또는 없음). run-phase 는 --plan-dir 의 계획에서
이 값을 읽어 세트를 그 포트로 reconf 하고(verify 가 사이드카 info 의 배치와 대조), merge 는 단계마다 세트 provenance 의 포트·배치가
그 단계에 배정된 팔의 계획값과 같은지 본다. 지정이 없는 계획은 기존 동작 그대로다.

keep-warm(issue #90): run-phase 는 runner 와 같은 방식으로 운영 사이드카 keep-warm 을 켠다. run_phase 는 keepwarm 을 인자로 받고(기본 꺼짐 -
테스트가 운영 사이드카를 치지 않게), 실제 송신기는 CLI(main)만 만든다(KeepWarm.production). runner.run 이 단계가 도는 동안만 보내고
단계 meta.keepwarm 에 구간·실패·카나리 제외 줄을 남긴다. merge 는 단계 meta 의 keep-warm 기록을 단계 순서대로 meta.keepwarm 에 옮긴다:
`phases` = 단계별 {phase, run_id, started, stopped, sent, failed, failures, exclude_line}(기록이 없는 과거 단계는 값이 null; failures = 실패 시각·사유,
sent_at 전체 목록은 싣지 않는다), `unmerged` = 단계 디렉터리 옆에 meta.json 없이 keepwarm.json 만 남은(크래시한) 시도의 {run_id, exclude_line}, `exclude_lines` = 카나리
제외 파일에 붙일 줄(단계 순서, 줄이 없는 단계는 건너뜀). 판정기는 이 키를 읽지 않는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

from bench.keepwarm import KeepWarm                # 표준 라이브러리만 쓴다 - 실제 송신기는 CLI(main)만 만든다
from bench.replica import check_arm_sidecar        # 표준 라이브러리만 쓴다(docker 호출은 함수 안) - 포트·배치 검사 규칙 한 곳


def plan(arms: List[str], sets: List[str], phase: int) -> Dict[str, str]:
    """단계 phase 의 팔 -> 세트 배정(순환 라틴 방진)."""
    if len(arms) != len(sets) or len(set(sets)) != len(sets) or len(set(arms)) != len(arms):
        raise SystemExit(f"crossover needs as many distinct sets as arms: arms={arms} sets={sets}")
    n = len(arms)
    return {arm: sets[(i + phase) % n] for i, arm in enumerate(arms)}


SAME_ACROSS_PHASES = ("split", "bank", "bench_clock", "corpus_keys_sha256")

PLAN_FILE = "crossover-plan.json"
PLAN_VERSION = 1
MERGE_OUTPUTS = ("results", "calls.jsonl", "meta.json")      # merge 가 만드는 산출물 - 하나라도 있으면 병합이 시작된 것


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _plan_path(out_dir: str) -> str:
    return os.path.join(out_dir, PLAN_FILE)


def _merge_started(out_dir: str) -> bool:
    return any(os.path.exists(os.path.join(out_dir, f)) for f in MERGE_OUTPUTS)


def _remove_merge_outputs(out_dir: str) -> None:
    """실패한 병합이 남긴 산출물만 지운다. 계획 파일은 남겨 재시도(와 사유 있는 축소)가 가능하게 한다."""
    shutil.rmtree(os.path.join(out_dir, "results"), ignore_errors=True)
    for f in ("calls.jsonl", "prod_events.jsonl", "meta.json"):
        try:
            os.remove(os.path.join(out_dir, f))
        except FileNotFoundError:
            pass


def _write_plan(out_dir: str, doc: Dict[str, Any], exclusive: bool) -> None:
    path = _plan_path(out_dir)
    if exclusive:                                       # 이미 있으면 FileExistsError - 계획을 덮어쓰지 않는다
        try:
            fh = open(path, "x")
        except FileExistsError:
            raise SystemExit(f"crossover plan already recorded: {path}") from None
        with fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        return
    tmp = f"{path}.tmp"                                 # 축소는 temp 에 쓰고 바꿔치기 - 쓰다 죽어도 계획이 반쯤 남지 않는다
    with open(tmp, "w") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_plan(out_dir: str) -> Tuple[Dict[str, Any], str]:
    """계획 파일을 한 번만 읽어 같은 바이트로 파싱과 sha256 을 함께 낸다(읽는 사이 truncate 가 끼어도 meta 가 어긋나지 않는다)."""
    path = _plan_path(out_dir)
    if not os.path.exists(path):
        raise SystemExit(f"no crossover plan at {path}: record it before the first phase "
                         f"(python -m bench.crossover record-plan {out_dir} --arm ... --set ... --laps N)")
    with open(path, "rb") as fh:
        raw = fh.read()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def load_plan(out_dir: str) -> Dict[str, Any]:
    return _read_plan(out_dir)[0]


def _effective_laps(doc: Dict[str, Any]) -> int:
    truncs = doc.get("truncations") or []
    return truncs[-1]["laps"] if truncs else doc["laps"]


def _check_arm_sidecars(arms: List[str], arm_sidecars: Dict[str, Dict[str, Any]]) -> None:
    """팔 사이드카 지정은 모든 팔에 주거나 아예 없어야 한다 - 일부 팔만 주면 나머지 팔의 세트가 앞 단계 포트에 그대로 남는다."""
    if sorted(arm_sidecars) != sorted(arms):
        raise SystemExit(f"arm sidecars must cover exactly the planned arms {arms}, got {sorted(arm_sidecars)}")
    for arm, spec in arm_sidecars.items():
        if not isinstance(spec, dict) or set(spec) != {"port", "batch_size"}:
            raise SystemExit(f"arm {arm}: sidecar needs exactly port and batch_size, got {spec!r}")
        check_arm_sidecar(spec["port"], spec["batch_size"])


def record_plan(out_dir: str, arms: List[str], sets: List[str], laps: int, now: Optional[str] = None,
                arm_sidecars: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """첫 단계 전에 계획을 남긴다. 바퀴 = 모든 팔이 모든 세트를 정확히 한 번씩 거치는 연속 단계 n개(n = 팔 수).
    팔은 객체로 둔다 - 팔마다 붙는 설정을 같은 항목에 덧붙인다: arm_sidecars = 팔 -> {"port", "batch_size"}
    (세트가 붙을 벤치 사이드카 포트와 그 포트의 기대 배치, issue #83). 없으면 팔 항목은 {"name"} 뿐이다."""
    plan(arms, sets, 0)                                 # 팔·세트 수와 중복 검사
    if not isinstance(laps, int) or laps < 1:
        raise SystemExit(f"crossover plan needs at least 1 lap, got {laps!r}")
    if arm_sidecars:
        _check_arm_sidecars(arms, arm_sidecars)
    if _merge_started(out_dir):
        raise SystemExit(f"{out_dir} already has merge outputs - a plan must be recorded before the first phase")
    os.makedirs(out_dir, exist_ok=True)
    doc = {"version": PLAN_VERSION,
           "arms": [{"name": a, **({"sidecar": dict(arm_sidecars[a])} if arm_sidecars else {})} for a in arms],
           "sets": list(sets), "laps": laps,
           "planned_phases": laps * len(arms), "recorded_at": now or _now(), "truncations": []}
    _write_plan(out_dir, doc, exclusive=True)
    return doc


def truncate_plan(out_dir: str, laps: int, reason: str, now: Optional[str] = None) -> Dict[str, Any]:
    """계획을 줄이는 유일한 길: 새 목표 바퀴 수와 사유·시각을 계획 파일에 덧붙인다(처음 계획은 지우지 않는다).
    병합이 시작된 뒤에는 거부한다 - 결과를 본 뒤의 축소는 사유로 인정하지 않는다."""
    doc = load_plan(out_dir)
    if _merge_started(out_dir):
        raise SystemExit(f"{out_dir} already has merge outputs - the plan can no longer be truncated")
    if not reason or not reason.strip():
        raise SystemExit("truncating a crossover plan needs a reason")
    cur = _effective_laps(doc)
    if not isinstance(laps, int) or not 1 <= laps < cur:
        raise SystemExit(f"truncate needs 1 <= laps < current plan ({cur} laps), got {laps!r}")
    doc["truncations"] = list(doc.get("truncations") or []) + [
        {"at": now or _now(), "reason": reason.strip(), "laps": laps, "planned_phases": laps * len(doc["arms"])}]
    _write_plan(out_dir, doc, exclusive=False)
    return doc


def _check_plan(doc: Dict[str, Any], arms: List[str], sets: List[str], metas: List[Dict[str, Any]]) -> None:
    """병합 인자가 계획과 같은 팔·세트(같은 순서 - 배정이 순서로 정해진다)이고, 계획이 모든 단계보다 먼저 기록됐는가."""
    if [a["name"] for a in doc["arms"]] != arms or doc["sets"] != sets:
        raise SystemExit(f"merge arms/sets {arms}/{sets} differ from the recorded plan "
                         f"{[a['name'] for a in doc['arms']]}/{doc['sets']}")
    def utc(s: str) -> dt.datetime:                     # 시간대 없는 값은 UTC 로 본다 - 비교가 TypeError 로 죽거나 검사를 건너뛰지 않게
        t = dt.datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    planned = _effective_laps(doc) * len(doc["arms"])
    if len(metas) > planned:      # 계획 초과 = 결과를 보고 더 돌린 선택적 연장과 구분할 수 없다. 더 돌리려면 계획을 새로 기록한다
        raise SystemExit(f"merge has {len(metas)} phases but the plan allows {planned} - "
                         f"running past the plan cannot be told from extending after seeing results")
    recorded = utc(doc["recorded_at"])
    for k, m in enumerate(metas):
        if m.get("started") and utc(m["started"]) < recorded:
            raise SystemExit(f"phase {k} started {m['started']} before the plan was recorded {doc['recorded_at']}")


def _check_phase(k: int, meta: Dict[str, Any], arms: List[str], sets: List[str],
                 configs: Dict[str, Dict[str, str]],
                 arm_sidecars: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    """단계 k 가 계획대로 돌았는가: 1 라운드, 팔 라벨 = 계획한 세트, 그 세트의 upstream 설정 = 팔의 설정,
    (계획에 팔 사이드카 지정이 있으면) 그 세트가 가리킨 벤치 사이드카 포트·배치 = 팔의 계획값."""
    if meta.get("rounds") != 1:
        raise SystemExit(f"phase {k}: crossover phases run --rounds 1 (got {meta.get('rounds')})")
    if sorted(meta.get("arms") or {}) != sorted(arms):
        raise SystemExit(f"phase {k}: arms {sorted(meta.get('arms') or {})} != {sorted(arms)}")
    prov = meta.get("provenance") or {}
    for arm, want_set in plan(arms, sets, k).items():
        got_set = ((prov.get("arms") or {}).get(arm) or {}).get("set")
        if got_set != want_set:
            raise SystemExit(f"phase {k}: arm {arm} ran on set {got_set!r}, plan says {want_set!r}")
        hs_env = ((prov.get("replicas") or {}).get(want_set) or {}).get("hs_env")
        if (hs_env or {}) != configs[arm]:
            raise SystemExit(f"phase {k}: set {want_set} had hs_env {hs_env}, arm {arm} needs {configs[arm]}")
        if arm_sidecars:      # runner 가 run 시작 때 실은 세트 state: reconf 가 쓴 sidecar_port·sidecar_batch_size
            rep = (prov.get("replicas") or {}).get(want_set) or {}
            want = arm_sidecars[arm]
            if (rep.get("sidecar_port"), rep.get("sidecar_batch_size")) != (want["port"], want["batch_size"]):
                raise SystemExit(f"phase {k}: set {want_set} pointed at sidecar port {rep.get('sidecar_port')!r} "
                                 f"batch {rep.get('sidecar_batch_size')!r}, arm {arm} needs port {want['port']} "
                                 f"batch {want['batch_size']}")
    # 팔 차이는 upstream 설정뿐이어야 한다 - 세트별 게이트웨이 코드·설정과 벤치 사이드카가 같은지 확인한다
    gw_ids = {json.dumps({f: ((prov.get("arms") or {}).get(arm) or {}).get(f)
                          for f in ("commit", "patch_sha256", "env_override_values")}, sort_keys=True) for arm in arms}
    if len(gw_ids) != 1:
        raise SystemExit(f"phase {k}: set gateways differ in code/config: {sorted(gw_ids)}")
    # 로컬 정책(issue #225): 모든 팔에 없으면(#217 이전 prov) 통과, 일부에만 있으면 확인 불가, 모두 있으면 files(파일명 -> sha256)만 비교한다.
    # dir·source 는 워크트리마다 달라 비교하지 않는다. 메시지에는 해시·경로를 싣지 않는다.
    lp = {arm: ((prov.get("arms") or {}).get(arm) or {}).get("local_policies") for arm in arms}
    have = sorted(arm for arm in arms if lp[arm] is not None)
    if have and len(have) != len(arms):
        raise SystemExit(f"phase {k}: local_policies recorded for arms {have} but not for {sorted(set(arms) - set(have))}; "
                         f"cannot confirm the arms ran the same local policies")
    if have:      # files 가 맵이 아니면(없음·null·기타) 해시를 비교할 수 없다 - 같다고 치지 않는다. 빈 맵 {} 은 맵이라 빈 맵끼리 같다
        bad = [arm for arm in have if not isinstance(lp[arm], dict) or not isinstance(lp[arm].get("files"), dict)]
        if bad:
            raise SystemExit(f"phase {k}: local_policies.files is missing or not a map for arms {bad}; "
                             f"cannot confirm the arms ran the same local policies")
        if len({json.dumps(lp[arm]["files"], sort_keys=True) for arm in have}) != 1:
            raise SystemExit(f"phase {k}: arms ran different local_policies.files (per-file sha256 maps differ)")
    sidecars = {json.dumps(((prov.get("replicas") or {}).get(s) or {}).get("sidecar"), sort_keys=True)
                for s in plan(arms, sets, k).values()}
    if len(sidecars) != 1:
        raise SystemExit(f"phase {k}: sets use different bench sidecar overrides: {sorted(sidecars)}")
    if not arm_sidecars:      # 팔 사이드카 지정이 없으면 모든 세트가 같은 포트·배치였어야 한다 - 한 사이드카의 주 포트와 보조 포트는 overrides 가 같아 위 검사로 못 가른다
        pb = {((prov.get("replicas") or {}).get(s) or {}).get("sidecar_port", None) for s in plan(arms, sets, k).values()}
        bs = {((prov.get("replicas") or {}).get(s) or {}).get("sidecar_batch_size", None) for s in plan(arms, sets, k).values()}
        if len(pb) != 1 or len(bs) != 1:
            raise SystemExit(f"phase {k}: sets pointed at different sidecar ports {sorted(map(str, pb))} / batches "
                             f"{sorted(map(str, bs))} but the plan has no arm sidecars")
    images = {((prov.get("replicas") or {}).get(s) or {}).get("image") for s in plan(arms, sets, k).values()}
    if len(images) != 1:        # 운영 재배포 앞뒤로 올린 세트는 서로 다른 upstream 이다
        raise SystemExit(f"phase {k}: sets run different upstream images: {sorted(map(str, images))}")


KEEPWARM_FIELDS = ("started", "stopped", "sent", "failed", "failures", "exclude_line")


def _unmerged_keepwarm(phase_dirs: List[str]) -> List[Dict[str, Any]]:
    """단계 디렉터리 옆(같은 runs 디렉터리)에 meta.json 없이 keepwarm.json 만 남은 시도. 크래시한 단계 시도는 이미 운영 사이드카에 keep-warm 을 보냈는데
    병합 입력에 없으므로 exclude_lines 에서 빠진다 - 제외 줄을 따로 싣고 사람이 확인해 붙이게 한다(exclude_lines 에는 넣지 않는다: 같은 디렉터리의 무관한 run 일 수 있다)."""
    merged = {os.path.realpath(d) for d in phase_dirs}
    out = []
    for parent in sorted({os.path.dirname(os.path.realpath(d)) for d in phase_dirs}):
        for name in sorted(os.listdir(parent)):
            d = os.path.join(parent, name)
            if d in merged or os.path.exists(os.path.join(d, "meta.json")):
                continue
            try:
                with open(os.path.join(d, "keepwarm.json")) as fh:
                    kw = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(kw, dict) and kw.get("exclude_line"):
                out.append({"run_id": name, "exclude_line": kw["exclude_line"]})
    return out


def _merged_keepwarm(metas: List[Dict[str, Any]], phase_dirs: List[str]) -> Dict[str, Any]:
    """단계 meta 의 keep-warm 기록을 단계 순서대로 옮긴다. 기록이 없거나 꺼진(null) 과거 단계는 값이 null 이고 병합을 막지 않는다."""
    phases = []
    for k, m in enumerate(metas):
        kw = m.get("keepwarm") if isinstance(m.get("keepwarm"), dict) else {}
        phases.append({"phase": k, "run_id": m.get("run_id"), **{f: kw.get(f) for f in KEEPWARM_FIELDS}})
    return {"phases": phases, "exclude_lines": [p["exclude_line"] for p in phases if p["exclude_line"]],
            "unmerged": _unmerged_keepwarm(phase_dirs)}


def merge(phase_dirs: List[str], out_dir: str, arms: List[str], sets: List[str],
          configs: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """단계 run 들을 라운드 = 단계인 교차 run 하나로 합친다. 판정기는 meta.crossover 로 교차 모드를 쓴다.
    out_dir 에 record-plan 이 쓴 계획 파일이 있어야 한다(없으면 거부) - meta.plan 에 계획·실제 단계 수와 축소 기록을 싣는다.
    실제 단계가 유효 계획(마지막 축소 목표, 없으면 처음 계획)보다 많아도 거부한다. 도중에 실패하면 이번 병합의 산출물만 지우고 계획은 남긴다.
    warm 표본을 늘리려면 순환을 여러 바퀴 돈다(단계 k = 0..n*L-1, 배정은 k mod n). 바퀴가 덜 끝나면 어떤 팔이
    한 세트를 더 많이 거쳐 그 세트의 오프셋이 평균에 남으므로 거부한다."""
    if not phase_dirs or len(phase_dirs) % len(arms):
        raise SystemExit(f"crossover needs whole laps of {len(arms)} phases (one per set), got {len(phase_dirs)}")
    if len({os.path.realpath(d) for d in phase_dirs}) != len(phase_dirs):   # plan(k) == plan(k+n) 라 같은 단계를
        raise SystemExit(f"phase dirs repeat: {phase_dirs}")                 # 두 번 넣어도 배정 검사를 통과한다
    metas = [json.load(open(os.path.join(d, "meta.json"))) for d in phase_dirs]
    if len({m.get("run_id") for m in metas}) != len(metas):
        raise SystemExit(f"phase run_ids repeat: {[m.get('run_id') for m in metas]}")
    plan_doc, plan_sha = _read_plan(out_dir)             # 계획 없는 병합은 거부 - 아무것도 쓰기 전에
    _check_plan(plan_doc, arms, sets, metas)
    arm_sidecars = {a["name"]: a["sidecar"] for a in plan_doc["arms"] if a.get("sidecar")} or None    # 기록 시점 값 - 인자로 못 바꾼다
    if arm_sidecars:
        _check_arm_sidecars(arms, arm_sidecars)          # 손으로 고친 계획(일부 팔만, 운영 포트)은 KeyError 가 아니라 이유와 함께 거부
    for k, m in enumerate(metas):
        _check_phase(k, m, arms, sets, configs, arm_sidecars)
    for key in SAME_ACROSS_PHASES:
        vals = {json.dumps(m.get(key)) for m in metas}
        if len(vals) != 1:
            raise SystemExit(f"phases disagree on {key}: {sorted(vals)}")
    os.makedirs(os.path.join(out_dir, "results"), exist_ok=False)
    try:                                                    # 도중에 죽으면 이번 병합이 만든 산출물만 지운다(계획 파일은 남긴다)
        with open(os.path.join(out_dir, "calls.jsonl"), "w") as out, \
                open(os.path.join(out_dir, "prod_events.jsonl"), "w") as ev:
            for k, d in enumerate(phase_dirs):
                for line in open(os.path.join(d, "calls.jsonl")):
                    if line.strip():
                        rec = json.loads(line)
                        rec["round"] = k                            # 라운드 = 교차 단계
                        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                pe = os.path.join(d, "prod_events.jsonl")
                if os.path.exists(pe):
                    shutil.copyfileobj(open(pe), ev)
                for f in os.listdir(os.path.join(d, "results")):
                    dst = os.path.join(out_dir, "results", f)
                    if not os.path.exists(dst):                     # 이름 = 팔.키.digest - 같은 이름이면 같은 내용
                        shutil.copy2(os.path.join(d, "results", f), dst)
        # 동결: 모든 단계의 전후 지문이 첫 단계 시작과 같아야 한다. 하나라도 다르면 after 에 그 값을 실어 판정기가 drift 로 본다
        fz0 = metas[0].get("freeze_before")
        drift: Optional[Any] = next((f for m in metas for f in (m.get("freeze_before"), m.get("freeze_after"))
                                     if f != fz0), None)
        # rewrite 실패: 팔 라벨마다 단계별 증가분 합(단계마다 다른 게이트웨이 컨테이너). 한 단계라도 카운터가 줄었으면
        # (단계 도중 재시작) 그 단계의 실패 수를 알 수 없다 - 다른 단계의 실제 실패와 합이 0 으로 상쇄되지 않게 None
        rf_after: Dict[str, Optional[int]] = {}
        for arm in arms:
            total: Optional[int] = 0
            for m in metas:
                b, a = (m.get("rewrite_failures_before") or {}).get(arm), (m.get("rewrite_failures_after") or {}).get(arm)
                total = None if total is None or a is None or b is None or a < b else total + (a - b)
            rf_after[arm] = total
        pe_counts = [m.get("prod_events") for m in metas]
        # 간섭 가드(issue #19): 단계마다의 정지 구간을 모두 싣고, 한 단계라도 중단됐으면 병합 run 도 중단으로 본다
        guards = [m.get("guard") or {} for m in metas]
        guard = {"limits": guards[0].get("limits"), "windows": [w for g in guards for w in g.get("windows") or []],
                 "pauses": [p for g in guards for p in g.get("pauses") or []],
                 "aborted": next((g["aborted"] for g in guards if g.get("aborted")), None)}
        sc = [m.get("prod_sidecar") for m in metas]
        prod_sidecar = None if any(c is None for c in sc) else \
            {k: sum(c.get(k, 0) for c in sc) for k in sorted({k for c in sc for k in c})}
        skw = [m.get("prod_sidecar_keepwarm") for m in metas]      # prod_sidecar 합에는 keep-warm 자신의 실패가 들어 있다 - 같은 방식으로 합쳐 뺄 수 있게 한다
        prod_sidecar_keepwarm = None if all(c is None for c in skw) else \
            {k: sum((c or {}).get(k, 0) for c in skw) for k in ("http_503", "deadline", "queue_full")}      # None = keep-warm 꺼진 단계(옛 meta 포함)는 0 으로 센다
        meta = {**{k: metas[0].get(k) for k in SAME_ACROSS_PHASES}, "run_id": os.path.basename(out_dir.rstrip("/")),
                "arms": metas[0]["arms"], "rounds": len(metas), "crossover": True,
                "carryover_balanced": all(m.get("carryover_balanced", True) for m in metas),
                "n_queries": metas[0].get("n_queries"),
                "freeze_before": fz0, "freeze_after": fz0 if drift is None else drift,
                "rewrite_failures_before": {a: 0 for a in arms}, "rewrite_failures_after": rf_after,
                "prod_events": None if any(c is None for c in pe_counts) else sum(pe_counts),
                "guard": guard, "prod_sidecar": prod_sidecar, "prod_sidecar_keepwarm": prod_sidecar_keepwarm,
                "phases": [{"run_id": m.get("run_id"), "plan": plan(arms, sets, k), "provenance": m.get("provenance"),
                            "started": m.get("started"), "finished": m.get("finished")} for k, m in enumerate(metas)],
                "configs": configs, "keepwarm": _merged_keepwarm(metas, phase_dirs),
                "plan": {**plan_doc, "actual_phases": len(metas), "plan_file_sha256": plan_sha}}
        with open(os.path.join(out_dir, "meta.json"), "w") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
    except BaseException:
        _remove_merge_outputs(out_dir)
        raise
    return meta


def _set_state(replica: Any, set_name: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(replica.STATE_DIR, replica.instance_names(set_name)["state"])) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def run_phase(k: int, arms: List[str], sets: List[str], gw_ports: Dict[str, int],
              configs: Dict[str, Dict[str, str]], split: str, limit: Optional[int], run_id: str,
              note: str = "", seed: int = 13, arm_sidecars: Optional[Dict[str, Dict[str, Any]]] = None,
              keepwarm: Optional[KeepWarm] = None) -> str:
    """교차 단계 k 하나: 세트마다 계획한 팔의 설정으로 reconf(전부 재기동 - 단계마다 같은 조건) -> verify 전부 pass ->
    runner 1 라운드. 팔 라벨은 설정 이름이고 포트는 그 단계에 그 설정을 가진 세트의 게이트웨이다.
    arm_sidecars(계획 파일의 팔 사이드카 지정)가 있으면 세트를 그 팔의 벤치 사이드카 포트로 붙이고 기대 배치를 기록한다 -
    verify 가 세트가 붙은 포트의 사이드카 info 배치와 대조한다(issue #83).
    keepwarm 을 주면 runner.run 이 단계가 도는 동안만 운영 사이드카에 keep-warm 을 보낸다(issue #90). 기본은 꺼짐 - CLI(main)만 실제 송신기를 만든다."""
    from bench import replica, runner                       # docker·tiktoken 을 쓰는 모듈 - 실행할 때만
    assign = plan(arms, sets, k)
    if not arm_sidecars:      # 세트가 기대 배치를 들고 있는데(up/reconf 로 지정) 계획의 팔 지정 없이 돌리면 단계 전체를 앞 포트로 재고 병합에서야 거부된다
        carrying = [s for s in sets if "sidecar_batch_size" in _set_state(replica, s)]
        if carrying:
            raise SystemExit(f"phase {k}: sets {carrying} carry an expected sidecar batch size but no arm sidecars were "
                             f"given - pass --plan-dir <runs>/<id> (or `up` the sets again without --sidecar-batch-size)")
    for arm, set_name in assign.items():
        if arm_sidecars:
            replica.reconf(set_name, configs[arm], sidecar_port=arm_sidecars[arm]["port"],
                           sidecar_batch_size=arm_sidecars[arm]["batch_size"])
        else:
            replica.reconf(set_name, configs[arm])
    for set_name in sets:
        rep = replica.verify(set_name)
        if not rep.get("pass"):
            raise SystemExit(f"phase {k}: set {set_name} failed verify: env={rep.get('env')} "
                             f"wrong={rep.get('env_wrong_override')} sidecar={rep.get('sidecar')} "
                             f"sidecar_batch={rep.get('sidecar_batch')}")
    # 벤치 시계는 세트 up 때 고정한 값을 그대로 쓴다(reconf 는 데이터를 바꾸지 않는다) - runner 가 세트 간 일치를 확인
    return runner.run({arm: gw_ports[assign[arm]] for arm in arms}, split, 1, 30, limit, seed, run_id,
                      runner.read_token(), note=f"{note} [crossover phase {k}: {assign}]", keepwarm=keepwarm)


def parse_arm_sidecar(flag: str) -> Tuple[str, Dict[str, int]]:
    """`arm=port:batch` -> (arm, {"port", "batch_size"}). 검증(운영 포트 등)은 record_plan 이 한다."""
    try:
        arm, rest = flag.split("=", 1)
        port, batch = rest.split(":", 1)
        if not arm:
            raise ValueError
        return arm, {"port": int(port), "batch_size": int(batch)}
    except ValueError:
        raise SystemExit(f"--arm-sidecar must be arm=port:batch, got {flag!r}") from None


def plan_arm_sidecars(plan_dir: str, arms: List[str], sets: List[str]) -> Optional[Dict[str, Dict[str, Any]]]:
    """계획 파일의 팔 사이드카 지정(없으면 None). 인자의 팔·세트가 계획과 다르면 거부 - 다른 계획의 포트로 단계를 돌리지 않는다."""
    doc = load_plan(plan_dir)
    if [a["name"] for a in doc["arms"]] != arms or doc["sets"] != sets:
        raise SystemExit(f"run-phase arms/sets {arms}/{sets} differ from the recorded plan "
                         f"{[a['name'] for a in doc['arms']]}/{doc['sets']}")
    specs = {a["name"]: a["sidecar"] for a in doc["arms"] if a.get("sidecar")} or None
    if specs:
        _check_arm_sidecars(arms, specs)
    return specs


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="순환 교차: 계획 기록·축소 / 단계 배정 출력 / 단계 run 병합")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record-plan", help="첫 단계 전에 계획(팔, 세트, 바퀴 수)을 <out_dir>/crossover-plan.json 에 기록")
    rec.add_argument("out_dir", help="병합 대상 run 디렉터리(<runs>/<id>)")
    rec.add_argument("--arm", action="append", required=True)
    rec.add_argument("--set", action="append", required=True)
    rec.add_argument("--laps", type=int, required=True, help="바퀴 수(바퀴 = 팔 수만큼의 연속 단계)")
    rec.add_argument("--arm-sidecar", action="append", default=[], metavar="ARM=PORT:BATCH",
                     help="팔이 쓸 벤치 사이드카 포트(주 또는 보조)와 기대 배치. 주려면 모든 팔에 준다(issue #83)")
    tr = sub.add_parser("truncate", help="계획 축소: 새 목표 바퀴 수와 사유·시각을 덧붙인다(병합 뒤에는 거부)")
    tr.add_argument("out_dir")
    tr.add_argument("--laps", type=int, required=True, help="줄인 뒤 목표 바퀴 수")
    tr.add_argument("--reason", required=True)
    rp = sub.add_parser("run-phase")
    rp.add_argument("--arm", action="append", required=True)
    rp.add_argument("--set", action="append", required=True)
    rp.add_argument("--gw-port", action="append", required=True, help="set=port - 세트에 붙은 게이트웨이")
    rp.add_argument("--configs", required=True)
    rp.add_argument("--phase", type=int, required=True)
    rp.add_argument("--split", choices=("dev", "holdout"), default="dev")
    rp.add_argument("--limit", type=int)
    rp.add_argument("--run-id", required=True)
    rp.add_argument("--note", default="")
    rp.add_argument("--plan-dir", help="record-plan 을 쓴 run 디렉터리 - 계획의 팔 사이드카 지정으로 세트를 붙인다(지정이 있는 계획이면 반드시)")
    for name in ("plan", "merge"):
        p = sub.add_parser(name)
        p.add_argument("--arm", action="append", required=True)
        p.add_argument("--set", action="append", required=True)
        if name == "plan":
            p.add_argument("--phase", type=int, required=True)
        else:
            p.add_argument("out_dir")
            p.add_argument("--phase-dir", action="append", required=True)
            p.add_argument("--configs", required=True, help='JSON {"arm": {"K": "V"}} - 팔마다 upstream 설정')
    a = ap.parse_args(argv)
    if a.cmd == "run-phase":
        configs = json.loads(a.configs)
        sidecars = plan_arm_sidecars(a.plan_dir, a.arm, a.set) if a.plan_dir else None
        out: Any = {"run_dir": run_phase(a.phase, a.arm, a.set,
                                         {kv.split("=")[0]: int(kv.split("=")[1]) for kv in a.gw_port},
                                         {arm: configs.get(arm, {}) for arm in a.arm}, a.split, a.limit,
                                         a.run_id, a.note, arm_sidecars=sidecars,
                                         keepwarm=KeepWarm.production())}
    elif a.cmd == "record-plan":
        parsed = [parse_arm_sidecar(f) for f in a.arm_sidecar]
        dup = sorted({n for n, _ in parsed if [m for m, _ in parsed].count(n) > 1})
        if dup:       # dict() 로 만들면 같은 팔을 두 번 준 앞 값이 조용히 사라진다 - 계획 파일은 한 번만 쓰이므로 거부
            raise SystemExit(f"--arm-sidecar given more than once for arm {dup}")
        out = record_plan(a.out_dir, a.arm, a.set, a.laps, arm_sidecars=dict(parsed) or None)
    elif a.cmd == "truncate":
        out = truncate_plan(a.out_dir, a.laps, a.reason)
    elif a.cmd == "plan":
        out = plan(a.arm, a.set, a.phase)
    else:
        configs = json.loads(a.configs)
        out = merge(a.phase_dir, a.out_dir, a.arm, a.set, {arm: configs.get(arm, {}) for arm in a.arm})
        n_unmerged = len(out["keepwarm"]["unmerged"])
        if n_unmerged:      # 크래시한 시도의 keep-warm 구간은 exclude_lines 에 없다 - 사람이 meta.keepwarm.unmerged 의 줄을 확인해 붙인다
            print(f"warning: {n_unmerged} unmerged keep-warm window(s) (병합되지 않은 keep-warm 구간 {n_unmerged}개): "
                  f"see meta.keepwarm.unmerged", file=sys.stderr)
        out = {k: out[k] for k in ("run_id", "rounds", "crossover", "freeze_after", "prod_events", "plan", "keepwarm")} | \
            {"drift": out["freeze_after"] != out["freeze_before"]}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
