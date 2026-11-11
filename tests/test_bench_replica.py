"""issue #13 복제본 hindsight env 합성 - 실험용 upstream 설정(--hs-env)은 얹되 동결 보장은 깨지 않는다."""
import os
import shutil

import pytest

from bench import replica as R

PROD = {"HINDSIGHT_API_RERANKER_MAX_CANDIDATES": "300", "HINDSIGHT_API_WORKER_ENABLED": "true",
        "HINDSIGHT_API_LLM_MODEL": "grok"}


def test_user_env_is_applied_on_top_of_freeze_overrides():
    env = R.hs_env(PROD, {"HINDSIGHT_API_RERANKER_MAX_CANDIDATES": "600"}, key="k")
    assert env["HINDSIGHT_API_RERANKER_MAX_CANDIDATES"] == "600"
    assert env["HINDSIGHT_API_WORKER_ENABLED"] == "false"          # 동결 override 는 그대로
    assert "HINDSIGHT_API_LLM_MODEL" not in env


def test_user_env_cannot_override_freeze_keys():
    with pytest.raises(SystemExit):
        R.hs_env(PROD, {"HINDSIGHT_API_WORKER_ENABLED": "true"}, key="k")


# ---------------------------------------------------------------- 세트(복제본 인스턴스) - issue #13 B

def test_default_set_keeps_legacy_names():
    n = R.instance_names("")
    assert (n["hs"], n["pg"], n["alias"], n["state"]) == (
        "hc-bench-hindsight", "hc-bench-pg0", "hindsight", "replica.json")


def test_named_set_gets_its_own_container_volume_alias_and_state():
    n = R.instance_names("cand")
    assert (n["hs"], n["pg"], n["alias"], n["state"]) == (
        "hc-bench-hindsight-cand", "hc-bench-pg0-cand", "hindsight-cand", "replica-cand.json")


@pytest.mark.parametrize("bad", ["../x", "a b", "A_B", "x" * 21])
def test_set_name_is_restricted(bad):
    with pytest.raises(SystemExit):
        R.instance_names(bad)


TEI = "HINDSIGHT_API_RERANKER_TEI_URL"


def test_set_replica_points_at_its_bench_sidecar():
    prod = {**PROD, TEI: "http://host.lima.internal:8787"}
    env = R.hs_env(prod, None, key="k", tei_url="http://host.lima.internal:18788")
    assert env[TEI] == "http://host.lima.internal:18788"
    assert R.hs_env(prod, None, key="k")[TEI] == "http://host.lima.internal:8787"     # 기본 세트는 운영 사이드카


def test_user_env_cannot_redirect_the_reranker():
    with pytest.raises(SystemExit):
        R.hs_env(PROD, {TEI: "http://elsewhere:1"}, key="k")


def test_gateway_arm_targets_its_set_replica():
    prod_gw = {"HC_UPSTREAM_BASE": "http://hindsight:8888", "HC_RECALL_PRUNE": "1", "HC_ACL_TOKEN": "x",
               "PATH": "/usr/bin"}
    env = R.compose_gateway_env(prod_gw, None, key="k", upstream_alias="hindsight-cand")
    assert env["HC_UPSTREAM_BASE"] == "http://hindsight-cand:8888"
    assert env["HC_RECALL_PRUNE"] == "1" and "HC_ACL_TOKEN" not in env and "PATH" not in env
    assert R.compose_gateway_env(prod_gw, None, key="k")["HC_UPSTREAM_BASE"] == "http://hindsight:8888"
    with pytest.raises(SystemExit):                                  # 팔 env 로 upstream 을 돌릴 수 없다
        R.compose_gateway_env(prod_gw, {"HC_UPSTREAM_BASE": "http://x:1"}, key="k")


# ---------------------------------------------------------------- 벤치 사이드카 - issue #13 B

PLIST_ENV = {"RERANK_PORT": "8787", "RERANK_BIND": "127.0.0.1", "RERANK_BATCH_SIZE": "32",
             "RERANK_MAX_TEXTS": "256", "RERANK_MODEL_REVISION": "abc", "HF_HUB_OFFLINE": "1"}
PLIST_ARGV = ["/opt/homebrew/bin/uv", "run", "--project", "/r", "uvicorn", "server:app",
              "--host", "127.0.0.1", "--port", "8787"]


def test_sidecar_clones_production_config_with_overrides_on_its_own_port():
    env, argv = R.sidecar_launch(PLIST_ENV, PLIST_ARGV, {"RERANK_BATCH_SIZE": "16"}, port=18788)
    assert env["RERANK_BATCH_SIZE"] == "16" and env["RERANK_MAX_TEXTS"] == "256"
    assert env["RERANK_MODEL_REVISION"] == "abc" and env["RERANK_PORT"] == "18788"
    assert argv[-2:] == ["--port", "18788"] and [a for a in argv[:-1] if a != "--no-sync"] == PLIST_ARGV[:-1]


@pytest.mark.parametrize("port,over", [
    (8787, {}),                                   # 운영 사이드카 포트
    (18788, {"RERANK_PORT": "8787"}),             # override 로 포트를 돌릴 수 없다
    (18788, {"RERANK_BIND": "0.0.0.0"}),          # 외부 노출 금지
    (80, {}),                                     # 벤치 포트 대역 밖
])
def test_sidecar_refuses_production_port_and_exposure(port, over):
    with pytest.raises(SystemExit):
        R.sidecar_launch(PLIST_ENV, PLIST_ARGV, over, port=port)


def test_set_attaches_to_an_existing_bench_sidecar_by_port():
    states = [{"name": "shared", "port": 18781, "overrides": {"RERANK_MAX_TEXTS": "300"}},
              {"name": "other", "port": 18785, "overrides": {}}]
    assert R.sidecar_for_port(states, 18781)["name"] == "shared"
    with pytest.raises(SystemExit):
        R.sidecar_for_port(states, 18782)              # 떠 있지 않은 사이드카
    with pytest.raises(SystemExit):
        R.sidecar_for_port(states, 8787)               # 운영 사이드카에 세트를 붙일 수 없다(기본 세트만 공유)


# ---------------------------------------------------------------- 하네스 결함 수정 (issue #13 티켓 2)

def test_bench_sidecar_never_resyncs_the_shared_venv():
    """벤치 사이드카가 운영과 같은 .venv 를 쓰되 uv 가 그 환경을 다시 동기화하지 않는다(운영이 다음 재기동 때 바뀐 환경을 쓰게 된다)."""
    _, argv = R.sidecar_launch(PLIST_ENV, PLIST_ARGV, {}, port=18788)
    assert argv[:3] == ["/opt/homebrew/bin/uv", "run", "--no-sync"]
    assert argv[3:-1] == PLIST_ARGV[2:-1] and argv[-1] == "18788"


def test_hs_env_rejects_non_string_values():
    """교차 --configs 는 JSON 이라 숫자가 올 수 있다 - 컨테이너를 지우기 전 단계에서 거부."""
    with pytest.raises(SystemExit):
        R.hs_env(PROD, {"HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE": 300}, key="k")


def _stub_docker(monkeypatch, calls, volume_exists=True, image="sha256:prod"):
    import subprocess as sp
    monkeypatch.setattr(R, "sh", lambda *a, **k: calls.append(a) or "")
    monkeypatch.setattr(R.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess(a, 0 if volume_exists else 1, b"", b""))
    monkeypatch.setattr(R, "inspect", lambda name: {"Image": image, "State": {"StartedAt": "t", "Running": True}})
    monkeypatch.setattr(R, "runtime_env", lambda name: dict(PROD))
    monkeypatch.setattr(R, "bench_key", lambda: "k")
    monkeypatch.setattr(R, "_start_replica", lambda *a, **k: calls.append(("start",) + a[1:]))


def test_default_set_up_refuses_while_named_sets_or_sidecars_are_up(tmp_path, monkeypatch):
    """기본 세트 up 의 teardown 이 교차 캠페인의 세트·공유 사이드카를 조용히 지우지 않는다."""
    calls: list = []
    _stub_docker(monkeypatch, calls)
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(R, "exists", lambda name: False)
    monkeypatch.setattr(R, "mem_available_mb", lambda: 10**6)
    monkeypatch.setattr(R, "latest_backup", lambda: "/nonexistent.tgz")
    monkeypatch.setattr(R, "hs_down", lambda: calls.append(("hs_down",)))
    (tmp_path / "replica-s1.json").write_text("{}")
    with pytest.raises(SystemExit):
        R.hs_up()
    assert ("hs_down",) not in calls and not any(c[:2] == ("docker", "rm") for c in calls)


def test_reconf_refuses_when_production_image_moved_since_up(tmp_path, monkeypatch):
    """단계 사이 운영 재배포 - 뒤 단계가 다른 upstream 으로 측정되지 않게 컨테이너를 지우기 전에 멈춘다."""
    calls: list = []
    _stub_docker(monkeypatch, calls, image="sha256:new")
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    (tmp_path / "replica-s1.json").write_text('{"image": "sha256:old", "sidecar_port": 18781}')
    with pytest.raises(SystemExit):
        R.reconf("s1", {"HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE": "300"})
    assert not any(c[:2] == ("docker", "rm") for c in calls) and not any(c[0] == "start" for c in calls)


def test_port_claimed_by_two_sidecar_states_is_refused():
    states = [{"name": "dead", "port": 18781, "overrides": {}}, {"name": "live", "port": 18781, "overrides": {}}]
    with pytest.raises(SystemExit):
        R.sidecar_for_port(states, 18781)


