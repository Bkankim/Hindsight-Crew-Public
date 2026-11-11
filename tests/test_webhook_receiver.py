"""게이트웨이 웹훅 수신 경로 (issue #303) - POST /webhooks/hindsight, 기능 플래그 HC_WEBHOOK_RECEIVER 기본 꺼짐.

근거 (upstream v1.1.0 hindsight-docs/docs/developer/api/webhooks.mdx):
  25행    중복 거름은 event_id(X-Hindsight-Event-Id)로 한다. operation_id 는 배치 retain 에서 문서마다 같은 값이다.
  39행    X-Hindsight-Signature-V2: `t=<unix_seconds>,v1=<hex>`, 서명 대상은 `<t>.<raw body>` (HMAC-SHA256).
  44행    t 가 허용 폭 안인지 확인한다(5분이 흔한 기본값).
  78-110행 consolidation.completed, status completed|failed.
  152-189행 memory_defense.triggered, status/data.action redact|block.

완료 기준 (a)~(f): (a) 플래그 꺼짐 404 + 다른 경로 불변, (b) 올바른 서명 200 + 처리 기록, (c) 틀린·없는 서명 401,
(d) 같은 event_id 두 번째는 처리하지 않음, (e) 실패·차단에서만 알림 + 본문에 비밀 없음 + 이벤트 종류 명시,
(f) 분리된 전송 도우미 뒤에서도 notify_discord 전송 본문이 분리 전과 같다.
실제 디스코드·네트워크 호출은 0회다(notifier 주입, httpx MockTransport).
"""
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import recurrence
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit

from conftest import ACL_DATA, UPSTREAM_KEY

ROOT = Path(__file__).resolve().parents[1]
PATH = "/webhooks/hindsight"
# 실행마다 새로 만든다: 리포에는 비밀 모양의 리터럴을 두지 않는다.
SECRET = "whsec-" + secrets.token_hex(16)
NOW = 1_800_000_000.0
FAKE_TOKEN = "ghp_" + "a1B2" * 9          # scrub_secrets 의 gh[pousr]_ 모양, 실제 토큰이 아니다


def _upstream(request: httpx.Request) -> httpx.Response:
    raise AssertionError("webhook receiver must not call upstream: %s" % request.url)


def make_app(monkeypatch, tmp_path, *, flag="1", secret=SECRET):
    if flag is None:
        monkeypatch.delenv("HC_WEBHOOK_RECEIVER", raising=False)
    else:
        monkeypatch.setenv("HC_WEBHOOK_RECEIVER", flag)
    if secret is None:
        monkeypatch.delenv("HC_WEBHOOK_SECRET", raising=False)
    else:
        monkeypatch.setenv("HC_WEBHOOK_SECRET", secret)
    http = httpx.AsyncClient(transport=httpx.MockTransport(_upstream), base_url="http://upstream", timeout=5.0)
    audit_path = tmp_path / "audit.log"
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(audit_path)),
                     upstream_key=UPSTREAM_KEY, http_client=http, clock=lambda: NOW)
    sent = []

    async def notifier(content):
        sent.append(content)
        return "ok"

    app.state.webhook_notifier = notifier
    return TestClient(app), sent, audit_path


def sign(body: bytes, *, secret=SECRET, t=None) -> str:
    t = int(NOW) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def event(kind="consolidation.completed", *, status="completed", event_id="ev-1", operation_id="op-1",
          bank="personal-alice", data=None):
    defaults = {
        "consolidation.completed": {"observations_created": 3, "observations_updated": 1,
                                    "observations_deleted": None, "error_message": None},
        "memory_defense.triggered": {"action": status, "detector": "sensitive_data", "document_id": "doc-1",
                                     "matched_types": ["github_token"], "message": "Sensitive data pattern matched"},
        "retain.completed": {"document_id": "doc-1", "tags": ["kind:note"], "memory_unit_count": 2},
    }
    return {"event_id": event_id, "event": kind, "bank_id": bank, "operation_id": operation_id, "status": status,
            "timestamp": "2026-10-30T00:00:00Z", "data": data if data is not None else defaults[kind]}


