"""상태 질의 보강 X1 (issue #339, 플래그 HC_ITEM_STATUS 기본 꺼짐), mock upstream 만 쓴다.

고정하는 계약(행렬: 경우 x 플래그/환경):
  (a) 꺼짐(환경변수 미설정·"0"·빈 값·알 수 없는 값): MCP recall 응답 바이트가 이 변경 이전(origin/main 77bca59)과 같고
      upstream 호출열이 recall 한 건뿐이다(찾기 호출 0회). 코드 기본값(`item_status.enabled()`)이 꺼짐이다.
  (b) 켜짐: 결과에 든 작업 키마다 `memories/list` 로 같은 키의 완료 계열 기억을 찾아 결과 끝에 덧붙인다.
      완료 계열만(kind:completion + 게이트웨이 별칭이 그것으로 바꾸는 kind), `query_timestamp` 이후 행 제외,
      시각 없는 행 제외(시각 인자가 있을 때), 이미 결과에 있는 id 제외, 키당 최신 1개, 키 상한,
      원래 행의 집합·순서·내용 불변, 덧붙인 행에 표시 필드.
  (c) 시간 초과·upstream 오류: 원래 응답 바이트 그대로(fail-open)이고 감사 줄에 사유가 남는다.
  (d) 범위 밖: REST recall, 오류 결과, 조회 범위(project 태그)가 없는 키는 건드리지 않는다.
감사 줄·fixture 에 기억 본문이 없다(본문 자리에 BODY-SECRET 표식을 넣고 감사 파일에 없음을 본다).
"""
import asyncio
import hashlib
import json
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, UPSTREAM_KEY
from gateway import item_status
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit
from gateway.bank_policies import BankPolicies

BANK = "personal-alice"
TOKEN = "tok-alice"
PROJECT = "project:hindsight-crew"
MARK = "gateway_item_status"
EL_ID = 11

# 변경 이전(origin/main 77bca59) 코드가 같은 입력(BASE_ROWS, 플래그 없음)에 돌려준 MCP recall 응답 바이트의 sha256.
BASELINE_SHA256 = "24ec7baf3a02bfb00b8c28f9873b7305337de7a95dc265fbfb0c5c2b65ef462b"


def mrow(mem_id, *, text="[인계] hindsight-crew#251 남은 일 BODY-SECRET-OPEN", tags=("kind:handoff", PROJECT),
         fact_type="world", at="2026-10-10T10:00:00Z", doc="doc-open"):
    """recall 결과 행(MCP: fact_type)."""
    return {"id": mem_id, "text": text, "fact_type": fact_type, "tags": list(tags), "mentioned_at": at,
            "document_id": doc, "context": "general"}


def lrow(mem_id, *, text="[완료] hindsight-crew#251 정리 끝 BODY-SECRET-DONE", tags=("kind:completion", PROJECT),
         fact_type="experience", at="2026-10-15T10:00:00Z", state="valid", doc=None):
    """memories/list 항목."""
    out = {"id": mem_id, "text": text, "fact_type": fact_type, "tags": list(tags), "document_id": doc or f"doc-{mem_id}"}
    if at is not None:
        out["mentioned_at"] = at
    if state is not None:
        out["state"] = state
    return out


BASE_ROWS = [mrow("open-251"), mrow("plain", text="[발견] 키 없는 기억", tags=("kind:gotcha", PROJECT))]
BASE_LIST = [lrow("done-251-old", at="2026-10-12T10:00:00Z"), lrow("done-251-new")]


def recall_element(el_id, rows, *, is_error=False, shape="double"):
    """recall 응답 요소. shape: double(upstream 실제: structuredContent.result 문자열 + content 텍스트 사본),
    direct(structuredContent 가 곧 payload + 텍스트 사본), direct-only(텍스트 사본 없음)."""
    payload = {"results": rows}
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if shape == "double":
        result = {"content": [{"type": "text", "text": text}], "structuredContent": {"result": text}}
    elif shape == "direct":
        result = {"content": [{"type": "text", "text": text}], "structuredContent": payload}
    else:
        result = {"content": [], "structuredContent": payload}
    return {"jsonrpc": "2.0", "id": el_id, "result": {**result, "isError": is_error}}


