"""완료 retain 무효화 훅 (issue #321, 플래그 HC_ITEM_INVALIDATE 기본 꺼짐), mock upstream 만 쓴다.

고정하는 계약(행렬: 플래그 x 표면 MCP retain·sync_retain / REST POST .../memories x 경우):
  (a) 꺼짐: retain 응답 바이트·상태 코드가 훅이 없던 때와 같고 후보 조회·PATCH 호출이 0회다.
  (b) 켜짐 + 키 있는 kind:completion: 저장 확인 뒤 대상에만 {"state": "invalidated"} PATCH 가 가고 감사 줄이 남는다.
      응답 바이트는 꺼짐과 같다.
  (c) completion 아님 / 키 없음 / 팀 뱅크 / 저장 미확인 / over_cap: PATCH 0회.
  (d) 후보 조회·PATCH 실패: retain 응답은 같고 감사 줄에 실패가 남는다.
  감사 줄·fixture 에 기억 본문·retain 본문이 없다(본문 자리에 BODY-SECRET 표식을 넣고 감사 파일에 없음을 본다).
"""
import json
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, UPSTREAM_KEY, bank_config_reply
from gateway import gate_effects, item_invalidate, item_keys
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit
from gateway.bank_policies import BankPolicies

BANK = "personal-alice"
TEAM = "team-eng"
PROJECT = "project:hindsight-crew"
DONE_TEXT = "[완료] hindsight-crew#251 정리 끝 BODY-SECRET-DONE"
RETAIN_TS = "2026-10-20T10:00:00Z"
TARGET_ID = "11111111-1111-4111-8111-111111111111"
SURFACES = ["mcp-retain", "mcp-sync_retain", "rest"]
RETAIN_MODES = ["sync", "async-ok"]


# 훅 도입 전 main(7bb97b4)에서 얻은 retain 응답. 꺼짐 불변 테스트가 이 바이트와 비교한다.
GOLDEN = {
    ("sync", "mcp"): (200, (
        b'event: message\r\ndata: {"jsonrpc": "2.0", "id": 7, "result": {"isError": false, "structuredContent": '
        b'{"status": "accepted", "memory_ids": ["new-1"]}, "content": [{"type": "text", "text": '
        b'"{\\"status\\": \\"accepted\\", \\"memory_ids\\": [\\"new-1\\"]}"}]}}\r\n\r\n')),
    ("sync", "rest"): (200, b'{"success":true,"items_count":1}'),
    ("async-ok", "mcp"): (200, (
        b'event: message\r\ndata: {"jsonrpc":"2.0","id":7,"result":{"isError":false,"structuredContent":'
        b'{"status":"accepted","operation_id":"op-1"},"content":[{"type":"text","text":'
        b'"{\\"status\\": \\"accepted\\", \\"operation_id\\": \\"op-1\\"}"},{"type":"text","text":'
        b'"[gateway: async retain - operation_id=op-1, get_operation\xec\x9c\xbc\xeb\xa1\x9c '
        b'\xec\x99\x84\xeb\xa3\x8c\xeb\xa5\xbc \xed\x99\x95\xec\x9d\xb8\xed\x95\x98\xec\x84\xb8\xec\x9a\x94]"}]}}'
        b'\r\n\r\n')),
    ("async-ok", "rest"): (202, b'{"success":true,"async":true,"items_count":1,"operation_id":"op-1"}'),
}


def sse(payload):
    return b"event: message\r\ndata: " + json.dumps(payload).encode("utf-8") + b"\r\n\r\n"


def row(mem_id, *, tags=("kind:handoff", PROJECT), text="[인계] hindsight-crew#251 남은 일 BODY-SECRET-OPEN",
        state="valid", at="2026-10-10T10:00:00Z", fact_type="world", doc="doc-old"):
    out = {"id": mem_id, "text": text, "fact_type": fact_type, "tags": list(tags), "mentioned_at": at,
           "document_id": doc}
    if state is not None:
        out["state"] = state
    return out


