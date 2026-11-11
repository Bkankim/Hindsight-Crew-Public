"""이슈 #473 (a): REST `PUT` 뱅크 루트(/v1/<tenant>/banks/<bank>)도 MCP와 같은 소유자 검사를 받는다.

전에는 #458이 이 경로를 `provision_bank`로 남겨 ACL 안 모든 뱅크에서 통과시켰다. 그래서 팀원 누구나
팀 뱅크 프로필을 REST로 덮어쓸 수 있었다. 같은 일(뱅크 프로필 쓰기)을 MCP로 하면 `update_bank`는
소유자 개인 뱅크에서만 통과한다. 이 파일은 그 차이를 표면(MCP/REST) x 뱅크(개인/팀) x 호출자 행렬로
고정하고, verify/gate2_banks.sh가 팀 뱅크를 팀원 토큰 대신 운영 경로(컨테이너 안 upstream 직접 호출,
키는 stdin)로 프로비저닝하는지를 가짜 docker·curl로 확인한다(실제 스택에는 닿지 않는다).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway import policy
from gateway.acl import ACL
from gateway.bank_policies import BankPolicies

REPO = Path(__file__).resolve().parent.parent
PERSONAL = "personal-alice"
TEAM = "team-eng"
TOKENS = {"alice": "tok-alice", "bob": "tok-bob"}


def rest_put(client, token, bank, body=None):
    return client.put(f"/v1/default/banks/{bank}",
                      headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
                      content=json.dumps(body if body is not None else {"name": bank}))


def mcp_update_bank(client, token, bank):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "update_bank", "arguments": {"name": bank}}}
    return client.post(f"/mcp/{bank}/",
                       headers={"authorization": f"Bearer {token}", "content-type": "application/json",
                                "accept": "application/json, text/event-stream"},
                       content=json.dumps(msg))


@pytest.mark.parametrize("who", sorted(TOKENS))
def test_team_member_rest_put_on_team_bank_root_is_denied_with_zero_upstream(make_app, recorder, who):
    """팀원(alice, bob 모두)이 팀 뱅크 루트에 PUT하면 403, upstream 0회."""
    c = TestClient(make_app())
    r = rest_put(c, TOKENS[who], TEAM)
    assert r.status_code == 403, r.text
    assert r.json()["reason"] == f"provision-on-nonpersonal:{policy.PROVISION_BANK}"
    assert recorder.calls == []


@pytest.mark.parametrize("who", sorted(TOKENS))
def test_owner_rest_put_on_own_personal_bank_root_passes(make_app, recorder, who):
    """소유자의 자기 개인 뱅크 프로비저닝(gate2의 개인 뱅크 경로)은 그대로 통과한다."""
    c = TestClient(make_app())
    bank = f"personal-{who}"
    r = rest_put(c, TOKENS[who], bank)
    assert r.status_code == 200, r.text
    assert [call["path"] for call in recorder.calls] == [f"/v1/default/banks/{bank}"]


def test_rest_put_on_someone_elses_personal_bank_stays_outside_the_acl(make_app, recorder):
    c = TestClient(make_app())
    r = rest_put(c, "tok-alice", "personal-bob")
    assert r.status_code == 403 and r.json()["reason"] == "bank-not-in-acl:personal-bob"
    assert recorder.calls == []


# 표면 x 뱅크 x 호출자: 뱅크 프로필 쓰기는 두 표면에서 같은 판정(소유자 개인 뱅크만)을 받는다.
MATRIX = [(surface, who, bank)
          for surface in ("mcp", "rest")
          for who in sorted(TOKENS)
          for bank in (f"personal-{who}", TEAM)]


@pytest.mark.parametrize("surface, who, bank", MATRIX,
                         ids=[f"{s}-{w}-{'team' if b == TEAM else 'own-personal'}" for s, w, b in MATRIX])
def test_bank_profile_write_gets_the_same_owner_verdict_on_both_surfaces(make_app, recorder, surface, who, bank):
    c = TestClient(make_app())
    call = mcp_update_bank if surface == "mcp" else rest_put
    r = call(c, TOKENS[who], bank)
    if bank == TEAM:
        assert r.status_code == 403, r.text
        assert r.json()["reason"].endswith("-on-nonpersonal:" + ("update_bank" if surface == "mcp"
                                                                  else policy.PROVISION_BANK))
        assert recorder.calls == []
    else:
        assert r.status_code == 200, r.text
        assert len(recorder.calls) == 1


@pytest.mark.parametrize("who", sorted(TOKENS))
@pytest.mark.parametrize("bank_kind", ["own-personal", "team"])
def test_pure_verdict_of_put_root_equals_mcp_update_bank(who, bank_kind):
    """policy 수준 행렬: decide_rest(PUT 루트)의 허용 여부 = decide(tools/call update_bank)의 허용 여부."""
    principal = ACL.from_dict({"tokens": {"t": {"identity": who, "personal": f"personal-{who}",
                                                "teams": [TEAM]}}}).principal_for("t")
    bank = f"personal-{who}" if bank_kind == "own-personal" else TEAM
    bp = BankPolicies.defaults_only().for_bank(bank)
    rest = policy.decide_rest(principal, bank, "PUT", "", bank_policy=bp)
    mcp = policy.decide(principal, bank, "tools/call", "update_bank", bank_policy=bp)
    assert rest.allow == mcp.allow == (bank_kind == "own-personal")


# --- verify/gate2_banks.sh: 운영 프로비저닝 경로 --------------------------------------------
# 가짜 curl은 게이트웨이 호출을 기록하고 상태 코드를 돌려준다(팀 뱅크 루트 PUT은 STUB_TEAM_PUT_CODE).
# 가짜 docker는 exec 인자와 stdin을 기록하고, 컨테이너 안 upstream 직접 PUT의 결과로 STUB_EXEC_CODE를 낸다.
STUB_CURL = r"""#!/bin/bash
m=""; u=""; prev=""
for a in "$@"; do
  [ "$prev" = "-X" ] && m="$a"
  case "$a" in http://*) u="$a" ;; esac
  prev="$a"