def deliver(client, payload, *, signature="auto", event_id_header="auto", extra_headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"content-type": "application/json"}
    if signature == "auto":
        headers["x-hindsight-signature-v2"] = sign(body)
    elif signature is not None:
        headers["x-hindsight-signature-v2"] = signature
    if event_id_header == "auto":
        headers["x-hindsight-event-id"] = payload.get("event_id", "")
    elif event_id_header is not None:
        headers["x-hindsight-event-id"] = event_id_header
    headers.update(extra_headers or {})
    return client.post(PATH, content=body, headers=headers)


def audit_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def webhook_rows(path):
    return [r for r in audit_rows(path) if r.get("tool") == "webhooks"]


def processed_rows(path):
    return [r for r in webhook_rows(path) if r["decision"] == "webhook"]


def notify_rows(path):
    return [r for r in webhook_rows(path) if r["decision"] == "webhook-notify"]


# --- (a) 플래그 꺼짐 ---------------------------------------------------------------------

@pytest.mark.parametrize("flag", [None, "", "0", "false", "off"])
def test_a_flag_off_is_404_and_other_routes_unchanged(monkeypatch, tmp_path, flag):
    client, sent, audit_path = make_app(monkeypatch, tmp_path, flag=flag)
    payload = event("memory_defense.triggered", status="block")
    resp = deliver(client, payload)
    assert resp.status_code == 404 and resp.json() == {"detail": "Not Found"}
    assert client.get(PATH).status_code == 404
    assert sent == [] and webhook_rows(audit_path) == []
    # 다른 표면은 지금 그대로다.
    assert client.get("/healthz").json()["ok"] is True
    root = client.post("/mcp/", content=b"{}", headers={"content-type": "application/json"})
    assert root.status_code == 403 and root.json() == {"error": "multi-bank endpoint disabled"}
    ctx = client.post("/context-triggers/personal-alice", content=b"{}", headers={"content-type": "application/json"})
    assert ctx.status_code == 403 and ctx.json()["reason"] == "unknown-or-missing-token"


def test_a_flag_off_route_table_is_flag_on_table_minus_webhook(monkeypatch, tmp_path):
    def routes(flag):
        client, _, _ = make_app(monkeypatch, tmp_path, flag=flag)
        return [(r.path, tuple(sorted(r.methods or ()))) for r in client.app.routes]

    off, on = routes("0"), routes("1")
    assert [r for r in on if r not in off] == [(PATH, ("POST",))]
    assert [r for r in on if r[0] != PATH] == off
    # 새 라우트는 /context-triggers 라우트 바로 뒤에 등록된다.
    paths = [p for p, _ in on]
    assert paths.index(PATH) == max(i for i, p in enumerate(paths) if p.startswith("/context-triggers/")) + 1


# --- (b) 올바른 서명 -> 200 + 처리 기록 ---------------------------------------------------------