class InvUpstream:
    """retain 응답(방식별), memories/list·recall·GET memory·operations·PATCH 를 흉내 내는 mock upstream."""

    def __init__(self, *, rows=(), retain_mode="sync", list_status=200, patch_status=200,
                 recall_rows=(), details=None, list_total=None, patch_raises=False):
        self.rows = list(rows)
        self.retain_mode = retain_mode
        self.list_status = list_status
        self.patch_status = patch_status
        self.recall_rows = list(recall_rows)
        self.details = details or {}
        self.list_total = list_total
        self.patch_raises = patch_raises
        self.calls = []

    # -- retain answers ---------------------------------------------------------------
    def retain_payload(self):
        if self.retain_mode == "sync":
            return {"status": "accepted", "memory_ids": ["new-1"]}
        if self.retain_mode in ("async-ok", "async-failed"):
            return {"status": "accepted", "operation_id": "op-1"}
        return {"status": "failed"}                              # not-stored

    def mcp_result(self, el_id):
        payload = self.retain_payload()
        failed = self.retain_mode == "not-stored"
        return {"jsonrpc": "2.0", "id": el_id, "result": {
            "isError": failed, "structuredContent": payload,
            "content": [{"type": "text", "text": json.dumps(payload)}]}}

    def handler(self, request):
        if (config := bank_config_reply(request)) is not None:   # #473 b upstream config read
            return config
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        query = parse_qsl(request.url.query.decode() if isinstance(request.url.query, bytes)
                          else str(request.url.query))
        self.calls.append({"method": request.method, "path": path, "query": query, "body": body})
        if request.method == "GET" and "/operations/" in path:
            ok = self.retain_mode != "async-failed"
            return httpx.Response(200, json={
                "operation_id": path.rsplit("/", 1)[1], "status": "completed" if ok else "failed",
                "result_metadata": {"unit_ids_count": 1, "extraction_errors_count": 0},
                "child_operations": None})
        if request.method == "GET" and path.endswith("/memories/list"):
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={"detail": "synthetic"})
            total = self.list_total if self.list_total is not None else len(self.rows)
            return httpx.Response(200, json={"items": self.rows, "total": total, "limit": 100, "offset": 0})
        if request.method == "POST" and path.endswith("/memories/recall"):
            return httpx.Response(200, json={"results": self.recall_rows})
        if request.method == "GET" and "/memories/" in path:
            detail = self.details.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=detail) if detail else httpx.Response(404, json={})
        if request.method == "PATCH" and "/memories/" in path:
            if self.patch_raises:
                raise httpx.ConnectError("synthetic", request=request)
            return httpx.Response(self.patch_status, json={"id": path.rsplit("/", 1)[1]})
        if path == "/mcp" and request.method == "POST":
            return httpx.Response(200, content=sse(self.mcp_result(body.get("id"))),
                                  headers={"content-type": "text/event-stream", "mcp-session-id": "s1"})
        if request.method == "POST" and path.endswith("/memories"):
            return self.rest_response()
        return httpx.Response(404, json={})

    def rest_response(self):
        if self.retain_mode == "not-stored":
            return httpx.Response(200, json={"success": False, "status": "failed"})
        if self.retain_mode == "sync":
            return httpx.Response(200, json={"success": True, "items_count": 1})
        return httpx.Response(202, json={"success": True, "async": True, "items_count": 1,
                                         "operation_id": "op-1"})

    def methods(self, method):
        return [c for c in self.calls if c["method"] == method]


def subdir(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def make_app(up, tmp_path, *, bank_policies=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(up.handler), base_url="http://upstream", timeout=5.0)
    log = tmp_path / "audit.log"
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(log)), upstream_key=UPSTREAM_KEY,
                     http_client=http, bank_policies=bank_policies)
    return TestClient(app), log


def send(client, surface, content=DONE_TEXT, tags=("kind:completion", PROJECT), bank=BANK):
    item = {"content": content, "tags": list(tags), "timestamp": RETAIN_TS}
    headers = {"authorization": "Bearer tok-alice", "content-type": "application/json"}
    if surface == "rest":
        return client.post(f"/v1/default/banks/{bank}/memories", json={"items": [item]}, headers=headers)
    tool = surface.split("-", 1)[1]
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": tool, "arguments": item}}
    return client.post(f"/mcp/{bank}/", json=body, headers=headers)


def audit_rows(log, decision=item_invalidate.DECISION):
    if not log.exists():
        return []
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r["decision"] == decision]