def recall_sse(el_id, rows, *, is_error=False, shape="double"):
    frame = json.dumps(recall_element(el_id, rows, is_error=is_error, shape=shape), ensure_ascii=False)
    return b"event: message\r\ndata: " + frame.encode("utf-8") + b"\r\n\r\n"


def batch_sse(ids, rows, **kwargs):
    frame = json.dumps([recall_element(i, rows, **kwargs) for i in ids], ensure_ascii=False)
    return b"event: message\r\ndata: " + frame.encode("utf-8") + b"\r\n\r\n"


class StatusUpstream:
    """MCP recall 응답(고정 행)과 memories/list(q·tags 필터·페이지)를 흉내 내는 mock upstream."""

    def __init__(self, *, rows=None, list_rows=None, is_error=False, list_status=200, list_delay=0.0,
                 list_raises=False, shape="double"):
        self.shape = shape
        self.rows = list(BASE_ROWS if rows is None else rows)
        self.list_rows = list(BASE_LIST if list_rows is None else list_rows)
        self.is_error = is_error
        self.list_status = list_status
        self.list_delay = list_delay
        self.list_raises = list_raises
        self.calls = []

    async def handler(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        query = parse_qsl(str(request.url.query.decode() if isinstance(request.url.query, bytes)
                              else request.url.query))
        self.calls.append({"method": request.method, "path": path, "query": query, "body": body})
        if request.method == "GET" and path.endswith("/memories/list"):
            if self.list_delay:
                await asyncio.sleep(self.list_delay)
            if self.list_raises:
                raise httpx.ConnectError("synthetic", request=request)
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={"detail": "synthetic"})
            return httpx.Response(200, json=self.page(query))
        if path == "/mcp" and request.method == "POST":
            if isinstance(body, list):
                content = batch_sse([el.get("id") for el in body], self.rows, is_error=self.is_error,
                                    shape=self.shape)
            else:
                content = recall_sse(body.get("id"), self.rows, is_error=self.is_error, shape=self.shape)
            return httpx.Response(200, content=content,
                                  headers={"content-type": "text/event-stream", "mcp-session-id": "s1"})
        if request.method == "POST" and path.endswith("/memories/recall"):
            return httpx.Response(200, json={"results": self.rows})
        return httpx.Response(404, json={})

    def page(self, query):
        params = {}
        for k, v in query:
            params.setdefault(k, []).append(v)
        tags = params.get("tags", [])
        rows = self.list_rows
        if params.get("state"):
            rows = [r for r in rows if r.get("state") == params["state"][0]]
        if params.get("q"):
            rows = [r for r in rows if params["q"][0] in r["text"]]
            if tags:                                                  # any_strict
                rows = [r for r in rows if set(tags) & set(r["tags"])]
        elif tags:                                                    # all_strict
            rows = [r for r in rows if set(tags) <= set(r["tags"])]
        limit, offset = int(params.get("limit", ["100"])[0]), int(params.get("offset", ["0"])[0])
        return {"items": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset}

    def lists(self):
        return [c for c in self.calls if c["method"] == "GET" and c["path"].endswith("/memories/list")]

    def list_qs(self):
        return [dict(c["query"]).get("q") for c in self.lists() if dict(c["query"]).get("q")]


def make_app(up, tmp_path, *, bank_policies=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(up.handler), base_url="http://upstream", timeout=5.0)
    log = tmp_path / "audit.log"
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(log)), upstream_key=UPSTREAM_KEY,
                     http_client=http, bank_policies=bank_policies)
    return TestClient(app), log


def set_env(monkeypatch, name, value):
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