@pytest.mark.parametrize("kind,status", [
    ("consolidation.completed", "completed"),
    ("consolidation.completed", "failed"),
    ("memory_defense.triggered", "redact"),
    ("memory_defense.triggered", "block"),
])
def test_b_valid_signature_is_200_and_leaves_one_audit_line(monkeypatch, tmp_path, kind, status):
    client, _, audit_path = make_app(monkeypatch, tmp_path)
    payload = event(kind, status=status, operation_id="op-77")
    resp = deliver(client, payload)
    assert resp.status_code == 200 and resp.json() == {"status": "processed"}
    rows = processed_rows(audit_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["method"] == "POST" and row["resolved_bank"] == "personal-alice" and row["reason"] == f"{kind}:{status}"
    hook = row["webhook"]
    assert hook["event"] == kind and hook["operation_id"] == "op-77" and hook["bank"] == "personal-alice"
    assert hook["event_id"] == "ev-1" and hook["status"] == status


def test_b_audit_line_has_summary_and_no_body_text(monkeypatch, tmp_path):
    client, _, audit_path = make_app(monkeypatch, tmp_path)
    data = {"observations_created": 3, "observations_updated": 1, "observations_deleted": None,
            "error_message": "boom " + FAKE_TOKEN + " PRIVATE-DETAIL"}
    assert deliver(client, event(status="failed", data=data)).status_code == 200
    text = audit_path.read_text(encoding="utf-8")
    assert "PRIVATE-DETAIL" not in text and FAKE_TOKEN not in text and SECRET not in text
    summary = processed_rows(audit_path)[0]["webhook"]["summary"]
    assert summary == {"observations_created": 3, "observations_updated": 1, "has_error": True}

    sub = tmp_path / "md"
    sub.mkdir()
    client, _, audit_path = make_app(monkeypatch, sub)
    block = event("memory_defense.triggered", status="block")
    block["data"]["message"] = "PRIVATE-DETAIL " + FAKE_TOKEN
    assert deliver(client, block).status_code == 200
    text = audit_path.read_text(encoding="utf-8")
    assert "PRIVATE-DETAIL" not in text and FAKE_TOKEN not in text
    summary = processed_rows(audit_path)[0]["webhook"]["summary"]
    assert summary == {"action": "block", "detector": "sensitive_data", "matched_types": ["github_token"],
                       "document_id": "doc-1"}


def test_b_unsubscribed_event_is_acknowledged_and_not_processed(monkeypatch, tmp_path):
    client, sent, audit_path = make_app(monkeypatch, tmp_path)
    resp = deliver(client, event("retain.completed"))
    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    assert processed_rows(audit_path) == [] and sent == []
    assert [r["reason"] for r in webhook_rows(audit_path)] == ["ignored:retain.completed"]


def test_b_bad_json_after_valid_signature_is_400(monkeypatch, tmp_path):
    client, sent, audit_path = make_app(monkeypatch, tmp_path)
    body = b"{not json"
    resp = client.post(PATH, content=body, headers={"x-hindsight-signature-v2": sign(body)})
    assert resp.status_code == 400 and sent == [] and processed_rows(audit_path) == []


# --- (c) 틀린·없는 서명 -> 401 -------------------------------------------------------------------

def _bad_signatures():
    body = json.dumps(event("memory_defense.triggered", status="block"), ensure_ascii=False).encode("utf-8")
    t = int(NOW)
    other = "other-" + secrets.token_hex(8)
    return {
        "missing": None,
        "empty": "",
        "garbage": "not-a-signature",
        "wrong-secret": sign(body, secret=other),
        "tampered-hex": sign(body)[:-1] + ("0" if sign(body)[-1] != "0" else "1"),
        "stale": sign(body, t=t - 301),
        "future": sign(body, t=t + 301),
        "signed-other-body": sign(body + b" "),
        "unsigned-timestamp": f"t={t + 1},v1=" + sign(body).split("v1=")[1],
        "no-v1": f"t={t}",
        "non-numeric-t": "t=abc,v1=" + sign(body).split("v1=")[1],
        "huge-t": "t=" + "9" * 400 + ",v1=" + sign(body).split("v1=")[1],
    }


@pytest.mark.parametrize("name", sorted(_bad_signatures()))
def test_c_bad_or_missing_signature_is_401_and_nothing_runs(monkeypatch, tmp_path, name):
    client, sent, audit_path = make_app(monkeypatch, tmp_path)
    payload = event("memory_defense.triggered", status="block")
    resp = deliver(client, payload, signature=_bad_signatures()[name])
    assert resp.status_code == 401
    assert sent == [] and processed_rows(audit_path) == []
    rows = webhook_rows(audit_path)
    assert len(rows) == 1 and rows[0]["decision"] == "deny" and rows[0]["reason"].startswith("webhook-signature:")
    assert SECRET not in audit_path.read_text(encoding="utf-8")


def test_c_body_only_signature_headers_are_not_accepted(monkeypatch, tmp_path):
    """X-Hindsight-Signature / X-Hub-Signature-256 은 본문만 서명해 재생을 못 막는다(문서 43-45행): V2 만 받는다."""
    client, sent, audit_path = make_app(monkeypatch, tmp_path)
    payload = event("memory_defense.triggered", status="block")
    body = json.dumps(payload).encode("utf-8")
    mac = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = client.post(PATH, content=body, headers={"x-hindsight-signature": mac, "x-hub-signature-256": mac})
    assert resp.status_code == 401 and sent == [] and processed_rows(audit_path) == []


def test_c_no_secret_configured_never_processes(monkeypatch, tmp_path):
    client, sent, audit_path = make_app(monkeypatch, tmp_path, secret=None)
    resp = deliver(client, event("memory_defense.triggered", status="block"))
    assert resp.status_code == 503 and sent == [] and processed_rows(audit_path) == []
    client, sent, audit_path = make_app(monkeypatch, tmp_path, secret="")
    assert deliver(client, event("memory_defense.triggered", status="block")).status_code == 503


def test_c_signature_edge_is_inclusive_at_300_seconds(monkeypatch, tmp_path):
    client, _, _ = make_app(monkeypatch, tmp_path)
    for i, t in enumerate((int(NOW) - 300, int(NOW) + 300)):
        payload = event(event_id=f"edge-{i}")
        body = json.dumps(payload).encode("utf-8")
        resp = client.post(PATH, content=body, headers={"x-hindsight-signature-v2": sign(body, t=t),
                                                        "x-hindsight-event-id": f"edge-{i}"})
        assert resp.status_code == 200


# --- (d) 중복 거름: 키는 event_id, operation_id 는 감사 줄에만 ---------------------------------------

def test_d_same_event_id_twice_second_is_not_processed(monkeypatch, tmp_path):
    client, sent, audit_path = make_app(monkeypatch, tmp_path)
    payload = event("memory_defense.triggered", status="block", event_id="ev-dup", operation_id="op-dup")
    first = deliver(client, payload)
    second = deliver(client, payload)
    assert first.status_code == 200 and first.json() == {"status": "processed"}
    assert second.status_code == 200 and second.json() == {"status": "duplicate"}
    assert len(processed_rows(audit_path)) == 1 and len(sent) == 1
    assert [r["reason"] for r in webhook_rows(audit_path) if r["decision"] == "allow"] == ["duplicate:ev-dup"]


def test_d_same_operation_id_with_different_event_id_is_processed_each(monkeypatch, tmp_path):
    """배치 retain 은 문서마다 이벤트 하나를 같은 operation_id 로 쏜다(문서 25행): operation_id 로 거르면 알림을 삼킨다."""
    client, sent, audit_path = make_app(monkeypatch, tmp_path)
    for n in range(3):
        resp = deliver(client, event("memory_defense.triggered", status="block", event_id=f"ev-{n}", operation_id="op-batch"))
        assert resp.status_code == 200 and resp.json() == {"status": "processed"}
    assert len(processed_rows(audit_path)) == 3 and len(sent) == 3
    assert {r["webhook"]["operation_id"] for r in processed_rows(audit_path)} == {"op-batch"}


@pytest.mark.parametrize("header", ["auto", None])
def test_d_event_id_comes_from_the_signed_body_header_is_optional(monkeypatch, tmp_path, header):
    client, sent, _ = make_app(monkeypatch, tmp_path)
    payload = event("memory_defense.triggered", status="block", event_id="ev-where")
    assert deliver(client, payload, event_id_header=header).json() == {"status": "processed"}
    assert deliver(client, payload, event_id_header=header).json() == {"status": "duplicate"}
    assert len(sent) == 1


def test_d_header_event_id_must_match_the_signed_body(monkeypatch, tmp_path):
    """X-Hindsight-Event-Id 는 서명 범위 밖이다. 헤더만 바꿔 같은 배달을 새 이벤트로 보이게 할 수 없다."""
    client, sent, audit_path = make_app(monkeypatch, tmp_path)
    payload = event("memory_defense.triggered", status="block", event_id="ev-real")
    assert deliver(client, payload, event_id_header="ev-forged").status_code == 400
    assert sent == [] and processed_rows(audit_path) == []
    assert deliver(client, payload).json() == {"status": "processed"}        # 진짜 이벤트는 막히지 않았다
    assert deliver(client, payload, event_id_header="ev-forged").status_code == 400


def test_d_header_only_event_id_is_processed_and_deduplicated(monkeypatch, tmp_path):
    """승인된 키 순서: X-Hindsight-Event-Id 헤더, 없으면 본문 event_id. 본문에 event_id 가 없어도 헤더가 있으면 처리한다."""
    client, sent, audit_path = make_app(monkeypatch, tmp_path)
    payload = event("memory_defense.triggered", status="block")
    del payload["event_id"]
    assert deliver(client, payload, event_id_header="ev-header-only").json() == {"status": "processed"}
    assert deliver(client, payload, event_id_header="ev-header-only").json() == {"status": "duplicate"}
    assert deliver(client, payload, event_id_header="ev-header-other").json() == {"status": "processed"}
    assert len(sent) == 2 and processed_rows(audit_path)[0]["webhook"]["event_id"] == "ev-header-only"


def test_d_no_event_id_anywhere_is_400(monkeypatch, tmp_path):
    client, sent, _ = make_app(monkeypatch, tmp_path)
    payload = event("memory_defense.triggered", status="block")
    del payload["event_id"]
    assert deliver(client, payload, event_id_header=None).status_code == 400 and sent == []
    assert deliver(client, payload, event_id_header="").status_code == 400 and sent == []


def test_d_failed_audit_write_releases_the_event_id_so_redelivery_is_processed(monkeypatch, tmp_path):
    """감사 기록이 안 남았으면 처리된 것이 아니다: 첫 요청이 500이어도 재배달이 duplicate 로 삼켜지지 않는다."""
    client, sent, audit_path = make_app(monkeypatch, tmp_path)
    flaky = TestClient(client.app, raise_server_exceptions=False)
    real, calls = Audit.record, []

    def record(self, **kw):
        calls.append(kw["decision"])
        if kw["decision"] == "webhook" and calls.count("webhook") == 1:
            raise OSError("disk full")
        return real(self, **kw)

    monkeypatch.setattr(Audit, "record", record)
    payload = event("memory_defense.triggered", status="block", event_id="ev-disk")
    assert deliver(flaky, payload).status_code == 500 and sent == []
    assert deliver(flaky, payload).json() == {"status": "processed"}
    assert len(sent) == 1 and len(processed_rows(audit_path)) == 1
    assert deliver(flaky, payload).json() == {"status": "duplicate"}


def test_d_unsigned_duplicate_does_not_poison_the_dedup_table(monkeypatch, tmp_path):
    """검증 전에는 중복 표에 아무것도 넣지 않는다: 서명 없는 요청이 같은 event_id 를 선점해 진짜 이벤트를 막을 수 없다."""
    client, sent, _ = make_app(monkeypatch, tmp_path)
    payload = event("memory_defense.triggered", status="block", event_id="ev-poison")
    assert deliver(client, payload, signature=None).status_code == 401
    assert deliver(client, payload).json() == {"status": "processed"} and len(sent) == 1


# --- (e) 알림 ----------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,status,alert", [
    ("consolidation.completed", "completed", False),
    ("consolidation.completed", "failed", True),
    ("memory_defense.triggered", "redact", False),
    ("memory_defense.triggered", "block", True),
])
def test_e_only_failure_or_block_notifies_and_names_the_event(monkeypatch, tmp_path, kind, status, alert):
    client, sent, audit_path = make_app(monkeypatch, tmp_path)
    data = None
    if kind == "consolidation.completed" and status == "failed":
        data = {"observations_created": None, "observations_updated": None, "observations_deleted": None,
                "error_message": "llm failed with " + FAKE_TOKEN}
    assert deliver(client, event(kind, status=status, data=data, operation_id="op-alert")).status_code == 200
    assert len(sent) == (1 if alert else 0)
    assert processed_rows(audit_path)[0]["webhook"]["notify"] == ("pending" if alert else "none")
    assert [r["webhook"]["notify"] for r in notify_rows(audit_path)] == (["ok"] if alert else [])
    if alert:
        text = sent[0]
        assert kind in text and status in text and "personal-alice" in text and "op-alert" in text
        assert SECRET not in text and FAKE_TOKEN not in text
        assert text.startswith("[hindsight-crew]") and len(text) <= 1900


