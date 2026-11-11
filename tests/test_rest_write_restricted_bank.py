"""이슈 #473 (b): upstream 뱅크 설정 `mcp_enabled_tools`로 쓰기를 막은 뱅크에 REST `POST memories`가 닿지 않는다.

upstream은 이 설정을 MCP 도구 호출에만 건다(hindsight_api/mcp_tools.py `_apply_bank_tool_filtering`). REST
retain 경로(hindsight_api/api/http.py `POST /v1/default/banks/{bank_id}/memories`)는 이 설정을 보지 않으므로,
MCP로는 retain이 막힌 보관 뱅크에 REST로는 쓸 수 있었다. 게이트웨이는 REST retain마다 같은 출처(upstream
`GET .../config`의 `config.mcp_enabled_tools`)를 조회해 같은 판정을 낸다: 목록에 `retain`이 없으면 거부,
null이면 허용, 조회 실패·시간 초과·알 수 없는 모양은 거부(fail-closed). 캐시는 없다(요청마다 조회).

모의 upstream만 쓴다. 판정 행렬: 설정 값/조회 결과 x 뱅크(개인/팀).
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, UPSTREAM_KEY, load_rest_fixture
from gateway.acl import ACL
from gateway.app import BANK_CONFIG_LOOKUP_HEADER, create_app
from gateway.audit import Audit

PERSONAL = "personal-alice"
TEAM = "team-eng"
BANKS = [PERSONAL, TEAM]
ITEMS = {"items": [{"content": "[note] demo write", "tags": ["kind:note"]}]}
RETAIN_OK = load_rest_fixture("memories_post_ok")["response"]


def config_body(bank, value):
    """upstream GET /config 응답(fixtures/rest/bank_config.json 모양)의 mcp_enabled_tools만 바꾼 꼴."""
    body = json.loads(json.dumps(load_rest_fixture("bank_config")["response"]))
    body["bank_id"] = bank
    body["config"]["mcp_enabled_tools"] = value
    return body


class Upstream:
    """경로별로 답하는 모의 upstream. config_reply: (상태, 본문) 또는 예외 인스턴스."""

    def __init__(self, config_reply):
        self.config_reply = config_reply
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        name, value = BANK_CONFIG_LOOKUP_HEADER
        lookup = request.headers.get(name) == value
        self.calls.append({"method": request.method, "path": request.url.path, "lookup": lookup,
                           "headers": {k.lower(): v for k, v in request.headers.items()}})
        if lookup:
            if isinstance(self.config_reply, Exception):
                raise self.config_reply
            status, body = self.config_reply
            content = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            return httpx.Response(status, content=content, headers={"content-type": "application/json"})
        return httpx.Response(200, content=json.dumps(RETAIN_OK).encode("utf-8"),
                              headers={"content-type": "application/json"})

    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]

    def config_gets(self):
        return [c for c in self.calls if c["lookup"]]


def make(tmp_path, config_reply):
    up = Upstream(config_reply)
    client = httpx.AsyncClient(transport=httpx.MockTransport(up.handler), base_url="http://upstream", timeout=5.0)
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=client)
    return TestClient(app), up


def post_memories(c, bank, body=ITEMS, token="tok-alice"):
    return c.post(f"/v1/default/banks/{bank}/memories",
                  headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
                  content=json.dumps(body))


def audit_lines(tmp_path):
    p = tmp_path / "audit.log"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


# --- 쓰기 제한 뱅크: 거부, upstream POST 0회(설정 조회 GET 1회만) -------------------------------
RESTRICTED = [["recall", "reflect"], [], ["sync_retain", "recall"]]


@pytest.mark.parametrize("bank", BANKS)
@pytest.mark.parametrize("enabled", RESTRICTED, ids=["recall-reflect", "empty-list", "sync_retain-only"])
def test_rest_retain_on_a_write_restricted_bank_is_denied_with_zero_upstream_posts(tmp_path, bank, enabled):
    c, up = make(tmp_path, (200, config_body(bank, enabled)))
    r = post_memories(c, bank)
    assert r.status_code == 403, r.text
    assert r.json()["reason"] == "blocked-by-mcp-enabled-tools:retain"
    assert up.posts() == []
    assert [g["path"] for g in up.config_gets()] == [f"/v1/default/banks/{bank}/config"]
    deny = [ln for ln in audit_lines(tmp_path) if ln.get("decision") == "deny"]
    assert [ln["reason"] for ln in deny] == ["blocked-by-mcp-enabled-tools:retain"]


# --- 설정 조회 실패: 거부(fail-closed), upstream POST 0회 ---------------------------------------
LOOKUP_FAILURES = [
    ("http-500", (500, {"detail": "boom"})),
    ("http-404", (404, {"detail": "bank not found"})),
    ("http-401", (401, {"detail": "unauthorized"})),
    ("timeout", httpx.ReadTimeout("slow")),
    ("connect-error", httpx.ConnectError("down")),
    ("not-json", (200, b"<html>not json</html>")),
    ("no-config-object", (200, {"bank_id": "x"})),
    ("config-not-object", (200, {"bank_id": "x", "config": ["retain"]})),
    ("key-missing", (200, {"bank_id": "x", "config": {"recall_max_tokens": 2048}})),
    ("value-is-string", (200, {"bank_id": "x", "config": {"mcp_enabled_tools": "retain"}})),
    ("non-string-entry", (200, {"bank_id": "x", "config": {"mcp_enabled_tools": [["retain"]]}})),
]


@pytest.mark.parametrize("bank", BANKS)
@pytest.mark.parametrize("case, reply", LOOKUP_FAILURES, ids=[c for c, _r in LOOKUP_FAILURES])
def test_rest_retain_is_denied_when_the_config_lookup_fails(tmp_path, bank, case, reply):
    c, up = make(tmp_path, reply)
    r = post_memories(c, bank)
    assert r.status_code == 503, (case, r.text)
    assert r.json()["reason"].startswith("bank-config-lookup-failed:"), r.text
    assert up.posts() == []
    assert len(up.config_gets()) == 1


@pytest.mark.parametrize("bank", BANKS)
def test_a_bank_upstream_has_not_created_is_denied_with_a_reason_that_names_it(tmp_path, bank):
    """final-review #473 Bugs 2: 뱅크 없음 404(upstream 본문 정확 일치)도 거부(fail-closed)하되, 사유로 원인을 알린다."""
    c, up = make(tmp_path, (404, {"detail": f"Bank '{bank}' not found"}))
    r = post_memories(c, bank)
    assert r.status_code == 503, r.text
    assert r.json()["reason"] == "bank-config-lookup-failed:bank-not-provisioned"
    assert up.posts() == []