def set_flag(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(item_invalidate.ENV_FLAG, raising=False)
    else:
        monkeypatch.setenv(item_invalidate.ENV_FLAG, value)


def snapshot(resp):
    return resp.status_code, resp.content, resp.headers.get("mcp-session-id"), resp.headers.get("content-type")


def target_rows(extra=()):
    """대상 1건 + 대상이 아닌 이웃들. 대상은 TARGET_ID 하나여야 한다."""
    return [
        row(TARGET_ID),
        row("completion-twin", tags=("kind:completion", PROJECT)),             # 완료는 대상 아님
        row("other-key", text="[인계] hindsight-crew#999 다른 일"),             # 키가 다름
        row("newer", at="2026-10-21T10:00:00Z"),                               # 완료보다 늦음
        row("already-invalidated", state="invalidated"),                      # 이미 무효
        row("no-state", state=None),                                          # state 없음은 비대상
        row("observation", fact_type="observation"),                          # 관찰은 후보가 아니다
        row("other-project", tags=("kind:handoff", "project:other")),          # 다른 project
        row("plain-fact", tags=("kind:decision", PROJECT), text="[결정] hindsight-crew#251 방식 채택"),
        *extra,
    ]


# --- (a) 꺼짐 불변 -------------------------------------------------------------------------------

@pytest.mark.parametrize("retain_mode", RETAIN_MODES)
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("flag", [None, "", "0", "false", "off"])
def test_flag_off_retain_unchanged(tmp_path, monkeypatch, flag, surface, retain_mode):
    set_flag(monkeypatch, flag)
    up = InvUpstream(rows=target_rows(), retain_mode=retain_mode)
    client, log = make_app(up, tmp_path)
    with client:
        resp = send(client, surface)
    # 훅이 없던 main(origin/main 7bb97b4)에서 같은 요청이 낸 응답 바이트(golden)와 같다.
    status, golden = GOLDEN[(retain_mode, "rest" if surface == "rest" else "mcp")]
    assert (resp.status_code, resp.content) == (status, golden)
    # upstream 호출열 전체(메서드·경로·순서·횟수)도 훅 도입 전 main 과 같다: 전달된 retain 한 건뿐이다.
    expected_calls = ([("POST", f"/v1/default/banks/{BANK}/memories")] if surface == "rest"
                      else [("POST", "/mcp")])
    assert [(c["method"], c["path"]) for c in up.calls] == expected_calls
    assert audit_rows(log) == []


# --- (b) 켜짐: 대상에만 PATCH ---------------------------------------------------------------------

def expected_reason(retain_mode):
    # 문서 id 가 없는 retain 이라 완료 쪽 출처는 operation id(async) 또는 unknown(sync)
    return "item-keys-v1 completion=" + ("op-1" if retain_mode == "async-ok" else "unknown")


@pytest.mark.parametrize("retain_mode", RETAIN_MODES)
@pytest.mark.parametrize("surface", SURFACES)
def test_flag_on_invalidates_only_targets_and_response_is_unchanged(tmp_path, monkeypatch, surface, retain_mode):
    set_flag(monkeypatch, None)
    off_client, _ = make_app(InvUpstream(rows=target_rows(), retain_mode=retain_mode), subdir(tmp_path, "off"))
    with off_client:
        off = send(off_client, surface)

    set_flag(monkeypatch, "1")
    up = InvUpstream(rows=target_rows(), retain_mode=retain_mode)
    client, log = make_app(up, subdir(tmp_path, "on"))
    with client:
        on = send(client, surface)
    assert snapshot(on) == snapshot(off)                       # 응답 바이트·상태·헤더 불변
    patches = up.methods("PATCH")
    assert [p["path"].rsplit("/", 1)[1] for p in patches] == [TARGET_ID]
    assert patches[0]["body"] == {"state": "invalidated", "reason": expected_reason(retain_mode)}
    lists = [c for c in up.calls if c["path"].endswith("/memories/list")]
    assert len(lists) == 2                                    # 본문 키 조회(q) + item 태그 조회
    assert ("q", "#251") in lists[0]["query"] and ("state", "valid") in lists[0]["query"]
    assert ("tags", PROJECT) in lists[0]["query"]
    # 저장 확인(async 는 operation 완료) 뒤에 PATCH 가 간다
    order = [(c["method"], c["path"].rsplit("/", 1)[1]) for c in up.calls]
    if retain_mode == "async-ok":
        assert order.index(("GET", "op-1")) < order.index(("PATCH", TARGET_ID))
    line, = audit_rows(log)
    ext = line["item_invalidate"]
    assert line["reason"] == "invalidated" and line["resolved_bank"] == BANK
    assert ext["rule_version"] == item_keys.RULE_VERSION and ext["lookup"] == "list"
    assert ext["keys"] == ["hindsight-crew#251"] and ext["target_ids"] == [TARGET_ID]
    assert ext["invalidated_ids"] == [TARGET_ID] and ext["over_cap"] is False
    assert ext["patch_status"] == {TARGET_ID: 200} and ext["lookup_status"] == [200, 200]
    assert ext["operation_id"] == (["op-1"] if retain_mode == "async-ok" else [])
    raw = log.read_text(encoding="utf-8")
    assert "BODY-SECRET" not in raw and UPSTREAM_KEY not in raw


def test_alias_table_from_the_bank_policy_joins_spellings(tmp_path, monkeypatch):
    set_flag(monkeypatch, "1")
    policies = BankPolicies.from_dict({"defaults": {}, "banks": {BANK: {"item_repo_aliases": {
        "hc": "hindsight-crew", "hindsight-crew": "hindsight-crew"}}}})
    up = InvUpstream(rows=[row(TARGET_ID, text="[인계] hindsight-crew#251 남은 일")])
    client, log = make_app(up, tmp_path, bank_policies=policies)
    with client:
        send(client, "rest", content="[완료] HC#251 끝", tags=("kind:completion", PROJECT))
    assert [p["path"].rsplit("/", 1)[1] for p in up.methods("PATCH")] == [TARGET_ID]
    assert audit_rows(log)[0]["item_invalidate"]["keys"] == ["hindsight-crew#251"]


# --- (c) PATCH 0회 -------------------------------------------------------------------------------

def over_cap_rows():
    return [row(f"cap-{i}") for i in range(item_keys.MAX_TARGETS_PER_COMPLETION + 1)]


NO_PATCH_CASES = {
    "not-completion": dict(tags=("kind:decision", PROJECT), reason=None),
    "no-key": dict(content="[완료] 이름 없는 작업 정리 끝", reason=None),
    "no-project-tag": dict(content="[완료] Bkankim/hindsight-crew#251 끝", tags=("kind:completion",),
                           reason="skip:no-project-tag"),
    "not-stored": dict(retain_mode="not-stored", reason=None),
    "async-failed": dict(retain_mode="async-failed", reason="skip:retain-not-completed"),
    "over-cap": dict(rows=over_cap_rows, reason="over-cap"),
}


@pytest.mark.parametrize("case", sorted(NO_PATCH_CASES))
@pytest.mark.parametrize("surface", SURFACES)
def test_no_patch_when_not_eligible_or_not_confirmed_or_over_cap(tmp_path, monkeypatch, surface, case):
    set_flag(monkeypatch, "1")
    spec = NO_PATCH_CASES[case]
    rows = spec["rows"]() if "rows" in spec else target_rows()
    up = InvUpstream(rows=rows, retain_mode=spec.get("retain_mode", "sync"))
    client, log = make_app(up, tmp_path)
    send_kwargs = {k: spec[k] for k in ("content", "tags") if k in spec}
    with client:
        send(client, surface, **send_kwargs)
    assert up.methods("PATCH") == []
    lines = audit_rows(log)
    if spec["reason"] is None:
        assert lines == [] and not [c for c in up.calls if c["path"].endswith("/memories/list")]
    else:
        assert [ln["reason"] for ln in lines] == [spec["reason"]]
    if case == "over-cap":
        ext = lines[0]["item_invalidate"]
        assert ext["over_cap"] is True and len(ext["target_ids"]) == item_keys.MAX_TARGETS_PER_COMPLETION + 1
        assert ext["invalidated_ids"] == []


@pytest.mark.parametrize("surface", ["mcp-retain", "mcp-sync_retain", "rest"])
def test_team_bank_never_looks_up_or_patches(tmp_path, monkeypatch, surface):
    set_flag(monkeypatch, "1")
    up = InvUpstream(rows=target_rows())
    client, log = make_app(up, tmp_path)
    with client:
        resp = send(client, surface, bank=TEAM)
    assert resp.status_code == 200
    assert up.methods("PATCH") == [] and not [c for c in up.calls if c["path"].endswith("/memories/list")]
    assert audit_rows(log) == []


# --- (d) 실패는 fail-open ------------------------------------------------------------------------

FAILURES = {
    "list-503": (dict(list_status=503), "lookup-failed:http-503"),
    "list-bad-shape": (dict(rows=None), "lookup-failed:bad-shape"),
    "list-truncated": (dict(list_total=10 ** 6), "lookup-failed:truncated"),
    "patch-500": (dict(patch_status=500), "patch-failed"),
    "patch-transport": (dict(patch_raises=True), "patch-failed"),
}


class BadShapeUpstream(InvUpstream):
    def handler(self, request):
        if request.method == "GET" and request.url.path.endswith("/memories/list"):
            self.calls.append({"method": "GET", "path": request.url.path, "query": [], "body": None})
            return httpx.Response(200, json={"unexpected": True})
        return super().handler(request)


@pytest.mark.parametrize("failure", sorted(FAILURES))
@pytest.mark.parametrize("surface", SURFACES)
def test_upstream_failure_keeps_retain_response_and_audits_it(tmp_path, monkeypatch, surface, failure):
    kwargs, reason = FAILURES[failure]
    kwargs = dict(kwargs)
    cls = InvUpstream
    if "rows" in kwargs:
        cls = BadShapeUpstream
        kwargs.pop("rows")
    set_flag(monkeypatch, None)
    base_client, _ = make_app(cls(rows=target_rows()), subdir(tmp_path, "base"))
    with base_client:
        base = send(base_client, surface)

    set_flag(monkeypatch, "1")
    up = cls(rows=target_rows(), **kwargs)
    client, log = make_app(up, subdir(tmp_path, "on"))
    with client:
        resp = send(client, surface)
    assert snapshot(resp) == snapshot(base)
    line, = audit_rows(log)
    assert line["reason"] == reason
    ext = line["item_invalidate"]
    if reason == "patch-failed":
        assert ext["invalidated_ids"] == []
        assert ext["patch_status"] == {TARGET_ID: 500 if failure == "patch-500" else "transport"}
    else:
        assert ext["target_ids"] == [] and up.methods("PATCH") == []
    assert "BODY-SECRET" not in log.read_text(encoding="utf-8")


def test_a_failed_effect_task_never_changes_the_response(tmp_path, monkeypatch):
    set_flag(monkeypatch, "1")

    async def boom(**_kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(item_invalidate, "execute", boom)
    up = InvUpstream(rows=target_rows())
    client, log = make_app(up, tmp_path)
    with client:
        resp = send(client, "rest")
    assert resp.status_code == 200 and up.methods("PATCH") == []
    assert [ln["reason"] for ln in audit_rows(log)] == ["failed:RuntimeError"]


def test_effects_backlog_skip_is_audited_without_calls(tmp_path, monkeypatch):
    set_flag(monkeypatch, "1")
    monkeypatch.setattr(gate_effects, "_active", gate_effects.MAX_ACTIVE_FINALIZE)
    up = InvUpstream(rows=target_rows())
    client, log = make_app(up, tmp_path)
    with client:
        send(client, "rest")
    assert up.methods("PATCH") == []
    assert [ln["reason"] for ln in audit_rows(log)] == ["skip:effects-backlog"]


# --- recall 방식: 찾기만 recall, 상태는 GET 으로 읽는다 ---------------------------------------------

def recall_row(mem_id):
    return {"id": mem_id, "text": "BODY-SECRET-RECALL", "type": "world", "tags": ["kind:handoff", PROJECT]}


@pytest.mark.parametrize("surface", SURFACES)
def test_recall_lookup_hydrates_candidates_with_get(tmp_path, monkeypatch, surface):
    set_flag(monkeypatch, "1")
    monkeypatch.setenv(item_invalidate.ENV_LOOKUP, "recall")
    details = {TARGET_ID: row(TARGET_ID),
               "gone": row("gone", state="invalidated"),
               "broken-get": None}
    up = InvUpstream(recall_rows=[recall_row(TARGET_ID), recall_row("gone"), recall_row("broken-get")],
                     details={k: v for k, v in details.items() if v})
    client, log = make_app(up, tmp_path)
    with client:
        send(client, surface)
    recalls = [c for c in up.calls if c["path"].endswith("/memories/recall")]
    assert len(recalls) == 1
    assert recalls[0]["body"]["query"] == "hindsight-crew#251" and recalls[0]["body"]["tags"] == [PROJECT]
    assert not [c for c in up.calls if c["path"].endswith("/memories/list")]
    assert [p["path"].rsplit("/", 1)[1] for p in up.methods("PATCH")] == [TARGET_ID]
    ext = audit_rows(log)[0]["item_invalidate"]
    assert ext["lookup"] == "recall" and ext["recall_hits"] == 3 and ext["hydrate_failed"] == 1
    assert ext["candidates"] == 2 and ext["hydrate_status"] == [200, 200, 404]
    assert "BODY-SECRET" not in log.read_text(encoding="utf-8")


def test_recall_rows_without_state_are_never_targets(tmp_path, monkeypatch):
    set_flag(monkeypatch, "1")
    monkeypatch.setenv(item_invalidate.ENV_LOOKUP, "recall")
    detail = row(TARGET_ID, state=None)                      # GET 응답에도 state 가 없으면 지어 넣지 않는다
    up = InvUpstream(recall_rows=[recall_row(TARGET_ID)], details={TARGET_ID: detail})
    client, log = make_app(up, tmp_path)
    with client:
        send(client, "rest")
    assert up.methods("PATCH") == []
    assert [ln["reason"] for ln in audit_rows(log)] == ["no-targets"]


# --- 최종 리뷰 반영(#321-1): 계획 상한·hydrate 상한·skip 줄 필드·item 태그 전용 후보 --------------------------

def test_completions_over_the_plan_cap_leave_a_skip_plan_cap_audit_line(tmp_path, monkeypatch):
    set_flag(monkeypatch, "1")
    up = InvUpstream(rows=target_rows(), retain_mode="async-ok")
    client, log = make_app(up, tmp_path)
    items = [{"content": f"[완료] hindsight-crew#{250 + i} 끝 BODY-SECRET-DONE", "tags": ["kind:completion", PROJECT],
              "timestamp": RETAIN_TS} for i in range(item_invalidate.MAX_PLANS_PER_REQUEST + 2)]
    with client:
        resp = client.post(f"/v1/default/banks/{BANK}/memories", json={"items": items},
                           headers={"authorization": "Bearer tok-alice"})
    assert resp.status_code == 202
    lines = audit_rows(log)
    assert len(lines) == len(items)                                         # 완료마다 줄 하나, 조용히 빠지는 것 없음
    capped = [ln for ln in lines if ln["reason"] == "skip:plan-cap"]
    assert [ln["item_invalidate"]["keys"] for ln in capped] == [["hindsight-crew#253"], ["hindsight-crew#254"]]
    for ln in capped:
        ext = ln["item_invalidate"]
        assert ext["operation_id"] == ["op-1"] and ext["rule_version"] == item_keys.RULE_VERSION
    assert "BODY-SECRET" not in log.read_text(encoding="utf-8")


def test_recall_hits_over_the_hydrate_cap_invalidate_nothing(tmp_path, monkeypatch):
    set_flag(monkeypatch, "1")
    monkeypatch.setenv(item_invalidate.ENV_LOOKUP, "recall")
    hits = [recall_row(TARGET_ID)] + [recall_row(f"hit-{i}") for i in range(item_invalidate.MAX_HYDRATE)]
    up = InvUpstream(recall_rows=hits, details={TARGET_ID: row(TARGET_ID)})
    client, log = make_app(up, tmp_path)
    with client:
        send(client, "rest")
    assert up.methods("PATCH") == []
    assert not [c for c in up.calls if c["method"] == "GET" and "/memories/" in c["path"]
                and not c["path"].endswith("/list")]                         # 잘린 목록으로 채우기·판정을 시작하지 않는다
    line, = audit_rows(log)
    assert line["reason"] == "skip:hydrate-cap"
    assert line["item_invalidate"]["recall_hits"] == len(hits) and line["item_invalidate"]["target_ids"] == []


SKIP_FIELDS = {"transport", "rule_version", "keys", "operation_id"}


@pytest.mark.parametrize("surface", SURFACES)
def test_skip_lines_carry_keys_operation_id_and_rule_version(tmp_path, monkeypatch, surface):
    set_flag(monkeypatch, "1")
    up = InvUpstream(rows=target_rows(), retain_mode="async-failed")
    client, log = make_app(up, subdir(tmp_path, "notstored"))
    with client:
        send(client, surface)
    line, = audit_rows(log)
    ext = line["item_invalidate"]
    assert line["reason"] == "skip:retain-not-completed" and SKIP_FIELDS <= set(ext)
    assert ext["keys"] == ["hindsight-crew#251"] and ext["operation_id"] == ["op-1"]
    assert ext["rule_version"] == item_keys.RULE_VERSION

    monkeypatch.setattr(gate_effects, "_active", gate_effects.MAX_ACTIVE_FINALIZE)
    up = InvUpstream(rows=target_rows(), retain_mode="sync")
    client, log = make_app(up, subdir(tmp_path, "backlog"))
    with client:
        send(client, surface)
    line, = audit_rows(log)
    ext = line["item_invalidate"]
    assert line["reason"] == "skip:effects-backlog" and SKIP_FIELDS <= set(ext)
    assert ext["keys"] == ["hindsight-crew#251"] and ext["operation_id"] is None      # 없는 값은 null
    assert ext["rule_version"] == item_keys.RULE_VERSION


class FilteringUpstream(InvUpstream):
    """memories/list 의 q(본문 부분 일치)·tags·tags_match(any / all_strict)를 흉내 낸다(색인 없는 전수 스캔)."""

    def handler(self, request):
        if request.method == "GET" and request.url.path.endswith("/memories/list"):
            query = parse_qsl(request.url.query.decode() if isinstance(request.url.query, bytes)
                              else str(request.url.query))
            self.calls.append({"method": "GET", "path": request.url.path, "query": query, "body": None})
            q = next((v for k, v in query if k == "q"), None)
            tags = [v for k, v in query if k == "tags"]
            match = next((v for k, v in query if k == "tags_match"), "any")
            rows = [r for r in self.rows if q is None or q.lower() in r["text"].lower()]
            if tags and match == "all_strict":
                rows = [r for r in rows if set(tags) <= set(r["tags"])]
            elif tags and match == "any_strict":
                rows = [r for r in rows if set(tags) & set(r["tags"])]
            return httpx.Response(200, json={"items": rows, "total": len(rows), "limit": 100, "offset": 0})
        return super().handler(request)


def tag_only(mem_id, tag="item:hindsight-crew-251"):
    return row(mem_id, tags=("kind:handoff", PROJECT, tag), text="[인계] 남은 일 번호 표기 없음 BODY-SECRET-TAGONLY")


def test_item_tag_only_candidate_is_found_and_invalidated(tmp_path, monkeypatch):
    set_flag(monkeypatch, "1")
    up = FilteringUpstream(rows=[tag_only(TARGET_ID)])
    client, log = make_app(up, tmp_path)
    with client:
        send(client, "rest")
    assert [p["path"].rsplit("/", 1)[1] for p in up.methods("PATCH")] == [TARGET_ID]
    ext = audit_rows(log)[0]["item_invalidate"]
    assert ext["target_ids"] == [TARGET_ID] and ext["candidates"] == 1 and len(ext["lookup_status"]) == 2
    tag_queries = [c for c in up.calls if ("tags", "item:hindsight-crew-251") in c["query"]]
    assert len(tag_queries) == 1 and ("tags", PROJECT) in tag_queries[0]["query"]      # 같은 project 범위
    assert ("state", "valid") in tag_queries[0]["query"] and ("q", "#251") not in tag_queries[0]["query"]
    assert "BODY-SECRET" not in log.read_text(encoding="utf-8")


def test_item_tag_only_candidates_count_toward_over_cap(tmp_path, monkeypatch):
    set_flag(monkeypatch, "1")
    rows = [row("text-hit")] + [tag_only(f"tag-{i}") for i in range(item_keys.MAX_TARGETS_PER_COMPLETION)]
    up = FilteringUpstream(rows=rows)                         # 본문 후보 1 + 태그 전용 10 = 11 > 상한 10
    client, log = make_app(up, tmp_path)
    with client:
        send(client, "rest")
    assert up.methods("PATCH") == []
    line, = audit_rows(log)
    assert line["reason"] == "over-cap" and len(line["item_invalidate"]["target_ids"]) == 11


def test_item_tag_lookup_uses_every_alias_spelling_of_the_repo(tmp_path, monkeypatch):
    set_flag(monkeypatch, "1")
    policies = BankPolicies.from_dict({"defaults": {}, "banks": {BANK: {"item_repo_aliases": {
        "hc": "hindsight-crew", "hindsight-crew": "hindsight-crew"}}}})
    up = FilteringUpstream(rows=[tag_only(TARGET_ID, tag="item:hc-251")])
    client, log = make_app(up, tmp_path, bank_policies=policies)
    with client:
        send(client, "rest")
    assert [p["path"].rsplit("/", 1)[1] for p in up.methods("PATCH")] == [TARGET_ID]
    assert audit_rows(log)[0]["item_invalidate"]["target_ids"] == [TARGET_ID]


def test_unset_timestamp_literal_is_treated_as_missing_not_as_an_unparsable_time(tmp_path, monkeypatch):
    set_flag(monkeypatch, "1")
    policies = BankPolicies.from_dict({"defaults": {"timestamp_autofill": False}, "banks": {}})
    up = InvUpstream(rows=target_rows())
    client, log = make_app(up, tmp_path, bank_policies=policies)
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "retain", "arguments": {
        "content": DONE_TEXT, "tags": ["kind:completion", PROJECT], "timestamp": "unset"}}}
    with client:
        client.post(f"/mcp/{BANK}/", json=body, headers={"authorization": "Bearer tok-alice",
                                                          "content-type": "application/json"})
    # 완료 시각이 현재로 대체되므로 그보다 이른 열림 원문(TARGET_ID, "newer")이 대상이고, 0건이면 안 된다
    assert TARGET_ID in [p["path"].rsplit("/", 1)[1] for p in up.methods("PATCH")]