def test_sidecar_up_refuses_a_port_still_claimed_by_another_state(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(R, "PROD_SIDECAR_PLIST", str(tmp_path / "missing.plist"))   # 검사가 없으면 FileNotFoundError
    (tmp_path / "sidecar-dead.json").write_text('{"name": "dead", "port": 18781, "overrides": {}}')
    with pytest.raises(SystemExit):
        R.sidecar_up("live", 18781, {})


@pytest.mark.parametrize("states,ok", [
    ([{"name": "shared", "port": 18781, "overrides": {"RERANK_MAX_TEXTS": "300"}}], True),
    ([{"name": "shared", "port": 18781, "overrides": {"RERANK_BATCH_SIZE": "16"}}], False),   # 같은 이름, 다른 override
    ([{"name": "shared", "port": 18782, "overrides": {"RERANK_MAX_TEXTS": "300"}}], False),   # 다른 포트로 재기동
    ([{"name": "other", "port": 18781, "overrides": {"RERANK_MAX_TEXTS": "300"}}], False),    # 같은 포트, 다른 사이드카
    ([], False),
])
def test_set_sidecar_must_match_what_up_recorded(states, ok):
    meta = {"sidecar_name": "shared", "sidecar_port": 18781, "sidecar": {"RERANK_MAX_TEXTS": "300"}}
    sc, matches = R.recorded_sidecar(meta, states)
    assert matches is ok


# ---------------------------------------------------------------- B2 팔: 세트별 사이드카 포트·배치 (issue #83)

import json

AUX_OVR = {"RERANK_AUX_PORT": "18782", "RERANK_AUX_BATCH_SIZE": "16"}
SHARED = {"name": "shared", "pid": 1, "port": 18781, "overrides": {"RERANK_MAX_TEXTS": "300", **AUX_OVR}}


def test_sidecar_launch_passes_the_aux_port_and_batch_to_the_bench_sidecar():
    env, _ = R.sidecar_launch(PLIST_ENV, PLIST_ARGV, AUX_OVR, port=18781)
    assert env["RERANK_AUX_PORT"] == "18782" and env["RERANK_AUX_BATCH_SIZE"] == "16"
    assert env["RERANK_PORT"] == "18781"


@pytest.mark.parametrize("over", [
    {"RERANK_AUX_PORT": "8787", "RERANK_AUX_BATCH_SIZE": "16"},        # 운영 사이드카 포트
    {"RERANK_AUX_PORT": "80", "RERANK_AUX_BATCH_SIZE": "16"},          # 벤치 포트 대역 밖
    {"RERANK_AUX_PORT": "18781", "RERANK_AUX_BATCH_SIZE": "16"},       # 주 포트와 같다
    {"RERANK_AUX_PORT": "x", "RERANK_AUX_BATCH_SIZE": "16"},           # 정수가 아니다
    {"RERANK_AUX_PORT": "18782"},                                       # 배치 없이 포트만
    {"RERANK_AUX_BATCH_SIZE": "16"},                                    # 포트 없이 배치만
    {"RERANK_AUX_PORT": "18782", "RERANK_AUX_BATCH_SIZE": "0"},        # 배치 1 미만
    {"RERANK_AUX_PORT": "18782", "RERANK_AUX_BATCH_SIZE": "abc"},
])
def test_sidecar_refuses_a_bad_aux_port_or_batch(over):
    with pytest.raises(SystemExit):
        R.sidecar_launch(PLIST_ENV, PLIST_ARGV, over, port=18781)


def test_set_attaches_to_the_aux_port_of_a_bench_sidecar():
    assert R.sidecar_for_port([SHARED], 18782)["name"] == "shared"       # 주 포트 18781 뿐 아니라 보조 포트도
    assert R.sidecar_for_port([SHARED], 18781)["name"] == "shared"
    with pytest.raises(SystemExit):
        R.sidecar_for_port([SHARED], 18783)
    with pytest.raises(SystemExit):                                       # 보조 포트를 다른 사이드카가 주 포트로 쓰면 모호하다
        R.sidecar_for_port([SHARED, {"name": "o", "port": 18782, "overrides": {}}], 18782)


@pytest.mark.parametrize("port,ok", [(18781, True), (18782, True), (18783, False)])
def test_set_on_an_aux_port_matches_the_sidecar_it_recorded(port, ok):
    meta = {"sidecar_name": "shared", "sidecar_port": port, "sidecar": SHARED["overrides"]}
    assert R.recorded_sidecar(meta, [SHARED])[1] is ok


def test_sidecar_up_refuses_an_aux_port_claimed_by_another_state(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(R, "PROD_SIDECAR_PLIST", str(tmp_path / "missing.plist"))
    (tmp_path / "sidecar-first.json").write_text(json.dumps(SHARED))
    with pytest.raises(SystemExit, match="claimed"):
        R.sidecar_up("second", 18782, {})                  # 다른 사이드카의 보조 포트를 주 포트로
    with pytest.raises(SystemExit, match="claimed"):
        R.sidecar_up("third", 18790, {"RERANK_AUX_PORT": "18782", "RERANK_AUX_BATCH_SIZE": "8"})   # 보조 포트끼리 겹침


def _set_state(tmp_path, **extra):
    meta = {"set": "s1", "image": "sha256:prod", "sidecar_port": 18781, "sidecar_name": "shared",
            "sidecar": dict(SHARED["overrides"]), "hs_env": {}, **extra}
    (tmp_path / "replica-s1.json").write_text(json.dumps(meta))
    (tmp_path / "sidecar-shared.json").write_text(json.dumps(SHARED))
    return meta


def _started_env(calls):
    return next(c for c in calls if c[0] == "start")[1]


@pytest.mark.parametrize("surface", ["up", "reconf"])
@pytest.mark.parametrize("port,batch", [(18781, 32), (18782, 16)])
def test_arm_sidecar_port_reaches_the_set_env_and_the_recorded_state(tmp_path, monkeypatch, surface, port, batch):
    """세트가 가리키는 벤치 사이드카 포트(주 또는 보조)와 기대 배치가 복제본 env 와 세트 state(provenance 출처)에 반영된다."""
    calls: list = []
    _stub_docker(monkeypatch, calls, volume_exists=surface == "reconf")        # up 은 옛 볼륨이 없어야 풀 수 있다
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    _set_state(tmp_path)
    if surface == "reconf":
        R.reconf("s1", {}, sidecar_port=port, sidecar_batch_size=batch)
    else:
        monkeypatch.setattr(R, "exists", lambda name: False)
        monkeypatch.setattr(R, "mem_available_mb", lambda: 10**6)
        monkeypatch.setattr(R, "set_down", lambda s: None)
        monkeypatch.setattr(R, "sha256_file", lambda p: "x")
        monkeypatch.setattr(R, "set_clock", lambda s="": "C")
        R.hs_up("/b/pg0.tar.gz", False, {}, "s1", port, batch)
    assert _started_env(calls)[TEI] == f"http://host.lima.internal:{port}"
    st = json.loads((tmp_path / "replica-s1.json").read_text())
    assert (st["sidecar_port"], st["sidecar_batch_size"], st["sidecar_name"]) == (port, batch, "shared")


def test_reconf_without_an_arm_sidecar_keeps_the_recorded_port_and_writes_no_batch(tmp_path, monkeypatch):
    """지정이 없으면 기존 동작 그대로: up 때 기록한 포트로 붙고 배치 기대값은 생기지 않는다."""
    calls: list = []
    _stub_docker(monkeypatch, calls)
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    _set_state(tmp_path)
    R.reconf("s1", {"HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE": "300"})
    assert _started_env(calls)[TEI] == "http://host.lima.internal:18781"
    st = json.loads((tmp_path / "replica-s1.json").read_text())
    assert st["sidecar_port"] == 18781 and "sidecar_batch_size" not in st


@pytest.mark.parametrize("port,batch", [
    (8787, 32),          # 운영 사이드카
    (18783, 16),         # 떠 있지 않은 사이드카 포트
    (80, 16),            # 벤치 포트 대역 밖
    (18782, 0),          # 배치 1 미만
    (18782, None),       # 포트만(기대 배치 없음)
    (None, 16),          # 배치만(포트 없음)
])
def test_reconf_refuses_a_bad_arm_sidecar_before_touching_the_container(tmp_path, monkeypatch, port, batch):
    calls: list = []
    _stub_docker(monkeypatch, calls)
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    _set_state(tmp_path)
    before = (tmp_path / "replica-s1.json").read_text()
    with pytest.raises(SystemExit):
        R.reconf("s1", {}, sidecar_port=port, sidecar_batch_size=batch)
    assert not any(c[:2] == ("docker", "rm") for c in calls) and not any(c[0] == "start" for c in calls)
    assert (tmp_path / "replica-s1.json").read_text() == before


def test_up_refuses_a_batch_expectation_without_a_named_set_and_sidecar():
    with pytest.raises(SystemExit):
        R.hs_up(None, False, {}, "", None, 16)


# verify: 세트가 가리키는 포트의 info 배치 == 기대 배치 (docker·launchd·HTTP 는 전부 주입)
PROD_INFO = {"model_id": "m", "revision": "r", "dtype": "float16", "device": "mps", "degraded": False}


class _Resp:
    status = 200

    def __init__(self, body):
        self._b = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._b).encode()


def _stub_verify(monkeypatch, tmp_path, meta, tei_info, tei_url=None):
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    (tmp_path / "replica-s1.json").write_text(json.dumps(meta))
    (tmp_path / "sidecar-shared.json").write_text(json.dumps(SHARED))
    prod_env = {**PROD, TEI: "http://host.lima.internal:8787"}
    bench_env = R.hs_env(prod_env, meta.get("hs_env"), "k", tei_url or f"http://host.lima.internal:{meta['sidecar_port']}")
    cont = {"Image": "sha256:prod", "State": {"StartedAt": "t"}, "Mounts": [{"Name": R.PROD_HFCACHE, "RW": False}]}
    cfg = {"config": {"recall_budget_function": "x"}, "overrides": {}}
    monkeypatch.setattr(R, "bench_key", lambda: "k")
    monkeypatch.setattr(R, "inspect", lambda name: cont)
    monkeypatch.setattr(R, "runtime_env", lambda name: prod_env if name == R.PROD_HS else bench_env)
    monkeypatch.setattr(R, "upstream_get_via", lambda c, p: cfg)
    monkeypatch.setattr(R, "hs_get", lambda p, s="": cfg)
    monkeypatch.setattr(R, "freeze_fingerprint", lambda s="": {})
    monkeypatch.setattr(R, "sh", lambda *a, **k: json.dumps({**PROD_INFO, **tei_info}) if a[0] == "docker"
                        else f"uv run uvicorn --port {SHARED['port']}")
    monkeypatch.setattr(R.urllib.request, "urlopen", lambda url, timeout=5: _Resp(PROD_INFO))


def _sc_meta(port=18782, batch=16, **extra):
    meta = {"set": "s1", "image": "sha256:prod", "sidecar_port": port, "sidecar_name": "shared",
            "sidecar": dict(SHARED["overrides"]), "hs_env": {}}
    if batch is not None:
        meta["sidecar_batch_size"] = batch
    return {**meta, **extra}


@pytest.mark.parametrize("port,batch,info,ok", [
    (18782, 16, {"batch_size": 16, "batch_sizes": {"18781": 32, "18782": 16}}, True),     # 보조 포트, 기대 일치
    (18781, 32, {"batch_size": 32, "batch_sizes": {"18781": 32, "18782": 16}}, True),     # 주 포트 팔(B2 의 대조 팔)
    (18782, 16, {"batch_size": 32, "batch_sizes": {"18781": 32, "18782": 16}}, False),    # info 가 32 를 말한다(16 을 기대)
    (18781, 32, {"batch_size": 16, "batch_sizes": {"18781": 32, "18782": 16}}, False),
    (18782, 16, {}, False),                                                                # info 가 배치를 말하지 않는다(보조 포트 없는 사이드카)
    (18782, 16, {"batch_size": 16, "batch_sizes": {"18781": 32, "18782": 8}}, False),     # info 안에서 서로 어긋난다
    (18781, None, {"batch_size": 32, "batch_sizes": {"18781": 32, "18782": 16}}, True),   # 기대 배치 지정 없음 = 기존 동작(대조 안 함)
    (18781, None, {"batch_size": 8}, True),                                                # 지정이 없으면 info 배치가 무엇이든 통과
    (18781, None, {}, True),
])
def test_verify_compares_the_sidecar_info_batch_with_the_arm_batch(tmp_path, monkeypatch, port, batch, info, ok):
    meta = _sc_meta(port=port, batch=batch)
    _stub_verify(monkeypatch, tmp_path, meta, info)
    rep = R.verify("s1")
    assert rep["pass"] is ok
    if batch is not None:
        assert rep["sidecar_batch"]["expected"] == batch and rep["sidecar_batch"]["match"] is ok
    else:
        assert "sidecar_batch" not in rep


def test_verify_fails_when_the_set_container_points_at_another_port_than_recorded(tmp_path, monkeypatch):
    """기록은 보조 포트인데 컨테이너 env 가 주 포트를 가리키면(info 는 주 포트 것) 배치가 우연히 맞아도 실패한다."""
    meta = _sc_meta(port=18782, batch=32)
    _stub_verify(monkeypatch, tmp_path, meta, {"batch_size": 32, "batch_sizes": {"18781": 32, "18782": 16}},
                 tei_url="http://host.lima.internal:18781")
    assert R.verify("s1")["pass"] is False


@pytest.mark.parametrize("meta_port,url_port", [(8787, 8787), (18781, 8787)])
def test_verify_fails_a_named_set_that_points_at_the_production_sidecar(tmp_path, monkeypatch, meta_port, url_port):
    meta = _sc_meta(port=meta_port, batch=None)
    _stub_verify(monkeypatch, tmp_path, meta, {}, tei_url=f"http://host.lima.internal:{url_port}")
    assert R.verify("s1")["pass"] is False


def test_reconf_moves_the_sidecar_port_in_the_set_gateways_arm_provenance_too(tmp_path, monkeypatch):
    """gw-up 시점 스냅숏(arms/<arm>.json)의 sidecar_port 가 reconf 로 옮겨 간 세트 포트와 어긋나지 않는다. 다른 세트의 팔은 그대로."""
    calls: list = []
    _stub_docker(monkeypatch, calls)
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    _set_state(tmp_path)
    (tmp_path / "arms").mkdir()
    (tmp_path / "arms" / "g1.json").write_text(json.dumps({"arm": "g1", "set": "s1", "sidecar_port": 18781}))
    (tmp_path / "arms" / "g2.json").write_text(json.dumps({"arm": "g2", "set": "s2", "sidecar_port": 18781}))
    R.reconf("s1", {}, sidecar_port=18782, sidecar_batch_size=16)
    assert json.loads((tmp_path / "arms" / "g1.json").read_text())["sidecar_port"] == 18782
    assert json.loads((tmp_path / "arms" / "g2.json").read_text())["sidecar_port"] == 18781


# ---------------------------------------------------------------- docker stderr 마스킹 (issue #129)
# compose-go dotenv 파서는 형식이 틀린 .env 줄 원문을 오류에 싣는다. sh() 의 RuntimeError 는 호출자 로그·트레이스백으로 나간다.
# docker 는 가짜 - 실 docker·.env.local 에 닿지 않는다. 시크릿 값은 가짜다.
_SECRET = "FAKE-SECRET-xyz123"
_STDERR_CASES = [
    pytest.param(f'failed to read .env.local: line 3: unexpected character "-" in variable name "BAD-KEY={_SECRET}"',
                 ["failed to read .env.local", "line 3", "unexpected character", "in variable name"], id="bad-key-char"),
    pytest.param(f'failed to read .env.local: line 5: unterminated quoted value "{_SECRET}',
                 ["failed to read .env.local", "line 5", "unterminated quoted value"], id="unterminated-quote"),
    pytest.param('failed to read .env.local: line 7: unexpected character "\\"" in variable name "K=\\"%s"' % _SECRET,
                 ["line 7", "unexpected character", "in variable name"], id="escaped-quote-in-line"),
    pytest.param('time="x" level=warning msg="y"\nfailed to read .env.local: line 9: unterminated quoted value \'%s\n'
                 'HC_OTHER=%s' % (_SECRET, _SECRET), ["line 9", "unterminated quoted value"], id="multi-line"),
    pytest.param('time="2026-10-02T12:00:00+09:00" level=warning msg="The \\"HC_X\\" variable is not set. Defaulting to a blank string."',
                 ['level=warning', 'time="2026-10-02T12:00:00+09:00"',
                  'The \\"HC_X\\" variable is not set. Defaulting to a blank string.'], id="logfmt-warning"),
    pytest.param('level=warning msg="bad HC_OTHER=\\"abc %s\\""' % _SECRET, ['level=warning'], id="logfmt-escaped-quote-inner"),
    pytest.param('level=info msg=%s' % _SECRET, ['level=info'], id="logfmt-msg-unquoted"),
    pytest.param("level=info msg='%s'" % _SECRET, ['level=info'], id="logfmt-msg-single-quoted"),
    pytest.param('/msg=%s' % _SECRET, ['msg=<redacted>'], id="logfmt-key-after-slash"),
    pytest.param(':level=%s' % _SECRET, ['level=<redacted>'], id="logfmt-key-after-colon"),
    pytest.param('level=%s' % _SECRET, ['level=<redacted>'], id="logfmt-level-secret"),
    pytest.param('time=%s' % _SECRET, ['time=<redacted>'], id="logfmt-time-secret"),
    # issue #165-3: 값 중간 단독 \r - 텍스트 모드 읽기가 \r 을 줄바꿈으로 바꾸면 뒤쪽 조각이 줄 규칙에 안 걸린다
    pytest.param(f'failed to read .env.local: line 5: unterminated quoted value "abc\r{_SECRET}',
                 ["line 5", "unterminated quoted value"], id="dotenv-lone-cr-unterminated"),
    pytest.param(f'failed to read .env.local: line 3: unexpected character "-" in variable name "BAD-KEY=abc\r{_SECRET}"',
                 ["line 3", "unexpected character", "in variable name"], id="dotenv-lone-cr-bad-key"),
    pytest.param(f'HC_OTHER=abc\r{_SECRET}', ["HC_OTHER=<redacted>"], id="assign-lone-cr"),
    # issue #165-2: msg="..." 안에 값이 든 logfmt 줄 - 알려진 compose 경고 문구만 남기고 나머지 msg 는 가린다
    pytest.param(f'time="2026-10-02T12:00:00+09:00" level=error msg="auth failed for {_SECRET} (retry)"',
                 ['level=error', 'time="2026-10-02T12:00:00+09:00"', 'msg=<redacted>'], id="logfmt-msg-with-secret"),
    pytest.param(f'level=warning msg="The \\"{_SECRET}\\" variable is not set. Defaulting to a blank string."',
                 ['level=warning'], id="logfmt-msg-secret-as-variable-name"),
    pytest.param(f'level=warning msg="the attribute `version` is obsolete, it will be ignored, please remove it to avoid potential confusion {_SECRET}"',
                 ['level=warning'], id="logfmt-msg-known-prefix-plus-secret"),
    pytest.param('level=warning msg="the attribute `version` is obsolete, it will be ignored, please remove it to avoid potential confusion"',
                 ['level=warning', 'the attribute `version` is obsolete'], id="logfmt-obsolete-version-kept"),
    # issue #165 final-review C3: 값 중간의 공백·탭·개행이 아닌 구분자는 값의 일부 - 정규식이 거기서 끝나면 뒤쪽 조각이 샌다
    *[pytest.param(f'HC_OTHER=abc{sep}{_SECRET}', ["HC_OTHER=<redacted>"], id=f"assign-separator-{ord(sep):04x}")
      for sep in ("\x0b", "\x0c", "\x85", "\u2028", "\xa0")],
    # Go 는 값을 \n 에서만 자르지만 Python splitlines() 는 이 문자들에서도 줄을 쪼갠다
    *[pytest.param(f'failed to read .env.local: line 5: unterminated quoted value "abc{sep}{_SECRET}',
                   ['line 5', 'unterminated quoted value'], id=f"separator-{ord(sep):04x}")
      for sep in (" ", "\x0b", "\x0c", "\x85")],
    pytest.param('Error response from daemon: No such container: hc-bench-hindsight',
                 ['Error response from daemon: No such container: hc-bench-hindsight'], id="plain-error"),
]


@pytest.mark.parametrize("stderr,keeps", _STDERR_CASES)
def test_sh_runtime_error_masks_dotenv_values(monkeypatch, stderr, keeps):
    import types
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr=stderr))
    with pytest.raises(RuntimeError) as e:
        R.sh("docker", "inspect", "hc-bench-hindsight")
    msg = str(e.value)
    assert "docker inspect hc-bench-hindsight... exit 1" in msg        # 명령·rc 는 남는다
    assert _SECRET not in msg and "SECRET-xyz" not in msg, msg
    for frag in keeps:
        assert frag in msg, (frag, msg)