done
hdr="$(cat)"
printf '%s %s auth=%s\n' "$m" "$u" "$([ -n "$hdr" ] && echo yes || echo no)" >> "${STUB_CURL_LOG:?}"
case "$m $u" in
  "PUT "*/banks/team-eng) printf '%s' "${STUB_TEAM_PUT_CODE:-403}" ;;
  *) printf '200' ;;
esac
"""

STUB_DOCKER = r"""#!/bin/bash
{ printf 'argv='; for a in "$@"; do printf '[%s]' "${a//$'\n'/ }"; done; echo; } >> "${STUB_DOCKER_LOG:?}"
case "$1" in
  exec)
    if [ -n "${STUB_EXEC_PY:-}" ]; then   # run the in-container snippet locally, urlopen patched (no network)
      shift 4; exec env PYTHONPATH="$STUB_EXEC_SITE" "$STUB_EXEC_PY" "$@"
    fi
    printf '%s\n' "$(cat)" >> "${STUB_STDIN_LOG:?}"
    printf '%s\n' "${STUB_EXEC_CODE:-200}" ;;
  *) exit 1 ;;
esac
"""

FAKE_UPSTREAM_KEY = "demo-upstream-key-not-real"


def _write_exec(path, text):
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def gate2(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exec(bin_dir / "curl", STUB_CURL)
    _write_exec(bin_dir / "docker", STUB_DOCKER)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "demo-tokens.env").write_text("HC_DEMO_TOKEN_ALICE=demo-token-alice\n")
    env_file = tmp_path / "gate.env"
    env_file.write_text(f"HC_HINDSIGHT_API_KEY={FAKE_UPSTREAM_KEY}\n")
    logs = {k: tmp_path / f"{k}.log" for k in ("curl", "docker", "stdin")}

    def run(**extra):
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
               "HC_ENV_FILE": str(env_file), "HC_SECRETS_DIR": str(secrets),
               "HC_GATEWAY_BIND": "127.0.0.1:9", "HC_HINDSIGHT_CONTAINER": "demo-hindsight-1",
               "STUB_CURL_LOG": str(logs["curl"]), "STUB_DOCKER_LOG": str(logs["docker"]),
               "STUB_STDIN_LOG": str(logs["stdin"])}
        env.update(extra)
        r = subprocess.run(["/bin/bash", str(REPO / "verify" / "gate2_banks.sh")], cwd=str(tmp_path), env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        read = {k: (p.read_text().splitlines() if p.exists() else []) for k, p in logs.items()}
        return r, read
    return run


def test_gate2_provisions_the_team_bank_by_the_ops_path_and_checks_the_member_put_is_refused(gate2):
    r, logs = gate2()
    assert r.returncode == 0, r.stderr
    gw = "http://127.0.0.1:9/v1/default/banks"
    # 개인 뱅크는 소유자 토큰으로 게이트웨이 경유, 팀 뱅크 게이트웨이 PUT은 거부 확인용 1회뿐.
    puts = [ln for ln in logs["curl"] if ln.startswith("PUT ")]
    assert puts == [f"PUT {gw}/personal-alice auth=yes", f"PUT {gw}/team-eng auth=yes"]
    # 팀 뱅크 프로비저닝은 컨테이너 안 upstream 직접 호출, 키는 stdin으로만(argv에는 없음).
    execs = [ln for ln in logs["docker"] if ln.startswith("argv=[exec]")]
    assert len(execs) == 1, logs["docker"]
    assert execs[0].startswith("argv=[exec][-i][demo-hindsight-1][python3][-c][")
    assert execs[0].endswith("][default][team-eng]")
    assert FAKE_UPSTREAM_KEY not in "\n".join(logs["docker"] + logs["curl"])
    assert logs["stdin"] == [FAKE_UPSTREAM_KEY]
    # 두 뱅크 모두 recall로 프로비저닝 확인.
    assert [ln for ln in logs["curl"] if ln.startswith("POST ")] == [
        f"POST {gw}/personal-alice/memories/recall auth=yes", f"POST {gw}/team-eng/memories/recall auth=yes"]


def test_gate2_fails_when_the_gateway_lets_a_member_overwrite_the_team_bank(gate2):
    r, logs = gate2(STUB_TEAM_PUT_CODE="200")
    assert r.returncode == 1
    assert "team-eng" in r.stderr and "403" in r.stderr


def test_gate2_fails_when_the_ops_provisioning_fails(gate2):
    r, _logs = gate2(STUB_EXEC_CODE="500")
    assert r.returncode == 1 and "team-eng" in r.stderr


def test_gate2_names_the_kind_of_a_non_http_failure_of_the_operator_put(gate2, tmp_path):
    """final-review #473 Bugs 5: 연결 거부 등 HTTPError 밖의 예외도 원인 종류 한 줄로 남긴다(키는 출력하지 않는다).
    컨테이너 안 스니펫을 가짜 docker가 로컬 파이썬으로 돌리고, sitecustomize로 urlopen이 연결 거부를 내게 한다."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        "import urllib.error, urllib.request\n"
        "def _refuse(*a, **k):\n"
        "    raise urllib.error.URLError(ConnectionRefusedError(61, 'refused'))\n"
        "urllib.request.urlopen = _refuse\n")
    r, _logs = gate2(STUB_EXEC_PY=sys.executable, STUB_EXEC_SITE=str(site))
    assert r.returncode == 1
    assert "-> error:URLError:ConnectionRefusedError" in r.stderr, r.stderr
    assert FAKE_UPSTREAM_KEY not in r.stderr + r.stdout