def recall(client, args=None, *, bank=BANK):
    body = {"jsonrpc": "2.0", "id": EL_ID, "method": "tools/call",
            "params": {"name": "recall", "arguments": {"query": "q", **(args or {})}}}
    return client.post(f"/mcp/{bank}/", json=body,
                       headers={"authorization": f"Bearer {TOKEN}", "content-type": "application/json"})


def run(monkeypatch, tmp_path, *, flag="1", args=None, bank_policies=None, env=None, **up_kwargs):
    """(응답, 감사 로그 경로, mock upstream)."""
    set_env(monkeypatch, item_status.ENV_FLAG, flag)
    for name, value in (env or {}).items():
        set_env(monkeypatch, name, value)
    tmp_path.mkdir(parents=True, exist_ok=True)
    up = StatusUpstream(**up_kwargs)
    client, log = make_app(up, tmp_path, bank_policies=bank_policies)
    with client:
        resp = recall(client, args)
    assert resp.status_code == 200
    return resp, log, up


def elements_of(resp):
    frame = [ln[len("data:"):].strip() for ln in resp.content.decode("utf-8").splitlines() if ln.startswith("data:")][-1]
    data = json.loads(frame)
    return data if isinstance(data, list) else [data]


def decode_element(element):
    """요소의 payload. structuredContent 와 텍스트 사본이 둘 다 있으면 같아야 한다."""
    result = element["result"]
    structured = result["structuredContent"]
    if "result" in structured:
        structured = json.loads(structured["result"])
    texts = [json.loads(b["text"]) for b in result["content"] if b.get("type") == "text"]
    assert all(t == structured for t in texts)        # 두 벌이 어긋나지 않는다
    return structured


def payload_of(resp):
    return decode_element(elements_of(resp)[-1])


def rows_of(resp):
    return payload_of(resp)["results"]


def audit_rows(log, decision=item_status.DECISION):
    if not log.exists():
        return []
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r["decision"] == decision]


def appended(resp, off_resp):
    on_rows, off_rows = rows_of(resp), rows_of(off_resp)
    assert on_rows[:len(off_rows)] == off_rows        # 원래 행의 집합·순서·내용 불변
    extra = on_rows[len(off_rows):]
    assert all(r.get(MARK) is True for r in extra)
    assert not any(MARK in r for r in off_rows)
    return extra


def off_response(monkeypatch, tmp_path, **kwargs):
    return run(monkeypatch, tmp_path / "off", flag=None, **kwargs)[0]


# --------------------------------------------------------------------------
# (a) 꺼짐: 응답 바이트·upstream 호출열이 지금과 같다
# --------------------------------------------------------------------------

def test_code_default_is_off(monkeypatch):
    monkeypatch.delenv(item_status.ENV_FLAG, raising=False)
    assert item_status.enabled() is False


@pytest.mark.parametrize("flag", [None, "0", "", "off", "no", "false", "garbage"])
def test_flag_off_recall_unchanged(monkeypatch, tmp_path, flag):
    resp, log, up = run(monkeypatch, tmp_path, flag=flag)
    assert hashlib.sha256(resp.content).hexdigest() == BASELINE_SHA256
    assert MARK.encode() not in resp.content
    assert [(c["method"], c["path"]) for c in up.calls] == [("POST", "/mcp")]      # 찾기 호출 0회
    assert audit_rows(log) == []


# --------------------------------------------------------------------------
# (b) 켜짐
# --------------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
def test_on_appends_latest_completion_at_the_end_with_marker(monkeypatch, tmp_path, flag):
    off = off_response(monkeypatch, tmp_path)
    resp, log, up = run(monkeypatch, tmp_path / "on", flag=flag)
    extra = appended(resp, off)
    assert [r["id"] for r in extra] == ["done-251-new"]                  # 키당 최신 1개
    assert extra[0]["text"] == BASE_LIST[1]["text"]
    assert up.list_qs() == ["#251"]                                      # 키 1개: 한 번 조회(본문 질의)
    q = [c for c in up.lists() if dict(c["query"]).get("q") == "#251"][0]["query"]
    assert ("tags", PROJECT) in q and ("tags_match", "any_strict") in q and ("state", "valid") in q
    rows = audit_rows(log)
    assert len(rows) == 1 and rows[0]["reason"] == "enriched"
    assert rows[0]["item_status"]["appended"] == 1 and rows[0]["item_status"]["keys"] == ["hindsight-crew#251"]
    assert "BODY-SECRET" not in log.read_text(encoding="utf-8")


