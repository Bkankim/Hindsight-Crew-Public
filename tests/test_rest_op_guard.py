"""이슈 #458 (a), #305: REST 표면도 경로·메서드를 연산으로 해석해 MCP와 같은 판정을 받는다.

전에는 `rest_proxy`가 팀 뱅크의 DELETE·PATCH만 막고 POST 관리 연산(멘탈 모델 clear·refresh·생성,
지시문 생성, 지식 베이스 페이지·폴더 생성)과 알 수 없는 경로를 upstream으로 넘겼다. 같은 일을 MCP로
하면 403·upstream 0회다. 이 파일은 그 차이를 행렬로 고정한다:

- 연산표 행 x 뱅크(개인/팀) x KB 읽기 플래그: REST 판정(403 여부)이 같은 도구의 MCP 판정과 같다.
- 팀 뱅크 POST 관리 경로, 알 수 없는 경로·메서드: 403, upstream 0회.
- 기존 REST 사용처(운영 스크립트·verify/의 게이트웨이 경유 호출): 그대로 통과.
"""
import json

import pytest
from fastapi.testclient import TestClient

from gateway import policy

PERSONAL = "personal-alice"
TEAM = "team-eng"
TOKEN = "tok-alice"

# (HTTP 메서드, 뱅크 아래 경로, 같은 판정을 받을 MCP 도구, REST 본문=MCP 인자)
# 이 표는 테스트가 따로 적은 기대값이다: gateway.policy.REST_OPERATIONS가 바뀌면 여기서 드러난다.
TABLE = [
    ("GET", "", "get_bank", None),
    ("PATCH", "", "update_bank", {}),
    ("DELETE", "", "delete_bank", None),
    ("GET", "config", "get_bank", None),
    ("PATCH", "config", "update_bank", {}),
    ("GET", "stats", "get_bank_stats", None),
    ("POST", "memories", "retain", {"items": []}),
    ("DELETE", "memories", "clear_memories", None),
    ("POST", "memories/recall", "recall", {"query": "q"}),
    ("GET", "memories/list", "list_memories", None),
    ("GET", "memories/m1", "get_memory", None),
    ("PATCH", "memories/m1", "update_memory", {}),
    ("DELETE", "memories/m1", "invalidate_memory", None),
    ("POST", "reflect", "reflect", {"query": "q"}),
    ("GET", "documents", "list_documents", None),
    ("GET", "documents/d1", "get_document", None),
    ("DELETE", "documents/d1", "delete_document", None),
    ("GET", "tags", "list_tags", None),
    ("GET", "mental-models", "list_mental_models", None),
    ("POST", "mental-models", "create_mental_model", {}),
    ("GET", "mental-models/m1", "get_mental_model", None),
    ("PATCH", "mental-models/m1", "update_mental_model", {}),
    ("DELETE", "mental-models/m1", "delete_mental_model", None),
    ("POST", "mental-models/m1/refresh", "refresh_mental_model", {}),
    ("POST", "mental-models/m1/clear", "clear_mental_model", {}),
    ("GET", "directives", "list_directives", None),
    ("POST", "directives", "create_directive", {}),
    ("DELETE", "directives/d1", "delete_directive", None),
    ("GET", "operations", "list_operations", None),
    ("GET", "operations/o1", "get_operation", None),
    ("DELETE", "operations/o1", "cancel_operation", None),
    ("GET", "knowledge-base/tree", "get_knowledge_base_tree", None),
    ("GET", "knowledge-base/search", "search_knowledge_base", None),
    ("GET", "knowledge-base/pages/p1", "get_knowledge_page", None),
]

# 팀 뱅크에서 거부돼야 하는 POST 관리 경로(#458 a)와 지식 베이스 쓰기 경로(#305, 두 표기 모두).
TEAM_POST_ADMIN = [
    "mental-models",
    "mental-models/m1/clear",
    "mental-models/m1/refresh",
    "directives",
    "knowledge-base/pages",
    "knowledge-base/folders",
    "knowledge/pages",
    "knowledge/folders",
]

# 연산표에 없는 경로·메서드: 어느 뱅크에서든 거부(deny-by-default).
UNKNOWN = [
    ("POST", "admin/reset"),
    ("GET", "llm-requests"),
    ("GET", "webhooks"),
    ("POST", "knowledge-base/pages"),
    ("POST", "knowledge-base/folders"),
    ("POST", "knowledge/pages"),
    ("POST", "knowledge/folders"),
    ("PATCH", "knowledge-base/nodes/n1"),
    ("DELETE", "knowledge-base/nodes/n1"),
    ("DELETE", "memories/m1/observations"),
    ("PUT", "memories"),
    ("PUT", "config"),
    ("POST", "mental-models/m1/unknown"),
    ("GET", "mental-models/m1/clear"),
    ("POST", "stats"),
    ("GET", "memories/recall/extra/deep"),
]