def test_sh_masks_before_taking_the_400_char_tail(monkeypatch):
    import types
    head = f'failed to read .env.local: line 5: unterminated quoted value "{_SECRET}'
    stderr = head + "\n" + "w" * 386                                   # 원문 꼬리 400자가 시크릿 중간부터 시작
    assert "SECRET-xyz123" in stderr[-400:]
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr=stderr))
    with pytest.raises(RuntimeError) as e:
        R.sh("docker", "inspect", "x")
    assert "SECRET-xyz" not in str(e.value)


# ---------------------------------------------------------------- 로그 출력 경로 마스킹 (issue #144, PR #132 C9)
# docker logs SystemExit 두 지점(_start_replica·gw_up)과 사이드카 로그 꼬리(sidecar_up)도 #132 와 같은 행렬로 마스킹한다.
# docker·사이드카·plist 는 전부 가짜, 상태·작업 디렉터리는 tmp_path - 실 운영 자원에 닿지 않는다.
def _isolate_local_policies(monkeypatch, repo):
    """gw_up 이 로컬 정책 원본으로 실 환경을 읽지 않게 한다: env HC_BENCH_LOCAL_POLICIES 를 비우고 기본 원본(REPO/policies/local)을 repo 로 돌린다(issue #217)."""
    monkeypatch.delenv("HC_BENCH_LOCAL_POLICIES", raising=False)
    monkeypatch.setattr(R, "REPO", str(repo))