def test_e_token_cut_by_the_error_length_cap_is_still_scrubbed(monkeypatch, tmp_path):
    """160자 컷이 토큰을 반으로 가르면 scrub_secrets 모양에 안 맞는다: 컷 전에 마스킹한다."""
    client, sent, _ = make_app(monkeypatch, tmp_path)
    data = {"observations_created": None, "observations_updated": None, "observations_deleted": None,
            "error_message": "x" * 149 + " " + FAKE_TOKEN}
    assert deliver(client, event(status="failed", data=data)).status_code == 200
    assert "ghp_" not in sent[0] and "[redacted]" in sent[0]


@pytest.mark.parametrize("matched", [5, True, "github_token", {"a": 1}, None, [], ["github_token", 7]])
def test_e_block_alert_survives_any_matched_types_shape(monkeypatch, tmp_path, matched):
    client, sent, audit_path = make_app(monkeypatch, tmp_path)
    block = event("memory_defense.triggered", status="block")
    block["data"]["matched_types"] = matched
    assert deliver(client, block).status_code == 200
    assert len(sent) == 1 and "memory_defense.triggered" in sent[0]
    assert [r["webhook"]["notify"] for r in notify_rows(audit_path)] == ["ok"]


def test_e_audit_line_is_written_before_the_alert_is_sent(monkeypatch, tmp_path):
    client, _, audit_path = make_app(monkeypatch, tmp_path)
    seen = []

    async def notifier(content):
        seen.append([r["decision"] for r in webhook_rows(audit_path)])
        return "ok"

    client.app.state.webhook_notifier = notifier
    assert deliver(client, event("memory_defense.triggered", status="block")).status_code == 200
    assert seen == [["webhook"]]
    assert [r["decision"] for r in webhook_rows(audit_path)] == ["webhook", "webhook-notify"]