def test_on_tag_only_item_key_is_found_through_the_tag_lookup(monkeypatch, tmp_path):
    rows = [mrow("open-tag", text="[인계] 본문에 번호 없음", tags=("kind:handoff", PROJECT, "item:hindsight-crew-300"))]
    done = lrow("done-300", text="[완료] 번호 없는 완료", tags=("kind:completion", PROJECT, "item:hindsight-crew-300"))
    resp, _, up = run(monkeypatch, tmp_path, rows=rows, list_rows=[done])
    assert [r["id"] for r in rows_of(resp)[1:]] == ["done-300"]
    tag_lookups = [dict(c["query"]) for c in up.lists() if "q" not in dict(c["query"])]
    assert tag_lookups and tag_lookups[0]["tags_match"] == "all_strict"


def test_on_keeps_only_completion_family_of_the_same_key(monkeypatch, tmp_path):
    lists = [
        lrow("handoff", tags=("kind:handoff", PROJECT)),                              # 완료 계열 아님
        lrow("other-key", text="[완료] hindsight-crew#999 끝"),                      # 키가 다름(q 가 #251 을 못 찾지만 #2510 류 방어)
        lrow("longer-number", text="[완료] hindsight-crew#2510 끝"),                  # q=#251 이 부분 일치로 가져온다
        lrow("other-project", tags=("kind:completion", "project:other")),
        lrow("invalidated", state="invalidated"),
        lrow("done-ok", at="2026-10-14T10:00:00Z"),
    ]
    resp, _, _ = run(monkeypatch, tmp_path, list_rows=lists)
    assert [r["id"] for r in rows_of(resp)[2:]] == ["done-ok"]


ALIASED = BankPolicies.from_dict({"defaults": {}, "banks": {BANK: {
    "tag_prefixes": ["project", "kind", "topic", "user"],
    "tag_aliases": {"kind:done": "kind:completion", "kind:work-log": "kind:completion"}}}})


@pytest.mark.parametrize("old_kind,policies,expected", [
    ("kind:done", ALIASED, ["old-kind"]),          # 별칭 표를 거쳐 completion 이 되는 옛 kind
    ("kind:work-log", ALIASED, ["old-kind"]),
    ("kind:done", None, []),                       # 표가 없으면 옛 kind 는 완료 계열이 아니다
    ("kind:handoff", ALIASED, []),
])
def test_on_completion_family_goes_through_the_alias_table(monkeypatch, tmp_path, old_kind, policies, expected):
    lists = [lrow("old-kind", tags=(old_kind, PROJECT))]
    resp, _, _ = run(monkeypatch, tmp_path, list_rows=lists, bank_policies=policies)
    assert [r["id"] for r in rows_of(resp)[2:]] == expected


QTS = "2026-10-14T10:00:00Z"


@pytest.mark.parametrize("name,list_rows,expected", [
    ("before-included", [lrow("early", at="2026-10-13T10:00:00Z")], ["early"]),
    ("at-cutoff-included", [lrow("exact", at=QTS)], ["exact"]),
    ("after-excluded", [lrow("late", at="2026-10-14T10:00:01Z")], []),
    ("offset-aware-compare", [lrow("tz", at="2026-10-14T18:59:00+09:00")], ["tz"]),   # 09:59Z, 기준 이하
    ("offset-aware-after", [lrow("tz-late", at="2026-10-14T19:01:00+09:00")], []),    # 10:01Z, 기준 이후
    ("no-time-excluded", [lrow("untimed", at=None)], []),
    ("unreadable-time-excluded", [lrow("garbled", at="not-a-time")], []),
    ("latest-of-eligible", [lrow("a", at="2026-10-12T10:00:00Z"), lrow("b", at="2026-10-13T10:00:00Z"),
                            lrow("c", at="2026-10-16T10:00:00Z")], ["b"]),            # 기준 뒤 c 는 후보가 아니다
])
def test_on_query_timestamp_cuts_off_later_and_untimed_rows(monkeypatch, tmp_path, name, list_rows, expected):
    resp, _, _ = run(monkeypatch, tmp_path, list_rows=list_rows, args={"query_timestamp": QTS})
    assert [r["id"] for r in rows_of(resp)[2:]] == expected