# --- 제한 없는 뱅크: 통과, 조회 1회 + POST 1회 ------------------------------------------------
UNRESTRICTED = [None, ["retain"], ["recall", "retain", "reflect"]]


@pytest.mark.parametrize("bank", BANKS)
@pytest.mark.parametrize("enabled", UNRESTRICTED, ids=["null", "retain-only", "retain-among-others"])
def test_rest_retain_on_an_unrestricted_bank_passes(tmp_path, bank, enabled):
    c, up = make(tmp_path, (200, config_body(bank, enabled)))
    r = post_memories(c, bank)
    assert r.status_code == 200, r.text
    assert [(x["method"], x["path"]) for x in up.calls] == [
        ("GET", f"/v1/default/banks/{bank}/config"), ("POST", f"/v1/default/banks/{bank}/memories")]


def test_config_lookup_uses_the_upstream_key_and_never_the_client_token(tmp_path):
    c, up = make(tmp_path, (200, config_body(PERSONAL, None)))
    assert post_memories(c, PERSONAL).status_code == 200
    g = up.config_gets()[0]
    assert (g["method"], g["path"]) == ("GET", f"/v1/default/banks/{PERSONAL}/config")
    assert g["headers"]["authorization"] == f"Bearer {UPSTREAM_KEY}"
    assert "tok-alice" not in json.dumps(g["headers"])


def test_no_cache_every_rest_retain_looks_the_config_up_again(tmp_path):
    """캐시 없음: 같은 뱅크에 연속 3회 쓰면 조회도 3회, 그사이 설정이 바뀌면 바로 따른다."""
    c, up = make(tmp_path, (200, config_body(TEAM, None)))
    assert post_memories(c, TEAM).status_code == 200
    assert post_memories(c, TEAM).status_code == 200
    up.config_reply = (200, config_body(TEAM, ["recall"]))
    assert post_memories(c, TEAM).status_code == 403
    assert len(up.config_gets()) == 3
    assert len(up.posts()) == 2


def test_the_restriction_runs_before_the_retain_rules(tmp_path):
    """권한 판정이 먼저다: 규칙 위반 본문도 제한 뱅크에서는 403(422 규칙 안내가 아니다)."""
    c, up = make(tmp_path, (200, config_body(TEAM, ["recall"])))
    r = post_memories(c, TEAM, body={"items": [{"content": "no label", "tags": ["BAD TAG"]}]})
    assert r.status_code == 403
    assert up.posts() == []


@pytest.mark.parametrize("method, sub, body", [
    ("POST", "memories/recall", {"query": "q"}),
    ("GET", "memories/list", None),
    ("GET", "config", None),
])
def test_reads_on_a_write_restricted_bank_do_not_look_the_config_up(tmp_path, method, sub, body):
    """쓰기 제한 조회는 retain(쓰기)에만 건다. 읽기는 전과 같이 upstream 1회."""
    c, up = make(tmp_path, (200, config_body(TEAM, ["recall"])))
    r = c.request(method, f"/v1/default/banks/{TEAM}/{sub}",
                  headers={"authorization": "Bearer tok-alice", "content-type": "application/json"},
                  content=(json.dumps(body) if body is not None else None))
    assert r.status_code == 200, r.text
    assert len(up.calls) == 1 and up.config_gets() == []