def test_audit_line_lists_are_capped_and_counts_are_kept(tmp_path, monkeypatch):
    set_flag(monkeypatch, "1")
    many = " ".join(f"hindsight-crew#{n}" for n in range(1, item_invalidate.MAX_KEYS + 40))
    up = InvUpstream(rows=target_rows())
    client, log = make_app(up, tmp_path)
    with client:
        send(client, "rest", content=f"[완료] {many} 끝")
    line, = audit_rows(log)
    ext = line["item_invalidate"]
    assert line["reason"] == "skip:too-many-keys"
    assert len(ext["keys"]) == item_invalidate.MAX_AUDIT_LIST and ext["keys_count"] == item_invalidate.MAX_KEYS + 39

    rows = [row(f"cap-{i}") for i in range(item_invalidate.MAX_AUDIT_LIST + 5)]
    up = InvUpstream(rows=rows)
    client, log = make_app(up, subdir(tmp_path, "overcap"))
    with client:
        send(client, "rest")
    ext = audit_rows(log)[0]["item_invalidate"]
    assert audit_rows(log)[0]["reason"] == "over-cap"
    assert len(ext["target_ids"]) == item_invalidate.MAX_AUDIT_LIST and ext["target_count"] == len(rows)


def test_list_lookup_scopes_by_project_with_any_strict_so_untagged_rows_are_excluded(tmp_path, monkeypatch):
    set_flag(monkeypatch, "1")
    untagged = row("untagged-old", tags=(), text="[인계] hindsight-crew#251 태그 없는 옛 기억")
    up = FilteringUpstream(rows=[row(TARGET_ID), untagged])
    client, log = make_app(up, tmp_path)
    with client:
        send(client, "rest")
    lists = [c for c in up.calls if c["path"].endswith("/memories/list")]
    assert lists and all(("tags_match", "any_strict") in c["query"] or ("tags_match", "all_strict") in c["query"]
                         for c in lists)
    assert ("tags_match", "any_strict") in lists[0]["query"] and ("tags", PROJECT) in lists[0]["query"]
    assert audit_rows(log)[0]["item_invalidate"]["candidates"] == 1          # 태그 없는 행은 후보에 안 들어온다