def test_on_untimed_row_is_allowed_without_query_timestamp(monkeypatch, tmp_path):
    resp, _, _ = run(monkeypatch, tmp_path, list_rows=[lrow("untimed", at=None)])
    assert [r["id"] for r in rows_of(resp)[2:]] == ["untimed"]


@pytest.mark.parametrize("bad", ["not-a-time", "2026-13-45", 12345])
def test_on_unreadable_query_timestamp_adds_nothing(monkeypatch, tmp_path, bad):
    resp, _, up = run(monkeypatch, tmp_path, args={"query_timestamp": bad})
    assert [r["id"] for r in rows_of(resp)] == ["open-251", "plain"]               # 기준 시각을 못 읽으면 덧붙이지 않는다
    assert MARK.encode() not in resp.content


def test_on_ids_already_in_results_are_excluded(monkeypatch, tmp_path):
    rows = [*BASE_ROWS, mrow("done-251-new", text=BASE_LIST[1]["text"], tags=("kind:completion", PROJECT),
                             at="2026-10-15T10:00:00Z")]
    resp, _, _ = run(monkeypatch, tmp_path, rows=rows)
    ids = [r["id"] for r in rows_of(resp)]
    assert ids.count("done-251-new") == 1
    # 이미 결과에 든 id 를 뺀 나머지에서 최신 1개: 다음으로 늦은 것
    assert ids[len(rows):] == ["done-251-old"]
    assert [r.get(MARK) for r in rows_of(resp)] == [None, None, None, True]


def test_on_one_memory_for_two_keys_is_appended_once(monkeypatch, tmp_path):
    rows = [mrow("open-a", text="[인계] hindsight-crew#251 일"), mrow("open-b", text="[인계] hindsight-crew#252 일")]
    both = lrow("done-both", text="[완료] hindsight-crew#251 hindsight-crew#252 둘 다 끝")
    resp, _, _ = run(monkeypatch, tmp_path, rows=rows, list_rows=[both])
    assert [r["id"] for r in rows_of(resp)[2:]] == ["done-both"]


def test_on_key_cap_limits_lookups_in_result_order(monkeypatch, tmp_path):
    rows = [mrow(f"open-{n}", text=f"[인계] hindsight-crew#{n} 일") for n in (301, 302, 303, 304)]
    lists = [lrow(f"done-{n}", text=f"[완료] hindsight-crew#{n} 끝") for n in (301, 302, 303, 304)]
    resp, log, up = run(monkeypatch, tmp_path, rows=rows, list_rows=lists, env={item_status.ENV_MAX_KEYS: "2"})
    assert sorted(up.list_qs()) == ["#301", "#302"]
    assert [r["id"] for r in rows_of(resp)[4:]] == ["done-301", "done-302"]
    assert audit_rows(log)[0]["item_status"]["keys"] == ["hindsight-crew#301", "hindsight-crew#302"]


def test_on_default_key_cap_is_eight(monkeypatch, tmp_path):
    rows = [mrow(f"open-{n}", text=f"[인계] hindsight-crew#{n} 일") for n in range(401, 412)]
    _, _, up = run(monkeypatch, tmp_path, rows=rows, list_rows=[], env={item_status.ENV_MAX_KEYS: None})
    assert len(up.list_qs()) == 8