def test_e_alert_text_differs_per_event_kind(monkeypatch, tmp_path):
    client, sent, _ = make_app(monkeypatch, tmp_path)
    failed = {"observations_created": None, "observations_updated": None, "observations_deleted": None,
              "error_message": "x"}
    deliver(client, event("consolidation.completed", status="failed", data=failed, event_id="e1"))
    deliver(client, event("memory_defense.triggered", status="block", event_id="e2"))
    assert "consolidation.completed" in sent[0] and "memory_defense.triggered" not in sent[0]
    assert "memory_defense.triggered" in sent[1] and "consolidation.completed" not in sent[1]


def test_e_notifier_failure_is_audited_and_still_200(monkeypatch, tmp_path):
    client, _, audit_path = make_app(monkeypatch, tmp_path)

    async def failing(content):
        return "failed:ConnectError"

    client.app.state.webhook_notifier = failing
    resp = deliver(client, event("memory_defense.triggered", status="block"))
    assert resp.status_code == 200
    assert [r["webhook"]["notify"] for r in notify_rows(audit_path)] == ["failed:ConnectError"]


def test_e_default_notifier_posts_through_the_discord_helper(monkeypatch, tmp_path):
    """notifier 를 주입하지 않으면 recurrence 의 전송 도우미를 쓴다. 실제 호출은 없다(도우미를 바꿔 끼운다)."""
    client, _, audit_path = make_app(monkeypatch, tmp_path)
    del client.app.state.webhook_notifier
    calls = []

    async def fake_post(content, *, client=None):
        calls.append(content)
        return "skipped:missing_env"

    monkeypatch.setattr(recurrence, "post_discord_message", fake_post)
    assert deliver(client, event("memory_defense.triggered", status="block")).status_code == 200
    assert len(calls) == 1 and "memory_defense.triggered" in calls[0]
    assert [r["webhook"]["notify"] for r in notify_rows(audit_path)] == ["skipped:missing_env"]