# --- 순수 도우미 -----------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("0", False), ("no", False), ("1", True), ("true", True), (" On ", True)])
def test_enabled_reads_the_flag_default_off(monkeypatch, value, expected):
    set_flag(monkeypatch, value)
    assert item_invalidate.enabled() is expected


@pytest.mark.parametrize("value,expected", [(None, "list"), ("recall", "recall"), ("LIST", "list"), ("bogus", "list")])
def test_lookup_mode_defaults_to_list(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(item_invalidate.ENV_LOOKUP, raising=False)
    else:
        monkeypatch.setenv(item_invalidate.ENV_LOOKUP, value)
    assert item_invalidate.lookup_mode() == expected


def test_prepare_returns_nothing_while_the_flag_is_off(monkeypatch):
    set_flag(monkeypatch, None)
    principal = ACL.from_dict(ACL_DATA).principal_for("tok-alice")
    items = [{"content": DONE_TEXT, "tags": ["kind:completion", PROJECT], "timestamp": RETAIN_TS}]
    assert item_invalidate.prepare_rest(items=items, bank=BANK, principal=principal, bank_policy=None) == {}
    set_flag(monkeypatch, "1")
    assert list(item_invalidate.prepare_rest(items=items, bank=BANK, principal=principal, bank_policy=None)) == [0]
    assert item_invalidate.prepare_rest(items=items, bank=TEAM, principal=principal, bank_policy=None) == {}