def _log_exit_via_start_replica(monkeypatch, tmp_path, text):
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(R, "inspect", lambda name: {"State": {"Running": False}})
    monkeypatch.setattr(R, "sh", lambda *a, **k: text if a[:2] == ("docker", "logs") else "")
    with pytest.raises(SystemExit) as e:
        R._start_replica(R.instance_names(""), {"K": "v"}, "img")
    return str(e.value)


def _log_exit_via_gw_up(monkeypatch, tmp_path, text):
    _isolate_local_policies(monkeypatch, tmp_path)
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(R, "WORK_DIR", str(tmp_path / "work"))
    (tmp_path / "replica.json").write_text("{}")
    monkeypatch.setattr(R, "export_commit", lambda sha, dest: None)       # 소스 추출은 이 테스트의 관심사가 아니다(issue #171 테스트가 따로 본다)
    monkeypatch.setattr(R, "sh", lambda *a, **k: text if a[:2] == ("docker", "logs") else "")
    monkeypatch.setattr(R, "gateway_env", lambda *a, **k: {"K": "v"})
    monkeypatch.setattr(R, "bench_key", lambda: "k")
    monkeypatch.setattr(R, "bench_acl_dir", lambda: str(tmp_path))
    monkeypatch.setattr(R, "http_json", lambda *a, **k: (0, None))
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit) as e:
        R.gw_up("g1", "HEAD", 18891)
    return str(e.value)


def _run_sidecar_up_dying(monkeypatch, tmp_path, log, plist_env=None, overrides=None):
    """사이드카가 죽으면서 로그 파일에 log(str|bytes)를 남긴 상황으로 sidecar_up 실패 경로를 태운다. (예외, state 파일 경로) 반환."""
    import os
    import plistlib
    import types
    plist = tmp_path / "prod.plist"
    plist.write_bytes(plistlib.dumps({"EnvironmentVariables": dict(plist_env or {}),
                                      "ProgramArguments": ["python", "srv.py", "--port", "8787"]}))
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(R, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(R, "PROD_SIDECAR_PLIST", str(plist))
    monkeypatch.setattr(R, "_port_in_use", lambda p: False)
    data = log if isinstance(log, bytes) else log.encode()

    def fake_popen(argv, stdout=None, **k):
        os.write(stdout, data)
        return types.SimpleNamespace(pid=1, poll=lambda: 1)
    monkeypatch.setattr(R.subprocess, "Popen", fake_popen)
    with pytest.raises(BaseException) as e:                  # 디코딩 오류도 여기로 들어와 단언이 보게 한다
        R.sidecar_up("s1", 18781, overrides or {})
    return e.value, R.sidecar_state("s1")


def _log_exit_via_sidecar_up(monkeypatch, tmp_path, text):
    exc, _ = _run_sidecar_up_dying(monkeypatch, tmp_path, text)
    assert isinstance(exc, SystemExit), repr(exc)
    return str(exc)


_LOG_SURFACES = [
    pytest.param(_log_exit_via_start_replica, "hc-bench-hindsight exited:", id="docker-logs-start-replica"),
    pytest.param(_log_exit_via_gw_up, "hc-bench-gw-g1 not healthy:", id="docker-logs-gw-up"),
    pytest.param(_log_exit_via_sidecar_up, "bench sidecar s1 exited:", id="sidecar-log"),
]


@pytest.mark.parametrize("stderr,keeps", _STDERR_CASES)
@pytest.mark.parametrize("surface,lead", _LOG_SURFACES)
def test_log_output_paths_mask_dotenv_values(monkeypatch, tmp_path, surface, lead, stderr, keeps):
    msg = surface(monkeypatch, tmp_path, stderr)
    assert msg.count(_SECRET) == 0 and "SECRET-xyz" not in msg, msg
    assert lead in msg, msg                                              # 어느 지점의 오류인지는 남는다
    for frag in keeps:
        assert frag in msg, (frag, msg)


def test_sidecar_log_is_masked_before_taking_the_600_char_tail(monkeypatch, tmp_path):
    head = f'failed to read .env.local: line 5: unterminated quoted value "{_SECRET}'
    text = head + "\n" + "w" * 586                                      # 원문 꼬리 600자가 시크릿 중간부터 시작
    assert "SECRET-xyz123" in text[-600:]
    assert "SECRET-xyz" not in _log_exit_via_sidecar_up(monkeypatch, tmp_path, text)


# ---------------------------------------------------------------- issue #165: 사이드카 로그 꼬리 - 값 기준 마스킹·비 UTF-8·state 정리 순서
# 1: 사이드카가 env 시크릿 값을 KEY= 없이 예외 문구에 찍고 죽는다. sidecar_up 은 자기 env(plist+override)를 알므로
#    시크릿 이름(TOKEN·SECRET·PASSWORD·KEY·DISCORD)의 값 자체를 값 기준으로 가린다. 값이 일반 단어인 비시크릿 키는 건드리지 않는다.
_ENV_SECRET = "FAKE-ENV-SECRET-q9z8x7"
_ENV_FORMS = [
    pytest.param("Traceback (most recent call last):\n  File \"srv.py\", line 9, in <module>\nValueError: bad token {v} rejected", id="traceback-phrase"),
    pytest.param("RuntimeError: cannot connect with '{v}'", id="repr-quoted"),
    pytest.param("auth header Bearer {v}", id="bearer"),
    pytest.param("x" * 700 + " tail {v}", id="after-long-prefix"),
]


@pytest.mark.parametrize("form", _ENV_FORMS)
@pytest.mark.parametrize("key", ["RERANK_AUTH_TOKEN", "RERANK_API_KEY", "RERANK_DB_PASSWORD", "RERANK_CLIENT_SECRET"])
@pytest.mark.parametrize("source", ["plist", "override"])
def test_sidecar_up_masks_env_secret_values_without_key_prefix(monkeypatch, tmp_path, source, key, form):
    env = {key: _ENV_SECRET, "RERANK_MODE": "fastmode-plain"}
    kw = {"plist_env": env} if source == "plist" else {"plist_env": {"RERANK_MODE": "fastmode-plain"}, "overrides": {key: _ENV_SECRET}}
    exc, _ = _run_sidecar_up_dying(monkeypatch, tmp_path, form.format(v=_ENV_SECRET) + "\nmode fastmode-plain", **kw)
    msg = str(exc)
    assert isinstance(exc, SystemExit), repr(exc)
    assert msg.count(_ENV_SECRET) == 0 and "ENV-SECRET" not in msg, msg
    assert "bench sidecar s1 exited:" in msg
    assert "fastmode-plain" in msg                            # 시크릿 이름이 아닌 키의 값은 진단용으로 남는다


def test_sidecar_up_env_secret_split_by_the_600_char_tail(monkeypatch, tmp_path):
    text = "E " + _ENV_SECRET + "\n" + "w" * 579               # 원문 꼬리 600자가 시크릿 중간부터 시작
    assert "ENV-SECRET" in text[-600:]
    exc, _ = _run_sidecar_up_dying(monkeypatch, tmp_path, text, plist_env={"RERANK_AUTH_TOKEN": _ENV_SECRET})
    assert isinstance(exc, SystemExit) and "ENV-SECRET" not in str(exc), repr(exc)


def test_sidecar_up_does_not_mask_short_secret_named_values(monkeypatch, tmp_path):
    # 값이 짧으면(true·1 등) 값 기준으로 가리면 로그 전체가 망가진다 - 최소 길이 미만은 값 기준 마스킹에서 뺀다
    exc, _ = _run_sidecar_up_dying(monkeypatch, tmp_path, "debug true enabled", plist_env={"RERANK_DISCORD_TOKEN_ENABLED": "true"})
    assert "debug true enabled" in str(exc)


# 4: 비 UTF-8 바이트가 든 사이드카 로그 - 디코딩 오류가 state 삭제보다 먼저 터지면 stale state 가 남는다.
_NON_UTF8_LOGS = [
    pytest.param(b"\xff\xfe\x80 boom", id="starts-invalid"),
    pytest.param(b"ok line\n" + b"\xc3\x28 broken\n" + b"tail", id="invalid-in-middle"),
    pytest.param(("HC_OTHER=%s\xff rest" % _SECRET).encode("latin-1"), id="secret-then-invalid"),
    pytest.param(("failed to read .env.local: line 5: unterminated quoted value \"%s\xe9" % _SECRET).encode("latin-1"), id="dotenv-then-invalid"),
    pytest.param(b"w" * 599 + b"\xff", id="invalid-at-tail-edge"),
]


@pytest.mark.parametrize("log", _NON_UTF8_LOGS)
def test_sidecar_up_non_utf8_log_cleans_state_and_does_not_leak(monkeypatch, tmp_path, log):
    import os
    exc, state = _run_sidecar_up_dying(monkeypatch, tmp_path, log)
    assert isinstance(exc, SystemExit), repr(exc)             # UnicodeDecodeError 가 아니다
    assert "bench sidecar s1 exited:" in str(exc)
    assert _SECRET not in str(exc) and "SECRET-xyz" not in str(exc), str(exc)
    assert not os.path.exists(state), "stale sidecar state left behind"
    assert os.listdir(R.STATE_DIR) == []                      # 어떤 state 도 남지 않는다


# ---------------------------------------------------------------- issue #165 (3, 실제 경로): sh() 와 docker logs 표면의 단독 \r
# subprocess 가 text=True 면 \r 이 \n 으로 바뀐 뒤 마스커에 들어와 뒤쪽 조각이 줄 규칙을 피한다. 가짜 subprocess 가 바이트로 돌려준다.
_LONE_CR_CASES = [c for c in _STDERR_CASES if "lone-cr" in c.id]


def _fake_run(stdout=b"", stderr=b"", rc=0, logs_stdout=None):
    """실 subprocess.run 처럼 동작하는 가짜: text=True 면 디코딩하며 \\r\\n·\\r 을 \\n 으로 바꾸고(universal newlines), 아니면 바이트 그대로."""
    import types

    def out(b):
        if b is None or k_text[0] is not True:
            return b
        return b.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    k_text = [None]

    def run(args, **k):
        k_text[0] = k.get("text")
        if logs_stdout is not None and list(args[:2]) == ["docker", "logs"]:
            return types.SimpleNamespace(returncode=0, stdout=out(logs_stdout), stderr=out(b""))
        return types.SimpleNamespace(returncode=rc, stdout=out(stdout), stderr=out(stderr))
    return run


def _bytes_via_sh(monkeypatch, tmp_path, text):
    monkeypatch.setattr(R.subprocess, "run", _fake_run(stderr=text.encode(), rc=1))
    with pytest.raises(RuntimeError) as e:
        R.sh("docker", "inspect", "x")
    return str(e.value)


def _bytes_via_start_replica(monkeypatch, tmp_path, text, env=None):
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(R, "inspect", lambda name: {"State": {"Running": False}})
    monkeypatch.setattr(R.subprocess, "run", _fake_run(logs_stdout=text.encode()))
    with pytest.raises(SystemExit) as e:
        R._start_replica(R.instance_names(""), env or {"K": "v"}, "img")
    return str(e.value)


def _bytes_via_gw_up(monkeypatch, tmp_path, text, env=None):
    _isolate_local_policies(monkeypatch, tmp_path)
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(R, "WORK_DIR", str(tmp_path / "work"))
    (tmp_path / "replica.json").write_text("{}")
    monkeypatch.setattr(R, "export_commit", lambda sha, dest: None)       # 소스 추출은 이 테스트의 관심사가 아니다(issue #171 테스트가 따로 본다)
    monkeypatch.setattr(R.subprocess, "run", _fake_run(logs_stdout=text.encode()))
    monkeypatch.setattr(R, "gateway_env", lambda *a, **k: env or {"K": "v"})
    monkeypatch.setattr(R, "bench_key", lambda: "k")
    monkeypatch.setattr(R, "bench_acl_dir", lambda: str(tmp_path))
    monkeypatch.setattr(R, "http_json", lambda *a, **k: (0, None))
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit) as e:
        R.gw_up("g1", "HEAD", 18891)
    return str(e.value)