def _headers(token=TOKEN):
    return {"authorization": f"Bearer {token}", "content-type": "application/json"}


def rest(client, method, bank, sub, payload=None, token=TOKEN):
    path = f"/v1/default/banks/{bank}" + (f"/{sub}" if sub else "")
    content = json.dumps(payload) if payload is not None else None
    return client.request(method, path, content=content, headers=_headers(token))


def mcp(client, bank, tool, arguments, token=TOKEN):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}}}
    return client.post(f"/mcp/{bank}/", content=json.dumps(body), headers=_headers(token))


def _rest_calls(recorder):
    return [c for c in recorder.calls if c["path"].startswith("/v1/")]


def _mcp_calls(recorder):
    return [c for c in recorder.calls if c["path"].startswith("/mcp")]


@pytest.mark.parametrize("kb_on", [False, True], ids=["kb-off", "kb-on"])
@pytest.mark.parametrize("bank", [PERSONAL, TEAM])
@pytest.mark.parametrize("method, sub, tool, payload", TABLE,
                         ids=[f"{m} {s or '<root>'}" for m, s, _t, _p in TABLE])
def test_rest_verdict_matches_mcp_verdict(make_app, recorder, monkeypatch,
                                          method, sub, tool, payload, bank, kb_on):
    """같은 연산, 같은 뱅크: REST가 403이면 MCP도 403이고, REST가 통과하면 MCP도 통과한다."""
    if kb_on:
        monkeypatch.setenv(policy.KB_READ_ENV_FLAG, "1")
    c = TestClient(make_app())

    m = mcp(c, bank, tool, payload)
    mcp_denied = m.status_code == 403
    r = rest(c, method, bank, sub, payload)
    rest_denied = r.status_code == 403

    assert rest_denied == mcp_denied, (
        f"REST {method} {sub!r} -> {r.status_code} {r.text[:200]}, MCP {tool} -> {m.status_code}")
    # 거부면 upstream 0회, 허용이면 정확히 1회(REST 쪽). MCP 쪽은 허용돼도 형식 규칙(isError)에서
    # 멈출 수 있어(빈 인자) 거부일 때의 0회만 본다.
    assert len(_rest_calls(recorder)) == (0 if rest_denied else 1)
    if mcp_denied:
        assert _mcp_calls(recorder) == []
    # 소유자 범위 확인: 팀 뱅크에서는 파괴·교정 도구가 거부, 개인 뱅크에서는 허용.
    if tool in policy.DESTRUCTIVE_TOOLS or tool in policy.CORRECTIVE_TOOLS:
        assert rest_denied == (bank == TEAM)
    if tool in policy.KB_READ_TOOLS:
        assert rest_denied == (not kb_on)


@pytest.mark.parametrize("sub", TEAM_POST_ADMIN)
def test_team_bank_rest_post_admin_path_denied_without_upstream(client, recorder, sub):
    r = rest(client, "POST", TEAM, sub, {})
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "forbidden"
    assert recorder.calls == []


@pytest.mark.parametrize("bank", [PERSONAL, TEAM])
@pytest.mark.parametrize("method, sub", UNKNOWN, ids=[f"{m} {s}" for m, s in UNKNOWN])
def test_unknown_rest_path_or_method_denied_without_upstream(client, recorder, method, sub, bank):
    r = rest(client, method, bank, sub, {} if method in ("POST", "PUT", "PATCH") else None)
    assert r.status_code == 403, r.text
    assert recorder.calls == []


def test_unknown_rest_path_deny_is_audited(make_app, recorder, tmp_path):
    app = make_app()
    c = TestClient(app)
    r = rest(c, "POST", TEAM, "mental-models/m1/clear", {})
    assert r.status_code == 403
    with open(app.state._audit_path, encoding="utf-8") as fh:
        entries = [json.loads(ln) for ln in fh if ln.strip()]
    denies = [e for e in entries if e["decision"] == "deny"]
    assert denies and denies[-1]["reason"] == "corrective-on-nonpersonal:clear_mental_model"
    r = rest(c, "POST", TEAM, "knowledge-base/pages", {})
    with open(app.state._audit_path, encoding="utf-8") as fh:
        entries = [json.loads(ln) for ln in fh if ln.strip()]
    assert entries[-1]["decision"] == "deny"
    assert entries[-1]["reason"].startswith("rest-op-not-whitelisted:")