# --- (f) 전송 도우미 분리 뒤에도 notify_discord 는 같다 --------------------------------------------------

def _draft():
    return {"draft_id": 7, "signal": "gotcha-recurrence", "bank": "personal-alice", "state": "ready-for-review",
            "evidence": {"similar_memory_id": "abcdef0123456789", "similar_score": 0.97,
                         "rescue_query_source": "ledger", "new_content_preview": "새 기억 " + FAKE_TOKEN},
            "trigger": {"recall": {"query": "배포 순서 확인"}, "all_of": ["deploy", "gateway"]}}


def _capture_post(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.content, dict(request.headers)))
        return httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def test_f_notify_discord_request_is_byte_identical_to_the_pre_split_form(monkeypatch):
    import asyncio

    monkeypatch.setenv("HC_NOTIFY_DISCORD_BOT_TOKEN", "bot-" + secrets.token_hex(4))
    monkeypatch.setenv("HC_NOTIFY_DISCORD_CHANNEL_ID", "123456789")
    client, seen = _capture_post(monkeypatch)
    draft = _draft()
    result = asyncio.run(recurrence.notify_discord(draft, client=client))
    assert result == "ok" and len(seen) == 1
    url, content, headers = seen[0]
    # 분리 전 구현: json.dumps({"content": discord_message(draft)}).encode("utf-8")
    assert url == "https://discord.com/api/v10/channels/123456789/messages"
    assert content == json.dumps({"content": recurrence.discord_message(draft)}).encode("utf-8")
    assert headers["user-agent"] == "DiscordBot (hindsight-crew context-triggers, 1.0)"
    assert headers["content-type"] == "application/json"
    assert headers["authorization"] == "Bot " + __import__("os").environ["HC_NOTIFY_DISCORD_BOT_TOKEN"]
    assert FAKE_TOKEN not in content.decode("utf-8")