@pytest.mark.parametrize("stderr,keeps", _LONE_CR_CASES)
@pytest.mark.parametrize("surface,lead", [
    pytest.param(_bytes_via_sh, "docker inspect x... exit 1", id="sh-stderr"),
    pytest.param(_bytes_via_start_replica, "hc-bench-hindsight exited:", id="docker-logs-start-replica"),
    pytest.param(_bytes_via_gw_up, "hc-bench-gw-g1 not healthy:", id="docker-logs-gw-up"),
])
def test_lone_cr_is_masked_on_the_real_subprocess_surfaces(monkeypatch, tmp_path, surface, lead, stderr, keeps):
    msg = surface(monkeypatch, tmp_path, stderr)
    assert msg.count(_SECRET) == 0 and "SECRET-xyz" not in msg, msg
    assert lead in msg, msg
    for frag in keeps:
        assert frag in msg, (frag, msg)


def test_sh_stdout_contract_is_unchanged_by_byte_capture(monkeypatch):
    monkeypatch.setattr(R.subprocess, "run", _fake_run(stdout=b" a\rb\r\nc\xff \n"))
    assert R.sh("x") == "a\nb\nc\ufffd"                       # 줄바꿈 정규화·strip·문자열 타입은 text=True 때와 같다(비 UTF-8 은 대체 문자)
    assert R.sh("x", keep_cr=True) == "a\rb\r\nc\ufffd"     # docker logs 만 원형(\r 포함)을 보존한다 - 마스커가 줄 끝 \r 은 처리한다
    monkeypatch.setattr(R.subprocess, "run", _fake_run(stdout=None))
    assert R.sh("x", capture=False) == ""


# ---------------------------------------------------------------- issue #165 final-review: 값 기준 마스킹 확대
# C2: docker logs 두 지점도 자기 env 의 시크릿 값을 KEY= 없이 찍힌 형태로 가린다(gw_up 의 소스 추출 부분은 건드리지 않는다).
@pytest.mark.parametrize("form", [
    pytest.param("Incorrect API key provided: {v}", id="phrase"),
    pytest.param("Authorization: Bearer {v}", id="bearer"),
    pytest.param("RuntimeError: cannot connect with '{v}'\nline two", id="repr-quoted"),
])
@pytest.mark.parametrize("surface,lead", [
    pytest.param(_bytes_via_start_replica, "hc-bench-hindsight exited:", id="start-replica"),
    pytest.param(_bytes_via_gw_up, "hc-bench-gw-g1 not healthy:", id="gw-up"),
])
def test_docker_logs_surfaces_mask_env_secret_values_without_key_prefix(monkeypatch, tmp_path, surface, lead, form):
    env = {"HC_HINDSIGHT_API_KEY": _ENV_SECRET, "HC_MODE": "fastmode-plain"}
    msg = surface(monkeypatch, tmp_path, form.format(v=_ENV_SECRET) + "\nmode fastmode-plain", env=env)
    assert msg.count(_ENV_SECRET) == 0 and "ENV-SECRET" not in msg, msg
    assert lead in msg and "fastmode-plain" in msg, msg           # 시크릿 이름이 아닌 키의 값은 진단용으로 남는다


# C4: 시크릿 키 이름 조각에 AUTH·DSN·CREDENTIAL 추가, TOKENS(복수) 키는 제외, *_URL 은 userinfo 의 password 가 있을 때만 대상.
@pytest.mark.parametrize("key", ["RERANK_AUTH_HEADER", "RERANK_DB_DSN", "RERANK_GCP_CREDENTIALS"])
def test_sidecar_up_masks_values_of_auth_dsn_credential_keys(monkeypatch, tmp_path, key):
    exc, _ = _run_sidecar_up_dying(monkeypatch, tmp_path, f"failed with {_ENV_SECRET} here", plist_env={key: _ENV_SECRET})
    assert isinstance(exc, SystemExit) and "ENV-SECRET" not in str(exc), repr(exc)


def test_sidecar_up_does_not_mask_values_of_plural_tokens_keys(monkeypatch, tmp_path):
    exc, _ = _run_sidecar_up_dying(monkeypatch, tmp_path, "limit 1000000 tokens reached",
                                   plist_env={"RERANK_MAX_TOKENS": "1000000", "RERANK_TOKENS_BUDGET": "1000000"})
    assert "limit 1000000 tokens reached" in str(exc), repr(exc)


@pytest.mark.parametrize("url,leaks,keeps", [
    pytest.param("postgres://dbuser:hunter2pw@db.internal:5432/app", ["hunter2pw", "dbuser:hunter2pw"], [], id="userinfo-whole-url"),
    pytest.param("https://svc:p4ssw0rd-long@api.example.com/v1", ["p4ssw0rd-long"], [], id="userinfo-password-alone"),
    pytest.param("http://127.0.0.1:8787/health", [], ["http://127.0.0.1:8787/health"], id="no-userinfo-kept"),
    pytest.param("postgres://dbuser@db.internal/app", [], ["postgres://dbuser@db.internal/app"], id="user-without-password-kept"),
])
def test_sidecar_up_masks_url_keys_only_when_userinfo_has_a_password(monkeypatch, tmp_path, url, leaks, keeps):
    log = f"connect {url} failed; password was {url.split(':')[2].split('@')[0] if leaks else 'n/a'}"
    exc, _ = _run_sidecar_up_dying(monkeypatch, tmp_path, log, plist_env={"RERANK_UPSTREAM_URL": url})
    msg = str(exc)
    assert isinstance(exc, SystemExit), repr(exc)
    for frag in leaks:
        assert frag not in msg, (frag, msg)
    for frag in keeps:
        assert frag in msg, (frag, msg)


# ---------------------------------------------------------------- 게이트웨이 팔 소스는 worktree 가 아니라 git archive (issue #171)
# 팔 빌드 소스가 리포 worktree 목록에 등록되면 Orca 사이드바에 "Detached HEAD" 항목으로 남는다.
# 임시 git 리포 + docker 만 가짜 - git 은 진짜로 돌려 추출 결과와 worktree 등록을 직접 본다.
def _git(repo, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _tmp_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "gateway").mkdir(parents=True)
    (repo / "policies").mkdir()
    (repo / "gateway" / "Dockerfile").write_text("FROM scratch\n")
    (repo / "gateway" / "app.py").write_text("V = 1\n")
    (repo / "gateway" / "run.sh").write_text("#!/bin/sh\n")
    (repo / "gateway" / "run.sh").chmod(0o755)
    (repo / "policies" / "p.json").write_text('{"a": 1}\n')
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c1")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    # 이후 커밋·워킹트리 변경은 sha 의 빌드 컨텍스트에 섞이면 안 된다
    (repo / "gateway" / "app.py").write_text("V = 2\n")
    (repo / "gateway" / "untracked.txt").write_text("x\n")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "c2")
    return repo, sha


def _tree(root):
    import os
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = (open(p, "rb").read(), os.access(p, os.X_OK))
    return out


def _expected_tree(repo, sha):
    import subprocess
    out = {}
    for line in _git(repo, "ls-tree", "-r", sha).splitlines():
        meta, path = line.split("\t", 1)
        blob = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", meta.split()[2]], check=True,
                              capture_output=True).stdout
        out[path] = (blob, meta.split()[0] == "100755")
    return out