def test_personal_owner_gets_corrective_rest_on_own_bank_only(make_app, recorder):
    """개인 뱅크 소유자는 MCP와 같은 범위: 자기 개인 뱅크의 교정 연산은 통과, 남의 개인 뱅크는 ACL 밖."""
    c = TestClient(make_app())
    assert rest(c, "POST", PERSONAL, "mental-models/m1/clear", {}).status_code == 200
    assert len(recorder.calls) == 1
    r = rest(c, "POST", "personal-bob", "mental-models/m1/clear", {})
    assert r.status_code == 403
    assert r.json()["reason"] == "bank-not-in-acl:personal-bob"
    assert len(recorder.calls) == 1


def test_provision_bank_put_is_rest_only_and_root_only(client, recorder):
    """PUT 뱅크 루트는 소유자 개인 뱅크에서만 통과한다(#473 a, 전에는 ACL 안 모든 뱅크).
    다른 경로의 PUT은 연산표에 없어 거부된다."""
    assert rest(client, "PUT", PERSONAL, "", {"name": PERSONAL}).status_code == 200
    assert rest(client, "PUT", TEAM, "", {"name": TEAM}).status_code == 403
    assert len(recorder.calls) == 1
    assert rest(client, "PUT", TEAM, "memories", {}).status_code == 403
    assert len(recorder.calls) == 1
    assert policy.resolve_rest_operation("PUT", "") == policy.PROVISION_BANK
    assert policy.PROVISION_BANK not in (policy.READ_TOOLS | policy.RETAIN_TOOLS
                                         | policy.DESTRUCTIVE_TOOLS | policy.CORRECTIVE_TOOLS)


# --- 기존 REST 사용처(게이트웨이 경유) -------------------------------------------------------
# `grep -rn "/v1/" scripts/ bench/ verify/` 결과 중 게이트웨이(HC_GATEWAY_URL·HC_GW)로 가는 호출만.
# 컨테이너 안에서 upstream을 직접 부르는 스크립트(bank_stats·bank-config·item-backfill·
# scope-migrate·tag_migrate·tag_hygiene·item_roles_sidecar·mcp-probe·restore-test, bench의
# docker exec 경로)는 게이트웨이를 거치지 않아 이 허용 목록의 영향을 받지 않는다.
EXISTING = [
    # scripts/model-refresh.sh (개인 뱅크, 소유자 토큰)
    ("model-refresh", "GET", PERSONAL, "mental-models", None),
    ("model-refresh", "GET", PERSONAL, "memories/list", None),
    ("model-refresh", "GET", PERSONAL, "operations/op-1", None),
    ("model-refresh", "POST", PERSONAL, "mental-models/m1/clear", None),
    ("model-refresh", "POST", PERSONAL, "mental-models/m1/refresh", None),
    # scripts/model_watch.py
    ("model_watch", "GET", PERSONAL, "operations", None),
    # scripts/model_audit.py
    ("model_audit", "GET", PERSONAL, "mental-models/m1", None),
    ("model_audit", "GET", PERSONAL, "tags", None),
    # scripts/model_cron.py
    ("model_cron", "PATCH", PERSONAL, "mental-models/m1", {"trigger": {}}),
    # verify/gate2_banks.sh (팀 뱅크는 #473 a부터 운영 경로로 프로비저닝, 게이트웨이 PUT은 403 확인용:
    # tests/test_rest_bank_put_owner.py)
    ("gate2", "PUT", PERSONAL, "", {"name": PERSONAL}),
    # verify/gate2·3·4·5·6·8, gate_contract_drift
    ("verify-recall", "POST", PERSONAL, "memories/recall", {"query": "x"}),
    ("verify-recall", "POST", TEAM, "memories/recall", {"query": "x"}),
    ("verify-retain", "POST", PERSONAL, "memories", {"items": [{"content": "x"}]}),
    ("verify-retain", "POST", TEAM, "memories", {"items": [{"content": "x"}]}),
    # verify/gate7_mcp_enforce.sh
    ("gate7", "GET", PERSONAL, "config", None),
]


@pytest.mark.parametrize("who, method, bank, sub, payload", EXISTING,
                         ids=[f"{w}:{m} {b} {s or '<root>'}" for w, m, b, s, _p in EXISTING])
def test_existing_rest_callers_still_pass(client, recorder, who, method, bank, sub, payload):
    r = rest(client, method, bank, sub, payload)
    assert r.status_code == 200, (who, r.text)
    assert len(recorder.calls) == 1


def test_verify_gate5_destructive_on_team_still_403(client, recorder):
    """verify/gate5_adversarial.sh destructive-on-team: DELETE team memories/<id>는 계속 403."""
    r = rest(client, "DELETE", TEAM, "memories/some-id")
    assert r.status_code == 403
    assert recorder.calls == []