def test_gate2_fails_without_an_upstream_key_and_never_execs(gate2, tmp_path):
    empty = tmp_path / "empty.env"
    empty.write_text("")
    r, logs = gate2(HC_ENV_FILE=str(empty))
    assert r.returncode == 1 and "HC_HINDSIGHT_API_KEY" in r.stderr
    assert not any(ln.startswith("argv=[exec]") for ln in logs["docker"])


def test_gate2_inline_upstream_call_targets_the_container_local_api_with_the_key_from_stdin():
    text = (REPO / "verify" / "gate2_banks.sh").read_text()
    assert "http://localhost:8888" in text
    assert "sys.stdin" in text
    assert "HC_GATE_NEEDS_CONTAINER=1" in text
    assert os.access(REPO / "verify" / "gate2_banks.sh", os.R_OK)


# --- rework #473-1: PUT 뱅크 루트도 뱅크 정책 blocked_operations 판정을 받는다 -------------------
# PUT 루트는 뱅크 프로필 쓰기라 MCP update_bank·REST PATCH 루트와 같은 연산 이름(update_bank)으로 판정한다.
# 정책은 실제 정책 파일과 같은 길(파일에 써서 BankPolicies.from_file, 검증기 _BLOCKABLE_OPERATIONS 포함)로 만든다.
import tempfile  # noqa: E402