def _registered(repo):
    return [l for l in _git(repo, "worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]


def _gw_up_until_build(monkeypatch, tmp_path, repo, sha_ref, patch=None):
    """git 은 진짜, docker 는 가짜. docker build 시점의 빌드 컨텍스트(gateway/)와 policies/ 를 복사해 둔 채 거기서 멈춘다."""
    import os
    import shutil
    seen = {}
    real_sh = R.sh

    def fake_sh(*a, **k):
        if a[0] == "git":
            return real_sh(*a, **k)
        if a[:2] == ("docker", "build"):
            ctx = a[-1]
            seen["ctx"] = ctx
            seen["tree"] = _tree(os.path.dirname(ctx))           # gateway/ 와 policies/ 를 함께 본다
            raise SystemExit("stop-at-build")
        return ""

    _isolate_local_policies(monkeypatch, repo)
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(R, "WORK_DIR", str(tmp_path / "work"))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "replica.json").write_text("{}")
    monkeypatch.setattr(R, "sh", fake_sh)
    monkeypatch.setattr(R, "bench_key", lambda: "k")
    with pytest.raises(SystemExit, match="stop-at-build"):
        R.gw_up("g1", sha_ref, 18891, patch=patch)
    return seen


@pytest.mark.parametrize("ref", ["sha", "HEAD~1"])
def test_gw_up_build_context_is_the_commit_content_and_registers_no_worktree(monkeypatch, tmp_path, ref):
    repo, sha = _tmp_repo(tmp_path)
    before = _registered(repo)
    seen = _gw_up_until_build(monkeypatch, tmp_path, repo, sha if ref == "sha" else ref)
    assert seen["tree"] == _expected_tree(repo, sha)             # git ls-tree -r <sha> 의 파일·내용·실행 비트와 같다
    assert seen["tree"]["gateway/app.py"][0] == b"V = 1\n"       # 이후 커밋(V = 2)이 아니라 요청한 SHA 내용
    assert "gateway/untracked.txt" not in seen["tree"]
    assert _registered(repo) == before                           # Orca 사이드바 잔존의 원인 - worktree 등록이 늘지 않는다
    assert seen["ctx"] == str(tmp_path / "work" / "wt-g1" / "gateway")


def test_gw_up_applies_patch_on_the_extracted_copy_only(monkeypatch, tmp_path):
    repo, sha = _tmp_repo(tmp_path)
    patch = tmp_path / "x.patch"                                 # sha(V = 1) 기준 패치 - HEAD(V = 2)에는 적용되지 않는다
    patch.write_text("--- a/gateway/app.py\n+++ b/gateway/app.py\n@@ -1 +1,2 @@\n V = 1\n+PATCHED = True\n")
    seen = _gw_up_until_build(monkeypatch, tmp_path, repo, sha, patch=str(patch))
    assert seen["tree"]["gateway/app.py"][0] == b"V = 1\nPATCHED = True\n"
    assert (repo / "gateway" / "app.py").read_text() == "V = 2\n"       # 리포 워킹트리는 그대로
    assert len(_registered(repo)) == 1


def test_gw_up_rerun_replaces_the_previous_extraction(monkeypatch, tmp_path):
    repo, sha = _tmp_repo(tmp_path)
    stale = tmp_path / "work" / "wt-g1"
    (stale / "gateway").mkdir(parents=True)
    (stale / "gateway" / "leftover.txt").write_text("old run\n")
    seen = _gw_up_until_build(monkeypatch, tmp_path, repo, sha)
    assert seen["tree"] == _expected_tree(repo, sha)            # 옛 run 의 잔여 파일이 빌드 컨텍스트에 남지 않는다


def _fake_docker_sh(monkeypatch):
    real_sh = R.sh
    monkeypatch.setattr(R, "sh", lambda *a, **k: real_sh(*a, **k) if a[0] == "git" else "")


@pytest.mark.parametrize("legacy_registered", [False, True], ids=["archive-dir", "old-run-worktree"])
@pytest.mark.parametrize("via", ["hs_down", "gw_down"])
def test_down_removes_arm_source_dir_and_any_old_worktree_registration(monkeypatch, tmp_path, legacy_registered, via):
    repo, sha = _tmp_repo(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    wt = work / "wt-g1"
    if legacy_registered:                                        # 이 변경 이전 run 이 남긴 detached worktree
        _git(repo, "worktree", "add", "--detach", str(wt), sha)
        assert len(_registered(repo)) == 2
    else:
        (wt / "gateway").mkdir(parents=True)
        (wt / "gateway" / "app.py").write_text("V = 1\n")
    monkeypatch.setattr(R, "REPO", str(repo))
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(R, "WORK_DIR", str(work))
    _fake_docker_sh(monkeypatch)
    if via == "hs_down":
        R.hs_down()
    else:
        R.gw_down("g1")
    assert not wt.exists()
    assert len(_registered(repo)) == 1                           # 등록도 남지 않는다


# ---------------------------------------------------------------- issue #171 final-review 반영
def test_gw_up_patch_applies_even_when_an_outer_git_repo_encloses_work_dir(monkeypatch, tmp_path):
    """C1: 추출본엔 .git 이 없어 git 이 상위 리포를 잡으면 패치가 조용히 건너뛰어질 수 있다."""
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    repo, sha = _tmp_repo(tmp_path)
    patch = tmp_path / "x.patch"
    patch.write_text("diff --git a/gateway/app.py b/gateway/app.py\n--- a/gateway/app.py\n+++ b/gateway/app.py\n"
                     "@@ -1 +1,2 @@\n V = 1\n+PATCHED = True\n")
    seen = {}
    real_sh = R.sh

    def fake_sh(*a, **k):
        if a[0] == "git":
            return real_sh(*a, **k)
        if a[:2] == ("docker", "build"):
            seen["tree"] = _tree(os.path.dirname(a[-1]))
            raise SystemExit("stop-at-build")
        return ""

    _isolate_local_policies(monkeypatch, repo)
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(R, "WORK_DIR", str(outer / "work"))      # 바깥 git 리포 아래
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "replica.json").write_text("{}")
    monkeypatch.setattr(R, "sh", fake_sh)
    monkeypatch.setattr(R, "bench_key", lambda: "k")
    with pytest.raises(SystemExit, match="stop-at-build"):
        R.gw_up("g1", sha, 18891, patch=str(patch))
    assert seen["tree"]["gateway/app.py"][0] == b"V = 1\nPATCHED = True\n"


def test_apply_patch_ignores_git_dir_from_the_caller_environment(monkeypatch, tmp_path):
    outer = tmp_path / "outer"
    wt = outer / "work" / "wt-g1"
    wt.mkdir(parents=True)
    _git(outer, "init", "-q")
    (wt / "a.txt").write_text("x\n")
    patch = tmp_path / "p.patch"
    patch.write_text("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n x\n+y\n")
    monkeypatch.setenv("GIT_DIR", str(outer / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outer))
    R.apply_patch(str(wt), str(patch))
    assert (wt / "a.txt").read_text() == "x\ny\n"


def test_apply_patch_fails_when_the_patch_was_not_applied(monkeypatch, tmp_path):
    """C1: exit 0 인데 적용 흔적이 없으면 실패로 끝낸다."""
    import subprocess
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "a.txt").write_text("x\n")
    patch = tmp_path / "p.patch"
    patch.write_text("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n x\n+y\n")
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        if "--check" in cmd:
            return real_run(["git", "--version"], capture_output=True) if False else subprocess.CompletedProcess(cmd, 1, b"", b"")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")      # 적용이 조용히 건너뛰어진 상황

    monkeypatch.setattr(R.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="not present"):
        R.apply_patch(str(wt), str(patch))
    assert any("--check" in c for c in calls)