@pytest.mark.parametrize("value,expected", [(None, 8), ("", 8), ("abc", 8), ("0", 8), ("-3", 8), ("3", 3)])
def test_max_keys_env_parse(monkeypatch, value, expected):
    set_env(monkeypatch, item_status.ENV_MAX_KEYS, value)
    assert item_status.max_keys() == expected


@pytest.mark.parametrize("value,expected", [(None, 1500), ("", 1500), ("abc", 1500), ("0", 1500), ("250", 250)])
def test_timeout_env_parse(monkeypatch, value, expected):
    set_env(monkeypatch, item_status.ENV_TIMEOUT_MS, value)
    assert item_status.timeout_ms() == expected


# --- 조회 범위(project 태그) ---------------------------------------------------------------------

def test_on_project_scope_comes_from_the_row_that_gave_the_key(monkeypatch, tmp_path):
    rows = [mrow("open-x", tags=("kind:handoff", "project:other"), text="[인계] other#251 일")]
    lists = [lrow("done-x", tags=("kind:completion", "project:other"), text="[완료] other#251 끝")]
    resp, _, up = run(monkeypatch, tmp_path, rows=rows, list_rows=lists, args={"tags": [PROJECT]})
    q = [c for c in up.lists() if dict(c["query"]).get("q")][0]["query"]
    assert ("tags", "project:other") in q and ("tags", PROJECT) not in q
    assert [r["id"] for r in rows_of(resp)[1:]] == ["done-x"]


def test_on_project_scope_falls_back_to_request_tags(monkeypatch, tmp_path):
    rows = [mrow("open-y", tags=("kind:handoff",), text="[인계] Bkankim/hindsight-crew#251 일")]   # 행에 project 태그 없음
    resp, _, up = run(monkeypatch, tmp_path, rows=rows, args={"tags": [PROJECT, "kind:completion"]})
    q = [c for c in up.lists() if dict(c["query"]).get("q")][0]["query"]
    assert ("tags", PROJECT) in q and ("tags", "kind:completion") not in q
    assert [r["id"] for r in rows_of(resp)[1:]] == ["done-251-new"]


def test_on_key_without_any_project_scope_is_not_enriched(monkeypatch, tmp_path):
    rows = [mrow("open-z", tags=("kind:handoff",), text="[인계] Bkankim/hindsight-crew#251 일")]
    resp, log, up = run(monkeypatch, tmp_path, rows=rows)
    assert up.lists() == []                                               # 조회 자체가 없다
    assert [r["id"] for r in rows_of(resp)] == ["open-z"]
    assert audit_rows(log) == []


def test_on_response_without_keys_makes_no_lookup(monkeypatch, tmp_path):
    rows = [mrow("plain", text="[발견] 키 없음", tags=("kind:gotcha", PROJECT))]
    resp, log, up = run(monkeypatch, tmp_path, rows=rows)
    assert up.lists() == [] and audit_rows(log) == []
    assert MARK.encode() not in resp.content


# --- 덧붙인 행과 다른 후처리 ---------------------------------------------------------------------------

def test_on_appended_row_goes_through_prune_and_label(monkeypatch, tmp_path):
    lists = [{**lrow("done-251-new"), "context": None}]                    # 빈 값은 prune 이 지운다
    resp, _, _ = run(monkeypatch, tmp_path, list_rows=lists, env={"HC_RECALL_KIND_LABEL": "1"})
    extra = rows_of(resp)[2]
    assert extra[MARK] is True and "context" not in extra
    assert extra["gateway_kind_label"].startswith("스냅샷")


def test_on_nothing_to_add_keeps_the_bytes(monkeypatch, tmp_path):
    off = off_response(monkeypatch, tmp_path)
    resp, log, up = run(monkeypatch, tmp_path / "on", list_rows=[])
    assert resp.content == off.content
    assert up.list_qs() == ["#251"]
    assert audit_rows(log)[0]["reason"] == "no-extras"