def _policies(blocked):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "bank-policies.json"
        path.write_text(json.dumps({"defaults": {}, "banks": {PERSONAL: {"blocked_operations": sorted(blocked)}}}))
        return BankPolicies.from_file(str(path))


def test_owner_rest_put_on_own_bank_is_denied_when_the_bank_policy_blocks_the_profile_write(make_app, recorder):
    c = TestClient(make_app(bank_policies=_policies({"update_bank"})))
    r = rest_put(c, "tok-alice", PERSONAL)
    assert r.status_code == 403, r.text
    assert r.json()["reason"] == "blocked-by-bank-policy:update_bank"
    assert recorder.calls == []


@pytest.mark.parametrize("blocked", [set(), {"delete_bank", "clear_memories"}], ids=["none", "other-ops"])
def test_owner_rest_put_on_own_bank_passes_when_the_profile_write_is_not_blocked(make_app, recorder, blocked):
    c = TestClient(make_app(bank_policies=_policies(blocked)))
    assert rest_put(c, "tok-alice", PERSONAL).status_code == 200
    assert len(recorder.calls) == 1


def rest_patch_root(client, token, bank):
    return client.patch(f"/v1/default/banks/{bank}",
                        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
                        content=json.dumps({"name": bank}))


def rest_patch_config(client, token, bank):
    return client.patch(f"/v1/default/banks/{bank}/config",
                        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
                        content=json.dumps({"updates": {"reflect_mission": "demo"}}))


PROFILE_WRITES = {"mcp-update_bank": mcp_update_bank, "rest-patch-root": rest_patch_root, "rest-put-root": rest_put,
                  "rest-patch-config": rest_patch_config}


@pytest.mark.parametrize("blocked", [True, False], ids=["blocked", "not-blocked"])
@pytest.mark.parametrize("surface", sorted(PROFILE_WRITES))
def test_profile_write_blocked_verdict_is_the_same_on_every_surface(make_app, recorder, surface, blocked):
    """같은 정책에서 MCP update_bank, REST PATCH 루트, REST PUT 루트가 같은 판정을 받는다."""
    c = TestClient(make_app(bank_policies=_policies({"update_bank"} if blocked else set())))
    r = PROFILE_WRITES[surface](c, "tok-alice", PERSONAL)
    if blocked:
        assert r.status_code == 403 and r.json()["reason"] == "blocked-by-bank-policy:update_bank", r.text
        assert recorder.calls == []
    else:
        assert r.status_code == 200, r.text
        assert len(recorder.calls) == 1


def test_owner_check_still_runs_before_the_blocked_operations_check():
    """소유자 검사가 먼저다: 팀 뱅크 PUT은 정책이 막아도 provision-on-nonpersonal로 거부된다."""
    principal = ACL.from_dict({"tokens": {"t": {"identity": "alice", "personal": PERSONAL,
                                                "teams": [TEAM]}}}).principal_for("t")
    policies = _policies({"update_bank"})
    team = policy.decide_rest(principal, TEAM, "PUT", "", bank_policy=policies.for_bank(PERSONAL))
    own = policy.decide_rest(principal, PERSONAL, "PUT", "", bank_policy=policies.for_bank(PERSONAL))
    assert (team.allow, team.reason) == (False, f"provision-on-nonpersonal:{policy.PROVISION_BANK}")
    assert (own.allow, own.reason) == (False, "blocked-by-bank-policy:update_bank")


def test_the_policy_file_loader_accepts_update_bank_in_blocked_operations():
    """final-review #473 Bugs 1: 전에는 검증기가 delete_bank·clear_memories만 받아, update_bank를 적은 정책
    파일은 BankPolicyError로 기동을 막았고 프로필 쓰기를 막을 방법이 없었다."""
    assert "update_bank" in _policies({"update_bank"}).for_bank(PERSONAL).blocked_operations