@pytest.mark.parametrize("via", ["gw_down", "gw_up"])
def test_arm_cycle_prunes_registration_whose_directory_is_gone(monkeypatch, tmp_path, via):
    """C2: 디렉터리가 사라진 옛 worktree 등록도 gw_up/gw_down 이 치운다."""
    repo, sha = _tmp_repo(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    wt = work / "wt-g1"
    _git(repo, "worktree", "add", "--detach", str(wt), sha)
    shutil.rmtree(wt)
    assert len(_registered(repo)) == 2
    monkeypatch.setattr(R, "REPO", str(repo))
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(R, "WORK_DIR", str(work))
    _fake_docker_sh(monkeypatch)
    if via == "gw_down":
        R.gw_down("g1")
    else:
        R.remove_arm_source(str(wt))                             # gw_up 이 export 전에 부르는 단계
    assert len(_registered(repo)) == 1


def test_locked_legacy_worktree_is_an_error_not_a_silent_rmtree(monkeypatch, tmp_path):
    """C7: 등록을 못 풀면 디렉터리를 지우지 않고 unlock 안내와 함께 실패한다."""
    repo, sha = _tmp_repo(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    wt = work / "wt-g1"
    _git(repo, "worktree", "add", "--detach", str(wt), sha)
    _git(repo, "worktree", "lock", str(wt))
    monkeypatch.setattr(R, "REPO", str(repo))
    monkeypatch.setattr(R, "WORK_DIR", str(work))
    _fake_docker_sh(monkeypatch)
    with pytest.raises(RuntimeError, match="unlock"):
        R.remove_arm_source(str(wt))
    assert wt.is_dir()                                           # 단서(디렉터리)를 남긴다
    _git(repo, "worktree", "unlock", str(wt))
    R.remove_arm_source(str(wt))                                 # 사용자가 unlock 하면 재시도로 정리된다
    assert not wt.exists() and len(_registered(repo)) == 1


def test_export_commit_survives_stderr_larger_than_a_pipe_buffer(monkeypatch, tmp_path):
    """C4: git archive 의 stderr 가 64KB 를 넘어도 교착하지 않는다."""
    import threading
    repo, sha = _tmp_repo(tmp_path)
    real_git = shutil.which("git")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "git").write_text(f"#!/bin/sh\nhead -c 300000 /dev/zero | tr '\\0' 'e' >&2\nexec {real_git} \"$@\"\n")
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(R, "REPO", str(repo))
    dest = tmp_path / "out"
    t = threading.Thread(target=R.export_commit, args=(sha, str(dest)), daemon=True)
    t.start()
    t.join(30)
    assert not t.is_alive()
    assert (dest / "gateway" / "app.py").read_text() == "V = 1\n"


@pytest.mark.parametrize("leftover", ["file", "dangling-symlink"])
def test_export_commit_clears_file_or_dangling_symlink_at_dest(monkeypatch, tmp_path, leftover):
    """C3"""
    repo, sha = _tmp_repo(tmp_path)
    monkeypatch.setattr(R, "REPO", str(repo))
    dest = tmp_path / "wt-g1"
    if leftover == "file":
        dest.write_text("junk")
    else:
        dest.symlink_to(tmp_path / "nowhere")
    R.export_commit(sha, str(dest))
    assert (dest / "gateway" / "app.py").read_text() == "V = 1\n"


def test_export_commit_cleans_up_when_tar_cannot_run(monkeypatch, tmp_path):
    """C5: tar 실행 자체가 실패해도 git archive 를 정리하고 부분 dest 를 남기지 않는다."""
    repo, sha = _tmp_repo(tmp_path)
    monkeypatch.setattr(R, "REPO", str(repo))
    real_run = R.subprocess.run

    def fake_run(cmd, **k):
        if cmd[0] == "tar":
            raise FileNotFoundError("tar")
        return real_run(cmd, **k)

    spawned = []
    real_popen = R.subprocess.Popen
    monkeypatch.setattr(R.subprocess, "Popen", lambda *a, **k: spawned.append(real_popen(*a, **k)) or spawned[-1])
    monkeypatch.setattr(R.subprocess, "run", fake_run)
    dest = tmp_path / "wt-g1"
    with pytest.raises(FileNotFoundError):
        R.export_commit(sha, str(dest))
    assert not dest.exists()
    assert spawned and spawned[0].poll() is not None             # git archive 가 회수됐다


# ---------------------------------------------------------------- 팔에 policies/local 주입 (issue #217)
# #187 뒤 운영자 정책·트리거는 gitignore 된 policies/local/ 에 있어 git archive 팔에는 들어오지 않는다.
# 원본 우선순위는 CLI 플래그 > env HC_BENCH_LOCAL_POLICIES > 벤치를 실행하는 리포(R.REPO)의 policies/local/.
# 가짜 리포·가짜 내용만 쓴다(실 policies/ 를 원본으로 쓰지 않는다). git 은 진짜, docker 는 가짜.
_LOCAL_FILES = {"bank-policies.json": b'{"bank": "SECRET-BANK-DESC"}\n',
                "context-triggers.json": b'{"triggers": ["SECRET-TRIGGER"]}\n'}


def _put_local(d, files):
    d.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (d / name).write_bytes(data)
    return d


def _sha(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _gw_up_full(monkeypatch, tmp_path, repo, sha, env=None, **kw):
    """gw_up 을 끝까지(헬스 통과 -> 팔 출처 기록) 돌린다. docker run 시점의 마운트 소스 policies/ 트리를 복사해 돌려준다."""
    import os
    seen = {}
    real_sh = R.sh

    def fake_sh(*a, **k):
        if a[0] == "git":
            return real_sh(*a, **k)
        if a[:2] == ("docker", "run"):
            mount = next(x for x in a if isinstance(x, str) and x.endswith(":/app/policies:ro"))[:-len(":/app/policies:ro")]
            seen["mount"] = mount
            seen["policies"] = _tree(mount)
        return ""

    monkeypatch.setattr(R, "REPO", str(repo))
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(R, "WORK_DIR", str(tmp_path / "work"))
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "replica.json").write_text("{}")
    monkeypatch.setattr(R, "sh", fake_sh)
    monkeypatch.setattr(R, "bench_key", lambda: "k")
    monkeypatch.setattr(R, "bench_acl_dir", lambda: str(tmp_path))
    monkeypatch.setattr(R, "gateway_env", lambda *a, **k: {"K": "v"})
    monkeypatch.setattr(R, "inspect", lambda name: {"Image": "sha256:img", "State": {"StartedAt": "t0"}})
    monkeypatch.setattr(R, "http_json", lambda *a, **k: (200, {"upstream_ok": True}))
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    monkeypatch.delenv("HC_BENCH_LOCAL_POLICIES", raising=False)
    if env is not None:
        monkeypatch.setenv("HC_BENCH_LOCAL_POLICIES", str(env))
    prov = R.gw_up("g1", sha, 18891, **kw)
    seen["wt"] = str(tmp_path / "work" / "wt-g1")
    seen["recorded"] = json.load(open(tmp_path / "state" / "arms" / "g1.json"))
    return prov, seen


def _local_tree(seen):
    return {k: v[0] for k, v in seen["policies"].items() if k.startswith("local/")}


def test_gw_up_copies_both_local_policy_files_byte_for_byte_and_records_hashes(monkeypatch, tmp_path, capsys):
    repo, sha = _tmp_repo(tmp_path)
    _put_local(repo / "policies" / "local", _LOCAL_FILES)           # 기본 원본 = 벤치를 실행하는 리포의 policies/local/
    prov, seen = _gw_up_full(monkeypatch, tmp_path, repo, sha)
    assert _local_tree(seen) == {f"local/{n}": d for n, d in _LOCAL_FILES.items()}      # 마운트되는 팔 policies/ 안, 같은 바이트
    assert seen["mount"] == os.path.join(seen["wt"], "policies")
    lp = prov["local_policies"]
    assert lp["result"] == "복사" and lp["source"] == "default"
    assert lp["files"] == {n: _sha(d) for n, d in _LOCAL_FILES.items()}                 # 파일별 sha256
    assert seen["recorded"]["local_policies"] == lp                                      # runner 가 읽는 팔 출처 파일에도 남는다
    dumped = json.dumps(prov) + capsys.readouterr().out
    assert "SECRET-" not in dumped                                                       # 내용은 어디에도 없고 해시만


@pytest.mark.parametrize("present", [["bank-policies.json"], ["context-triggers.json"]], ids=["bank-only", "triggers-only"])
def test_gw_up_copies_only_the_local_policy_files_that_exist(monkeypatch, tmp_path, present):
    repo, sha = _tmp_repo(tmp_path)
    _put_local(repo / "policies" / "local", {n: _LOCAL_FILES[n] for n in present})
    _put_local(repo / "policies" / "local", {"other.json": b"{}\n"})                     # 두 파일 밖은 복사하지 않는다
    prov, seen = _gw_up_full(monkeypatch, tmp_path, repo, sha)
    assert _local_tree(seen) == {f"local/{n}": _LOCAL_FILES[n] for n in present}
    assert prov["local_policies"]["files"] == {n: _sha(_LOCAL_FILES[n]) for n in present}


@pytest.mark.parametrize("flag,env,want", [
    (False, False, "default"), (False, True, "env"), (True, False, "flag"), (True, True, "flag")])
def test_gw_up_local_policy_source_priority_flag_over_env_over_repo_default(monkeypatch, tmp_path, flag, env, want):
    repo, sha = _tmp_repo(tmp_path)
    blobs = {s: {n: s.encode() + b":" + d for n, d in _LOCAL_FILES.items()} for s in ("default", "env", "flag")}
    _put_local(repo / "policies" / "local", blobs["default"])
    flag_dir = _put_local(tmp_path / "o_flag", blobs["flag"])
    env_dir = _put_local(tmp_path / "o_env", blobs["env"])
    kw = {"local_policies": str(flag_dir)} if flag else {}
    prov, seen = _gw_up_full(monkeypatch, tmp_path, repo, sha, env=env_dir if env else None, **kw)
    assert prov["local_policies"]["source"] == want
    assert _local_tree(seen) == {f"local/{n}": d for n, d in blobs[want].items()}
    assert prov["local_policies"]["files"] == {n: _sha(d) for n, d in blobs[want].items()}


def test_gw_up_without_local_policies_leaves_no_local_dir_and_records_none(monkeypatch, tmp_path):
    repo, sha = _tmp_repo(tmp_path)                              # 기본 원본(repo/policies/local)이 없다
    prov, seen = _gw_up_full(monkeypatch, tmp_path, repo, sha)
    assert not any(k.startswith("local") for k in seen["policies"])
    assert not os.path.lexists(os.path.join(seen["wt"], "policies", "local"))
    assert prov["local_policies"]["result"] == "없음" and prov["local_policies"]["files"] == {}
    assert seen["recorded"]["local_policies"] == prov["local_policies"]
    assert seen["policies"] == {k.split("/", 1)[1]: v for k, v in _expected_tree(repo, sha).items() if k.startswith("policies/")}


@pytest.mark.parametrize("via", ["flag", "env"])
def test_gw_up_refuses_an_explicit_local_policy_dir_that_is_missing(monkeypatch, tmp_path, via):
    """오타 낸 덮어쓰기 경로가 조용히 '없음'이 되면 템플릿 정책으로 판정하게 된다 - 추출·빌드 전에 거부한다."""
    repo, sha = _tmp_repo(tmp_path)
    _put_local(repo / "policies" / "local", _LOCAL_FILES)
    bad = str(tmp_path / "nope")
    monkeypatch.setattr(R, "REPO", str(repo))
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(R, "WORK_DIR", str(tmp_path / "work"))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "replica.json").write_text("{}")
    real_sh = R.sh

    def fake_sh(*a, **k):
        assert a[0] == "git", f"ran {a[:2]} before refusing"
        return real_sh(*a, **k)

    monkeypatch.setattr(R, "sh", fake_sh)
    monkeypatch.delenv("HC_BENCH_LOCAL_POLICIES", raising=False)
    kw = {}
    if via == "flag":
        kw["local_policies"] = bad
    else:
        monkeypatch.setenv("HC_BENCH_LOCAL_POLICIES", bad)
    with pytest.raises(SystemExit, match="local policies"):
        R.gw_up("g1", sha, 18891, **kw)
    assert not os.path.lexists(tmp_path / "work" / "wt-g1" / "policies")


def test_gw_up_local_policy_injection_leaves_origin_repo_and_policies_untouched(monkeypatch, tmp_path):
    repo, sha = _tmp_repo(tmp_path)
    other = _put_local(tmp_path / "o_flag", _LOCAL_FILES)
    _put_local(repo / "policies" / "local", {n: b"default:" + d for n, d in _LOCAL_FILES.items()})

    def snap():
        out = {}
        for root in (repo, other):
            for d, _, fs in os.walk(root):
                if os.sep + ".git" in d + os.sep:
                    continue
                for f in fs:
                    p = os.path.join(d, f)
                    st = os.stat(p)
                    out[p] = (open(p, "rb").read(), st.st_mtime_ns, st.st_ino)
        return out, _git(repo, "status", "--porcelain", "--ignored"), _git(repo, "worktree", "list", "--porcelain")

    before = snap()
    prov, seen = _gw_up_full(monkeypatch, tmp_path, repo, sha, local_policies=str(other))
    assert snap() == before                                                 # 원본 리포·원본 policies/(추적분 포함)·원본 디렉터리 불변
    arm = os.path.join(seen["wt"], "policies", "local", "bank-policies.json")
    assert os.stat(arm).st_ino != os.stat(other / "bank-policies.json").st_ino      # 링크가 아니라 복사본
    assert not os.path.samefile(arm, other / "bank-policies.json")
    with open(arm, "ab") as fh:                                             # 팔 쪽을 건드려도 원본은 그대로
        fh.write(b"x")
    assert (other / "bank-policies.json").read_bytes() == _LOCAL_FILES["bank-policies.json"]


def test_gw_up_cli_flag_reaches_gw_up(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(R, "gw_up", lambda *a, **k: seen.update(args=a, kw=k) or {"pass": True})
    assert R.main(["gw-up", "g1", "--port", "18891", "--local-policies", "/x/y"]) == 0
    assert "/x/y" in seen["args"] or seen["kw"].get("local_policies") == "/x/y"
    seen.clear()
    assert R.main(["gw-up", "g1", "--port", "18891"]) == 0
    assert "/x/y" not in seen["args"] and seen["kw"].get("local_policies") in (None, "")


# ---- final-review 20261008-130553 반영 (issue #217)
def _gw_up_refused(monkeypatch, tmp_path, repo, sha, match, env=None, **kw):
    """gw_up 이 SystemExit 으로 거부하는 경로. docker 는 가짜이고, 도커 명령이 나가면 실패한다(빌드 전 거부 확인용 호출 목록을 돌려준다)."""
    ran = []
    real_sh = R.sh

    def fake_sh(*a, **k):
        if a[0] == "git":
            return real_sh(*a, **k)
        ran.append(a[:2])
        return ""

    monkeypatch.setattr(R, "REPO", str(repo))
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(R, "WORK_DIR", str(tmp_path / "work"))
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "replica.json").write_text("{}")
    monkeypatch.setattr(R, "sh", fake_sh)
    monkeypatch.setattr(R, "bench_key", lambda: "k")
    monkeypatch.delenv("HC_BENCH_LOCAL_POLICIES", raising=False)
    if env is not None:
        monkeypatch.setenv("HC_BENCH_LOCAL_POLICIES", str(env))
    with pytest.raises(SystemExit, match=match):
        R.gw_up("g1", sha, 18891, **kw)
    return ran