# --------------------------------------------------------------------------
# (c) fail-open: 시간 초과·upstream 오류면 원래 응답 그대로
# --------------------------------------------------------------------------

FAILURES = {
    "http-500": (dict(list_status=500), "lookup-failed:http-500"),
    "http-404": (dict(list_status=404), "lookup-failed:http-404"),
    "transport": (dict(list_raises=True), "lookup-failed:transport"),
    "timeout": (dict(list_delay=0.6), "timeout"),
}


@pytest.mark.parametrize("name", FAILURES)
def test_fail_open_returns_the_original_response(monkeypatch, tmp_path, name):
    kwargs, reason = FAILURES[name]
    off = off_response(monkeypatch, tmp_path)
    resp, log, _ = run(monkeypatch, tmp_path / "on", env={item_status.ENV_TIMEOUT_MS: "100"}, **kwargs)
    assert resp.status_code == 200 and resp.content == off.content
    rows = audit_rows(log)
    assert len(rows) == 1 and rows[0]["reason"] == reason
    assert "BODY-SECRET" not in log.read_text(encoding="utf-8")


def test_one_failing_key_drops_the_whole_enrichment(monkeypatch, tmp_path):
    rows = [mrow("open-a", text="[인계] hindsight-crew#251 일"), mrow("open-b", text="[인계] hindsight-crew#252 일")]
    up = StatusUpstream(rows=rows, list_rows=[lrow("done-251", text="[완료] hindsight-crew#251 끝")])
    real_page = up.page

    def failing_page(query):
        if dict(query).get("q") == "#252":
            raise RuntimeError("synthetic")
        return real_page(query)

    up.page = failing_page
    set_env(monkeypatch, item_status.ENV_FLAG, "1")
    client, log = make_app(up, tmp_path)
    with client:
        resp = recall(client)
    assert [r["id"] for r in rows_of(resp)] == ["open-a", "open-b"]            # 전부 아니면 전무(fail-open)
    assert audit_rows(log)[0]["reason"].startswith("failed:") or audit_rows(log)[0]["reason"].startswith("lookup-failed:")


def test_timeout_budget_is_one_wall_clock_for_all_keys(monkeypatch, tmp_path):
    rows = [mrow(f"open-{n}", text=f"[인계] hindsight-crew#{n} 일") for n in (501, 502, 503)]
    off = off_response(monkeypatch, tmp_path, rows=rows)
    import time
    start = time.monotonic()
    resp, log, _ = run(monkeypatch, tmp_path / "on", rows=rows, list_delay=0.4,
                       env={item_status.ENV_TIMEOUT_MS: "200"})
    assert time.monotonic() - start < 2.0                                       # 키 수 x 지연이 아니라 상한 하나
    assert resp.content == off.content and audit_rows(log)[0]["reason"] == "timeout"


# --------------------------------------------------------------------------
# (d) 범위 밖: 오류 결과, REST recall
# --------------------------------------------------------------------------

def test_on_error_result_is_untouched(monkeypatch, tmp_path):
    off = off_response(monkeypatch, tmp_path, is_error=True)
    resp, log, up = run(monkeypatch, tmp_path / "on", is_error=True)
    assert resp.content == off.content and up.lists() == [] and audit_rows(log) == []


def test_on_rest_recall_is_out_of_scope(monkeypatch, tmp_path):
    set_env(monkeypatch, item_status.ENV_FLAG, "1")
    up = StatusUpstream()
    client, log = make_app(up, tmp_path)
    with client:
        resp = client.post(f"/v1/default/banks/{BANK}/memories/recall", json={"query": "q"},
                           headers={"authorization": f"Bearer {TOKEN}", "content-type": "application/json"})
    assert resp.status_code == 200 and MARK.encode() not in resp.content
    assert up.lists() == [] and audit_rows(log) == []