@pytest.mark.parametrize("env,expected", [
    ({}, "skipped:missing_env"),
    ({"HC_NOTIFY_DISCORD_BOT_TOKEN": "t"}, "skipped:missing_env"),
    ({"HC_NOTIFY_DISCORD_CHANNEL_ID": "1"}, "skipped:missing_env"),
])
def test_f_notify_discord_skips_without_credentials(monkeypatch, env, expected):
    import asyncio

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    client, seen = _capture_post(monkeypatch)
    assert asyncio.run(recurrence.notify_discord(_draft(), client=client)) == expected and seen == []


def test_f_notify_discord_builds_the_message_only_with_credentials_and_inside_the_try(monkeypatch):
    """분리 전에는 discord_message(draft)가 자격 확인 뒤, try 안에서 불렸다: 깨진 초안도 던지지 않고 failed:<class> 다."""
    import asyncio

    broken = dict(_draft(), evidence=None)
    client, seen = _capture_post(monkeypatch)
    assert asyncio.run(recurrence.notify_discord(broken, client=client)) == "skipped:missing_env"
    monkeypatch.setenv("HC_NOTIFY_DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("HC_NOTIFY_DISCORD_CHANNEL_ID", "1")
    assert asyncio.run(recurrence.notify_discord(broken, client=client)) == "failed:AttributeError" and seen == []


def test_f_notify_discord_status_and_exception_results_are_unchanged(monkeypatch):
    import asyncio

    monkeypatch.setenv("HC_NOTIFY_DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("HC_NOTIFY_DISCORD_CHANNEL_ID", "1")
    forbidden = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    assert asyncio.run(recurrence.notify_discord(_draft(), client=forbidden)) == "failed:status_403"

    def boom(request):
        raise httpx.ConnectError("no route")

    broken = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    assert asyncio.run(recurrence.notify_discord(_draft(), client=broken)) == "failed:ConnectError"


def test_f_send_helper_takes_free_form_content_with_same_env_ua_scrub_and_cap(monkeypatch):
    import asyncio

    monkeypatch.setenv("HC_NOTIFY_DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("HC_NOTIFY_DISCORD_CHANNEL_ID", "42")
    client, seen = _capture_post(monkeypatch)
    text = "[hindsight-crew] 웹훅 " + FAKE_TOKEN + " " + "가" * 3000
    assert asyncio.run(recurrence.post_discord_message(text, client=client)) == "ok"
    url, content, headers = seen[0]
    sent = json.loads(content)["content"]
    assert url.endswith("/channels/42/messages") and FAKE_TOKEN not in sent and len(sent) <= 1900
    assert headers["user-agent"] == "DiscordBot (hindsight-crew context-triggers, 1.0)"
    # 자격 값이 없으면 보내지 않는다. 예외도 던지지 않는다.
    monkeypatch.delenv("HC_NOTIFY_DISCORD_BOT_TOKEN")
    assert asyncio.run(recurrence.post_discord_message("x", client=client)) == "skipped:missing_env"


def test_f_webhook_alert_cannot_ping_but_notify_discord_body_is_untouched(monkeypatch):
    import asyncio

    monkeypatch.setenv("HC_NOTIFY_DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("HC_NOTIFY_DISCORD_CHANNEL_ID", "42")
    client, seen = _capture_post(monkeypatch)
    assert asyncio.run(recurrence.post_discord_message("문서: @everyone <@123>", client=client)) == "ok"
    body = json.loads(seen[0][1])
    assert body["allowed_mentions"] == {"parse": []} and "@everyone" in body["content"]
    seen.clear()
    assert asyncio.run(recurrence.notify_discord(_draft(), client=client)) == "ok"
    assert list(json.loads(seen[0][1])) == ["content"]


# --- compose 배선 ------------------------------------------------------------------------------

def test_compose_wires_the_flag_with_default_off_and_no_secret_literal():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s+HC_WEBHOOK_RECEIVER: \$\{HC_WEBHOOK_RECEIVER:-0\}\s*$", compose, re.M)
    pending = compose.index("HC_PENDING_TRIGGERS:")
    assert compose.index("HC_WEBHOOK_RECEIVER:") > pending
    # 서명 비밀은 .env.gateway.local(env_file)에서만 온다: compose environment 에 값 줄이 없다.
    assert not re.search(r"^\s+HC_WEBHOOK_SECRET\s*:", compose, re.M)
    assert "HC_WEBHOOK_SECRET" in (ROOT / ".env.example").read_text(encoding="utf-8")