@pytest.mark.parametrize("via", ["flag", "env"])
@pytest.mark.parametrize("contents", ["empty-dir", "only-unrelated-file"])
def test_gw_up_refuses_an_explicit_local_policy_dir_without_any_policy_file(monkeypatch, tmp_path, via, contents):
    """#5: 두 정책 파일이 하나도 없는 명시 디렉터리는 오타와 같다 - 빌드 전에 거부한다. 기본 경로는 지금처럼 '없음'."""
    repo, sha = _tmp_repo(tmp_path)
    d = tmp_path / "given"
    d.mkdir()
    if contents == "only-unrelated-file":
        (d / "other.json").write_text("{}\n")
    kw = {"local_policies": str(d)} if via == "flag" else {}
    ran = _gw_up_refused(monkeypatch, tmp_path, repo, sha, "local policies", env=d if via == "env" else None, **kw)
    assert ran == [] and not os.path.lexists(tmp_path / "work" / "wt-g1")        # 추출·docker 전에 거부


def _commit_symlink(repo, kind, outside):
    """repo 의 새 커밋에 kind 위치의 심링크(대상 = outside 아래)를 넣고 그 SHA 를 돌려준다."""
    if kind == "policies-dir":
        _git(repo, "rm", "-rq", "policies")
        os.symlink(outside, repo / "policies")
    elif kind == "local-dir":
        os.symlink(outside, repo / "policies" / "local")
    else:                                                        # local-file
        (repo / "policies" / "local").mkdir()
        os.symlink(os.path.join(outside, "bank-policies.json"), repo / "policies" / "local" / "bank-policies.json")
    _git(repo, "add", "-f", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "symlink")
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("kind", ["policies-dir", "local-dir", "local-file"])
def test_gw_up_refuses_a_ref_whose_policy_path_is_a_symlink_out_of_the_arm_tree(monkeypatch, tmp_path, kind):
    """#2: 추출본의 policies / policies/local / 그 안 파일이 심링크면 복사가 트리 밖을 쓴다 - 거부하고 밖은 그대로."""
    repo, _ = _tmp_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "bank-policies.json").write_bytes(b"ORIGINAL\n")
    before = _tree(outside)
    sha = _commit_symlink(repo, kind, str(outside))
    given = _put_local(tmp_path / "given", _LOCAL_FILES)
    _gw_up_refused(monkeypatch, tmp_path, repo, sha, "symlink", local_policies=str(given))
    assert _tree(outside) == before                              # 호스트 쪽 파일·디렉터리는 안 바뀌었다
    assert (given / "bank-policies.json").read_bytes() == _LOCAL_FILES["bank-policies.json"]


def test_gw_up_records_the_local_policy_dir_as_an_absolute_path(monkeypatch, tmp_path):
    """#10: 상대 경로 플래그도 메타데이터 dir 에는 절대 경로로 남는다(CWD 가 달라도 출처를 재현할 수 있게)."""
    repo, sha = _tmp_repo(tmp_path)
    given = _put_local(tmp_path / "rel" / "dir", _LOCAL_FILES)
    monkeypatch.chdir(tmp_path)
    prov, _ = _gw_up_full(monkeypatch, tmp_path, repo, sha, local_policies=os.path.join("rel", "dir"))
    assert prov["local_policies"]["dir"] == str(given) and os.path.isabs(prov["local_policies"]["dir"])


@pytest.mark.parametrize("surface", [_log_exit_via_gw_up, _bytes_via_gw_up])
def test_existing_gw_up_log_helpers_are_isolated_from_the_ambient_local_policy_env(monkeypatch, tmp_path, surface):
    """#8: 개발 셸의 HC_BENCH_LOCAL_POLICIES(없는 경로여도)와 실 R.REPO 의 policies/local 이 이 헬퍼들에 새지 않는다."""
    real_repo = R.REPO
    monkeypatch.setenv("HC_BENCH_LOCAL_POLICIES", str(tmp_path / "does-not-exist"))
    out = surface(monkeypatch, tmp_path, "boom")
    assert "not healthy" in out and "local policies" not in out
    assert R.REPO != real_repo and R.REPO.startswith(str(tmp_path))


def test_gw_up_until_build_helpers_are_isolated_from_the_ambient_local_policy_env(monkeypatch, tmp_path):
    """#9: _gw_up_until_build·outer-repo 테스트의 build 시점 트리 단언이 주변 env 에 좌우되지 않는다."""
    repo, sha = _tmp_repo(tmp_path)
    ambient = _put_local(tmp_path / "ambient", _LOCAL_FILES)
    monkeypatch.setenv("HC_BENCH_LOCAL_POLICIES", str(ambient))
    seen = _gw_up_until_build(monkeypatch, tmp_path, repo, sha)
    assert seen["tree"] == _expected_tree(repo, sha)             # ambient 정책이 섞여 들어오지 않는다


# ---------------------------------------------------------------- issue #297: arm 백업의 기대 override (LINEAGE)

PROD_CFG = {"config": {"recall_budget_function": "x", "memory_defense": {"enabled": False},
                       "default_observation_scopes": None, "observations_mission": None,
                       "disposition_skepticism": 3, "recall_prefer_observations": False},
            "overrides": {"memory_defense": {"enabled": False}}}
ARM_OVR = {"memory_defense": {"enabled": False}, "default_observation_scopes": [["project:*"]],
           "observations_mission": "m", "disposition_skepticism": 4, "recall_prefer_observations": True}
ARM_CFG = {"config": {**PROD_CFG["config"], **ARM_OVR}, "overrides": dict(ARM_OVR)}
D10_CFG = {"config": {**PROD_CFG["config"], "memory_defense": {"enabled": True}}, "overrides": {}}


def _lineage(tmp_path, overrides, name="LINEAGE"):
    p = tmp_path / name
    p.write_text(json.dumps({"source_backup": {"sha256": "0" * 64}, "expected_bank_overrides": overrides}))
    return str(p)


@pytest.mark.parametrize("bench_cfg, expected, ok", [
    (ARM_CFG, None, False),                 # 옵션 없음 = 지금처럼 운영과 override 가 다르면 실패
    (ARM_CFG, ARM_OVR, True),               # arm 모양(범위·미션·성향·관찰 우선) + 기대와 같음 = 통과
    (ARM_CFG, {**ARM_OVR, "disposition_skepticism": 5}, False),    # 기대와 값이 다르다
    (ARM_CFG, {k: v for k, v in ARM_OVR.items() if k != "observations_mission"}, False),   # 기대에 없는 override
    (D10_CFG, {}, True),                    # D10: override 없음이 기대(운영의 memory_defense 차이는 설명된다)
    (D10_CFG, None, False),
    (PROD_CFG, None, True),                 # 옵션 없음 + 운영과 같음 = 지금과 같다
])
def test_bank_override_report_accepts_lineage_expected_overrides(bench_cfg, expected, ok):
    rep = R.bank_override_report(PROD_CFG, bench_cfg, expected)
    assert (rep["bank_overrides_equal"] and not rep["bank_config_diff_unexplained"]) is ok


def test_expected_overrides_still_reject_unexplained_config_keys():
    bench = {"config": {**ARM_CFG["config"], "retain_chunk_size": 9}, "overrides": dict(ARM_OVR)}
    rep = R.bank_override_report(PROD_CFG, bench, ARM_OVR)
    assert rep["bank_overrides_equal"] and rep["bank_config_diff_unexplained"] == ["retain_chunk_size"]


def test_load_expected_overrides_reads_lineage_and_refuses_a_file_without_them(tmp_path):
    assert R.load_expected_overrides(_lineage(tmp_path, ARM_OVR))["overrides"] == ARM_OVR
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"source_backup": {}}))
    with pytest.raises(SystemExit):
        R.load_expected_overrides(str(bad))


@pytest.mark.parametrize("via_up, via_flag, bench_cfg, ok", [
    (False, False, ARM_CFG, False),         # 기록도 옵션도 없다 = 지금과 같은 실패
    (True, False, ARM_CFG, True),           # up --expected-overrides 가 state 에 남긴 기대
    (False, True, ARM_CFG, True),           # verify --expected-overrides
    (False, True, PROD_CFG, False),         # 기대가 있으면 운영과 같아도 기대와 달라 실패
])
def test_verify_uses_expected_overrides_from_up_state_or_flag(tmp_path, monkeypatch, via_up, via_flag, bench_cfg, ok):
    lin = _lineage(tmp_path, ARM_OVR)
    meta = _sc_meta(batch=None)
    if via_up:
        meta["expected_overrides"] = R.load_expected_overrides(lin)
    _stub_verify(monkeypatch, tmp_path, meta, {})
    monkeypatch.setattr(R, "upstream_get_via", lambda c, p: PROD_CFG)
    monkeypatch.setattr(R, "hs_get", lambda p, s="": bench_cfg)
    rep = R.verify("s1", lin if via_flag else None)
    assert rep["pass"] is ok
    assert (rep.get("expected_overrides") is not None) is (via_up or via_flag)


def test_up_records_expected_overrides_in_the_set_state(tmp_path, monkeypatch):
    calls: list = []
    _stub_docker(monkeypatch, calls, volume_exists=False)
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    (tmp_path / "sidecar-shared.json").write_text(json.dumps(SHARED))
    monkeypatch.setattr(R, "mem_available_mb", lambda: 99999)
    monkeypatch.setattr(R, "sha256_file", lambda p: "f" * 64)
    monkeypatch.setattr(R, "set_clock", lambda s="": "2026-10-27T00:00:00Z")
    monkeypatch.setattr(R, "exists", lambda name: False)
    lin = _lineage(tmp_path, ARM_OVR)
    R.hs_up(str(tmp_path / "pg0.tar.gz"), set_name="s2", sidecar_port=SHARED["port"], expected_overrides=lin)
    st = json.loads((tmp_path / "replica-s2.json").read_text())
    assert st["expected_overrides"]["overrides"] == ARM_OVR
    assert st["expected_overrides"]["source"] == os.path.abspath(lin)


def test_down_removes_the_default_set_state_file(tmp_path, monkeypatch):
    """rework #297-1: 기본 세트 replica.json 이 남으면 runner meta.provenance.replica 에 옛 run 의 값이 실린다."""
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(R, "WORK_DIR", str(tmp_path / "no-work"))
    monkeypatch.setattr(R, "sh", lambda *a, **k: "")
    (tmp_path / "replica.json").write_text("{}")
    (tmp_path / "replica-s1.json").write_text("{}")
    (tmp_path / "bench.key").write_text("k")
    R.hs_down()
    assert not (tmp_path / "replica.json").exists() and not (tmp_path / "replica-s1.json").exists()
    assert (tmp_path / "bench.key").exists()