def test_on_non_recall_tools_are_untouched(monkeypatch, tmp_path):
    set_env(monkeypatch, item_status.ENV_FLAG, "1")
    up = StatusUpstream()
    client, _ = make_app(up, tmp_path)
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "list_memories", "arguments": {}}}
    with client:
        client.post(f"/mcp/{BANK}/", json=body,
                    headers={"authorization": f"Bearer {TOKEN}", "content-type": "application/json"})
    assert up.lists() == []


# --------------------------------------------------------------------------
# final-review 반영 (#339): 배치의 요소별 중복 제거, 직접 structuredContent
# --------------------------------------------------------------------------

def batch(client, ids=(11, 12), args=None):
    body = [{"jsonrpc": "2.0", "id": i, "method": "tools/call",
             "params": {"name": "recall", "arguments": {"query": "q", **(args or {})}}} for i in ids]
    return client.post(f"/mcp/{BANK}/", json=body,
                       headers={"authorization": f"Bearer {TOKEN}", "content-type": "application/json"})


@pytest.mark.parametrize("ids", [(11, 12), (11, 12, 13)])
def test_on_batch_each_recall_element_gets_its_own_completion(monkeypatch, tmp_path, ids):
    set_env(monkeypatch, item_status.ENV_FLAG, "1")
    up = StatusUpstream()
    client, log = make_app(up, tmp_path)
    with client:
        resp = batch(client, ids)
    elements = elements_of(resp)
    assert [e["id"] for e in elements] == list(ids)
    for element in elements:
        rows = decode_element(element)["results"]
        assert [r["id"] for r in rows] == ["open-251", "plain", "done-251-new"]     # 요소마다 같은 완료가 붙는다
        assert rows[2][MARK] is True
    assert len(audit_rows(log)) == 1 and audit_rows(log)[0]["item_status"]["appended"] == len(ids)
    assert up.list_qs() == ["#251"]                                                 # 같은 키는 한 번만 조회한다


def test_on_batch_dedup_stays_within_an_element(monkeypatch, tmp_path):
    set_env(monkeypatch, item_status.ENV_FLAG, "1")
    rows = [mrow("open-a", text="[인계] hindsight-crew#251 일"), mrow("open-b", text="[인계] hindsight-crew#252 일")]
    both = lrow("done-both", text="[완료] hindsight-crew#251 hindsight-crew#252 둘 다 끝")
    up = StatusUpstream(rows=rows, list_rows=[both])
    client, _ = make_app(up, tmp_path)
    with client:
        resp = batch(client)
    for element in elements_of(resp):
        assert [r["id"] for r in decode_element(element)["results"]] == ["open-a", "open-b", "done-both"]


@pytest.mark.parametrize("shape", ["direct", "direct-only"])
def test_on_direct_structured_content_is_enriched_in_every_copy(monkeypatch, tmp_path, shape):
    off = off_response(monkeypatch, tmp_path, shape=shape)
    resp, _, _ = run(monkeypatch, tmp_path / "on", shape=shape)
    assert [r["id"] for r in rows_of(resp)] == ["open-251", "plain", "done-251-new"]
    assert rows_of(resp)[:2] == rows_of(off)
    result = elements_of(resp)[0]["result"]
    assert result["structuredContent"]["results"][2][MARK] is True
    if shape == "direct":
        assert len(result["content"]) == 1                                          # 텍스트 사본도 같이 바뀐다


def test_on_duplicate_response_ids_are_not_enriched(monkeypatch, tmp_path):
    """같은 JSON-RPC id 의 recall 응답이 둘이면 어느 요청의 인자인지 모르므로 보강하지 않는다(final-review C10)."""
    set_env(monkeypatch, item_status.ENV_FLAG, "1")
    up = StatusUpstream()
    (tmp_path / "on").mkdir(exist_ok=True)
    client, log = make_app(up, tmp_path / "on")
    with client:
        resp = batch(client, ids=(11, 11))
    for element in elements_of(resp):
        assert [r["id"] for r in decode_element(element)["results"]] == ["open-251", "plain"]
    assert MARK.encode() not in resp.content and up.lists() == [] and audit_rows(log) == []
