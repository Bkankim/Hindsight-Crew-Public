"""이슈 #49: REST 후행 슬래시 경로가 게이트웨이 판정을 건너뛰지 않는다.

upstream은 지금 `memories/`를 307로 돌려보내지만(벤치 복제본 실측), 게이트웨이가 그 동작에
기대면 안 된다. 게이트웨이는 `rest`의 후행 슬래시를 떼어 만든 **정규 경로 하나**로 판정(DELETE
차단, retain rules·감사·귀속, recall ledger)과 upstream 전달과 감사 `tool` 값을 모두 처리한다.
판정한 경로와 전달한 경로가 다르면 우회로가 생기므로, 경로 변형 x 판정 전체를 행렬로 고정한다.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, Recorder
from gateway import policy
from test_rest_rules import (STRICT_BANK, TEAM_BANK, audit_entries, body, forwarded_items, item,
                             strict_app)

SLASHES = ["", "/", "//"]          # 후행 슬래시 0개 / 1개 / 2개
BLOCKED_BANK = "project-demo-gm"   # 정책 파일상 delete_bank·clear_memories 차단 뱅크


def _auth(token):
    return {"authorization": f"Bearer {token}", "content-type": "application/json"}


def _json_post(client, path, payload, token="tok-alice"):
    return client.post(path, content=json.dumps(payload), headers=_auth(token))


@pytest.fixture
def recall_recorder(recorder, monkeypatch):
    """recall 경로에는 ledger가 읽을 수 있는 `results`를 돌려주는 upstream."""
    base = Recorder.handler

    def handler(request: httpx.Request) -> httpx.Response:
        resp = base(recorder, request)
        if request.url.path.rstrip("/").endswith("/memories/recall"):
            return httpx.Response(200, json={"results": [{"id": "mem-1", "text": "x"}]})
        return resp

    monkeypatch.setattr(recorder, "handler", handler)
    return recorder


def _assert_upstream_saw_only_canonical(recorder, canonical):
    assert recorder.calls, "upstream이 호출되지 않았다"
    assert recorder.calls[-1]["path"] == canonical
    assert all(not c["path"].endswith("/") for c in recorder.calls)


@pytest.mark.parametrize("slashes", SLASHES)
def test_untagged_retain_is_422_and_never_forwarded(make_app, recorder, slashes):
    c = TestClient(strict_app(make_app))
    r = _json_post(c, f"/v1/default/banks/{STRICT_BANK}/memories{slashes}", body(item(tags=[])))

    assert r.status_code == 422
    assert r.json()["error"] == "rule-violation"
    assert r.json()["rule"] == "RULE:tags-required"
    assert recorder.calls == []


@pytest.mark.parametrize("slashes", SLASHES)
def test_valid_retain_reaches_upstream_on_canonical_path_with_tag_audit(make_app, recorder, slashes):
    canonical = f"/v1/default/banks/{STRICT_BANK}/memories"
    c = TestClient(strict_app(make_app))
    r = _json_post(c, f"{canonical}{slashes}", body(item()))

    assert r.status_code == 200
    _assert_upstream_saw_only_canonical(recorder, canonical)
    assert forwarded_items(recorder)[0]["timestamp"].endswith("Z")     # 규칙 본문 변환이 적용됐다
    entries = audit_entries(c)
    retain_tags = [e for e in entries if e["decision"] == "retain-tags"]
    assert len(retain_tags) == 1
    # 감사 tool 값도 정규 경로다 - bench rerank_kind의 endswith("/memories") 판정과 맞물린다.
    assert retain_tags[0]["tool"] == canonical
    assert {e["tool"] for e in entries
            if e["reason"] == "rest-allow" or e["decision"] == "retain-tags"} == {canonical}


@pytest.mark.parametrize("slashes", SLASHES)
def test_team_bank_retain_gets_attribution_on_canonical_path(make_app, recorder, slashes):
    canonical = f"/v1/default/banks/{TEAM_BANK}/memories"
    c = TestClient(strict_app(make_app))
    r = _json_post(c, f"{canonical}{slashes}", body(item(metadata={"attribution": "forged"})))

    assert r.status_code == 200
    _assert_upstream_saw_only_canonical(recorder, canonical)
    assert forwarded_items(recorder)[0]["metadata"][policy.ATTRIBUTION_KEY] == "alice"


@pytest.mark.parametrize("slashes", SLASHES)
def test_recall_is_ledgered_and_forwarded_on_canonical_path(make_app, recall_recorder, slashes):
    canonical = f"/v1/default/banks/{STRICT_BANK}/memories/recall"
    c = TestClient(strict_app(make_app))
    r = _json_post(c, f"{canonical}{slashes}", {"query": "후행 슬래시 recall 원장 기록 확인"})

    assert r.status_code == 200
    _assert_upstream_saw_only_canonical(recall_recorder, canonical)
    events = c.app.state.recall_ledger.recent(identity="alice", bank=STRICT_BANK, since=0)
    assert [(e.transport, e.result_ids) for e in events] == [("rest", ("mem-1",))]
    assert {e["tool"] for e in audit_entries(c) if e["reason"] == "rest-allow"} == {canonical}


@pytest.fixture
def blocked_delete_app(make_app, monkeypatch):
    monkeypatch.setitem(ACL_DATA["tokens"], "tok-demo-gm",
                        {"identity": "demo-gm-user", "personal": BLOCKED_BANK, "teams": []})
    return TestClient(strict_app(make_app))


@pytest.mark.parametrize("slashes", SLASHES)
@pytest.mark.parametrize("rest, operation", [("/memories", "clear_memories"), ("", "delete_bank")])
def test_blocked_delete_applies_to_every_slash_variant(
        blocked_delete_app, recorder, slashes, rest, operation):
    r = blocked_delete_app.delete(f"/v1/default/banks/{BLOCKED_BANK}{rest}{slashes}",
                                  headers=_auth("tok-demo-gm"))

    assert r.status_code == 403
    assert r.json() == {"error": "forbidden", "reason": f"blocked-by-bank-policy:{operation}"}
    assert recorder.calls == []


@pytest.mark.parametrize("slashes", SLASHES)
def test_unblocked_delete_is_forwarded_on_canonical_path(make_app, recorder, slashes):
    canonical = f"/v1/default/banks/{STRICT_BANK}/memories"
    c = TestClient(strict_app(make_app))
    r = c.delete(f"{canonical}{slashes}", headers=_auth("tok-alice"))

    assert r.status_code == 200
    _assert_upstream_saw_only_canonical(recorder, canonical)


@pytest.mark.parametrize("slashes", SLASHES)
def test_query_string_survives_path_normalization(make_app, recorder, slashes):
    canonical = f"/v1/default/banks/{STRICT_BANK}/memories/list"
    c = TestClient(strict_app(make_app))
    r = c.get(f"{canonical}{slashes}?tags=a&tags=b&limit=5", headers=_auth("tok-alice"))

    assert r.status_code == 200
    _assert_upstream_saw_only_canonical(recorder, canonical)
    assert recorder.last["query"] == "tags=a&tags=b&limit=5"


# ===== final-review 반영(issue #49): 비정규 경로는 정규화하지 않고 400으로 거부한다 =====
# httpx는 base_url과 합칠 때 dot-segment를 접고 `#`을 fragment로 뗀다. 판정 경로에 남은 이
# 문자들이 전달 경로에서 사라지면 "판정한 경로 = 전달한 경로"가 깨져 ACL·rules가 우회된다.
# `..`를 접어 통과시키면 판정 대상 뱅크가 바뀌므로 거부가 안전하다.

METHODS = ["POST", "DELETE", "GET"]
# TestClient(httpx)로 보내는 인코딩 형태. 서버가 디코딩한 값(`..`, `#`, `?`)이 rest에 들어온다.
ENCODED_REST = ["%2E%2E/team-eng/memories", "%2E/memories", "memories%23", "memories%3F",
                "memories//recall", "memories/%2E%2E"]
# 클라이언트가 접거나 떼어 버려 TestClient로는 못 보내는 형태는 ASGI scope(디코딩된 path)로 직접 보낸다.
RAW_PATHS = [
    f"/v1/default/banks/{STRICT_BANK}/../team-eng/memories",
    f"/v1/default/banks/{STRICT_BANK}/./memories",
    f"/v1/default/banks/{STRICT_BANK}/memories/..",
    f"/v1/default/banks/{STRICT_BANK}/memories#",
    f"/v1/default/banks/{STRICT_BANK}/memories?",
    f"/v1/default/banks/{STRICT_BANK}/memories\\x",
    f"/v1/default/banks/{STRICT_BANK}/memories//recall",
    f"/v1/../banks/{STRICT_BANK}/memories",
    f"/v1/#/banks/{STRICT_BANK}/memories",
]


def _asgi_request(app, method, path, token="tok-alice", payload=None):
    """디코딩된 path를 ASGI scope로 그대로 전달한다(클라이언트 쪽 경로 접기를 피한다)."""
    import asyncio
    body = json.dumps(payload).encode() if payload is not None else b""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "server": ("testserver", 80), "client": ("127.0.0.1", 1),
             "headers": [(b"authorization", f"Bearer {token}".encode()),
                         (b"content-type", b"application/json"),
                         (b"content-length", str(len(body)).encode())]}
    sent = []

    async def run():
        done = False

        async def receive():
            nonlocal done
            if done:
                return {"type": "http.disconnect"}
            done = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)

    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(data) if data else None


def _assert_rejected_once(c, recorder, status, data, path_hint=None):
    assert status == 400
    assert data == {"error": "forbidden", "reason": "non-canonical-path"}
    assert recorder.calls == []                                   # upstream 호출 0회
    entries = audit_entries(c)
    assert len(entries) == 1                                      # 감사 deny 1줄
    assert (entries[0]["decision"], entries[0]["reason"]) == ("deny", "non-canonical-path")


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("rest", ENCODED_REST)
def test_non_canonical_encoded_rest_is_rejected_400(make_app, recorder, method, rest):
    c = TestClient(strict_app(make_app))
    r = c.request(method, f"/v1/default/banks/{STRICT_BANK}/{rest}",
                  content=json.dumps(body(item())), headers=_auth("tok-alice"))
    _assert_rejected_once(c, recorder, r.status_code, r.json())


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("path", RAW_PATHS)
def test_non_canonical_raw_path_is_rejected_400(make_app, recorder, method, path):
    app = strict_app(make_app)
    status, data = _asgi_request(app, method, path, payload=body(item()))
    c = TestClient(app)
    _assert_rejected_once(c, recorder, status, data)


def test_bank_segment_dot_dot_stays_blocked_by_bank_format(make_app, recorder):
    """bank 자리의 `..`는 _BANK_RE가 이미 막는다 - 회귀 고정."""
    app = strict_app(make_app)
    status, data = _asgi_request(app, "POST", "/v1/default/banks/../memories", payload=body(item()))
    assert status == 403
    assert data["reason"] == "bad-bank-format"
    assert recorder.calls == []


@pytest.mark.parametrize("method, rest, payload, expected_path", [
    ("POST", "memories", body(item()), "memories"),
    ("POST", "memories/", body(item()), "memories"),
    ("POST", "memories/recall", {"query": "정상 경로 회귀 확인용 recall 질의"}, "memories/recall"),
    ("GET", "memories/list?limit=5", None, "memories/list"),
])
def test_canonical_paths_still_pass_the_guard(make_app, recorder, method, rest, payload, expected_path):
    c = TestClient(strict_app(make_app))
    r = c.request(method, f"/v1/default/banks/{STRICT_BANK}/{rest}",
                  content=(json.dumps(payload) if payload is not None else None),
                  headers=_auth("tok-alice"))
    assert r.status_code == 200
    assert recorder.last["path"] == f"/v1/default/banks/{STRICT_BANK}/{expected_path}"
    assert len(recorder.calls) == 1
