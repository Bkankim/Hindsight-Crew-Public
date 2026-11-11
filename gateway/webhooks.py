"""upstream Hindsight 웹훅 수신 경로 (issue #303) - POST /webhooks/hindsight, 기능 플래그 HC_WEBHOOK_RECEIVER 기본 꺼짐.

통합 완료·memory_defense 차단이 일어나도 아무도 모르던 틈을 메운다. 플래그가 꺼져 있으면 라우트 자체를 등록하지
않는다(404, 다른 표면은 그대로). 켜면 한 요청이 이 순서로만 진행한다.

  1. 검증   X-Hindsight-Signature-V2 = `t=<unix_seconds>,v1=<hex>`, HMAC-SHA256(secret, `<t>.<raw body>`),
            |now - t| <= 300초. 비밀은 env HC_WEBHOOK_SECRET(게이트웨이 env_file .env.gateway.local)에서만 읽는다.
            실패는 401, 비밀 미설정은 503. 검증 전에는 본문을 해석하지 않고 중복 표에도 아무것도 넣지 않는다.
  2. 중복   서명된 본문의 event_id 로 거른다. X-Hindsight-Event-Id 헤더는 서명 범위 밖이라 있으면 본문과 같아야 한다.
            operation_id 로는 거르지 않는다: 배치 retain 은 문서마다 이벤트 하나를 같은 operation_id 로 쏜다.
  3. 기록   감사 로그 한 줄(이벤트 종류, operation_id, 뱅크, 결과 요약). 본문 원문은 남기지 않는다.
  4. 알림   실패한 통합·block 된 memory_defense 만 디스코드로(recurrence.post_discord_message).

근거 (upstream v1.1.0 hindsight-docs/docs/developer/api/webhooks.mdx): 25행 event_id 중복 거름, 39행 V2 형식,
43-45행 타임스탬프 허용 폭, 78-110행 consolidation.completed, 152-189행 memory_defense.triggered.

서명 비밀과 디스코드 자격 값은 어떤 로그·알림 본문에도 쓰지 않는다. 알림 본문은 post_discord_message 가 한 번 더
scrub_secrets 를 거친다(alert_text 도 직접 거쳐서, 바꿔 끼운 notifier 도 같은 본문을 받는다).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import recurrence
from .audit import Audit, cut_field, field_keys
from .context_triggers import scrub_secrets

ENV_FLAG = "HC_WEBHOOK_RECEIVER"
ENV_SECRET = "HC_WEBHOOK_SECRET"
PATH = "/webhooks/hindsight"
SIGNATURE_HEADER = "x-hindsight-signature-v2"
EVENT_ID_HEADER = "x-hindsight-event-id"
TOLERANCE_SECONDS = 300          # webhooks.mdx 44행: "five minutes is a common default"
DEDUP_TTL_SECONDS = 24 * 3600    # upstream 재시도는 첫 실패 뒤 약 7.6시간까지(webhooks.mdx 13-20행)
IDENTITY = "upstream-webhook"
TOOL = "webhooks"
ALERT_ERROR_MAX = 160

CONSOLIDATION = "consolidation.completed"
MEMORY_DEFENSE = "memory_defense.triggered"
HANDLED_EVENTS = (CONSOLIDATION, MEMORY_DEFENSE)

WebhookNotifier = Callable[[str], Awaitable[str]]


def enabled() -> bool:
    """기본 꺼짐. `HC_WEBHOOK_RECEIVER=1` 일 때만 라우트를 등록한다(create_app 시점에 한 번 읽는다)."""
    return os.environ.get(ENV_FLAG, "0").strip().lower() in {"1", "true", "yes", "on"}


def verify_signature(secret: str, body: bytes, header: Optional[str], now: float) -> Tuple[bool, str]:
    """(ok, reason). reason 은 감사 줄용 짧은 표지이고 헤더 값이나 비밀은 담지 않는다."""
    if not header:
        return False, "missing"
    try:
        parts = dict(p.strip().split("=", 1) for p in header.split(","))
        stamp, received = parts["t"], parts["v1"]
        t = int(stamp)
    except (ValueError, KeyError):
        return False, "malformed"
    try:
        stale = abs(now - t) > TOLERANCE_SECONDS
    except OverflowError:                    # t=<hundreds of digits>: float - int overflows, still a bad signature
        return False, "malformed"
    if stale:
        return False, "stale-timestamp"
    expected = hmac.new(secret.encode("utf-8"), f"{stamp}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode("ascii"), received.encode("utf-8", "replace")):
        return False, "mismatch"
    return True, "ok"


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def is_alert(event: str, status: Optional[str]) -> bool:
    """알림 대상: 통합 실패, memory_defense block. redact 는 비밀이 지워진 채 저장됐으므로 기록만 한다."""
    return (event == CONSOLIDATION and status == "failed") or (event == MEMORY_DEFENSE and status == "block")


def summarize(event: str, data: Any) -> Dict[str, Any]:
    """감사 줄용 결과 요약. 숫자·짧은 표지만 담고 오류 문구나 message 본문은 담지 않는다."""
    data = data if isinstance(data, dict) else {}
    if event == CONSOLIDATION:
        out: Dict[str, Any] = {k: data[k] for k in ("observations_created", "observations_updated", "observations_deleted")
                               if _count(data.get(k)) is not None}
        if data.get("error_message"):
            out["has_error"] = True
        return out
    out = {}
    for key in ("action", "detector", "document_id"):
        value = _text(data.get(key))
        if value is not None:
            out[key] = cut_field(value)[0]
    types = data.get("matched_types")
    if isinstance(types, list):
        out["matched_types"] = [cut_field(t)[0] for t in types[:16] if isinstance(t, str)]
    return out


def alert_text(event: str, *, status: Optional[str], bank: str, operation_id: str, data: Any) -> str:
    data = data if isinstance(data, dict) else {}
    lines = [f"[hindsight-crew] 웹훅 경보 - {event} ({status})",
             f"- 뱅크: {bank}",
             f"- operation_id: {operation_id}"]
    if event == CONSOLIDATION:
        error = _text(data.get("error_message"))
        # scrub before the cut: a token cut in half no longer matches its secret shape
        lines.append("- 통합이 실패했다" + (f": {scrub_secrets(error)[:ALERT_ERROR_MAX]}" if error else ""))
    else:
        raw_types = data.get("matched_types")
        if isinstance(raw_types, (list, tuple)):
            types = [str(t) for t in raw_types[:8]]
        else:                                    # 스키마가 달라져도 block 알림은 나간다: 문자열화
            types = [str(raw_types)] if raw_types else []
        types = [cut_field(t)[0] for t in types]
        lines.append(f"- 저장이 차단됐다 (탐지: {_text(data.get('detector')) or '-'}, 유형: {', '.join(types) or '-'})")
        if _text(data.get("document_id")):
            lines.append(f"- 문서: {data['document_id']}")
    return scrub_secrets("\n".join(lines))[:recurrence.DISCORD_MESSAGE_MAX]


def register(app: FastAPI, *, audit: Audit, clock: Callable[[], float]) -> bool:
    """플래그가 켜져 있을 때만 POST /webhooks/hindsight 를 등록한다. 등록했으면 True.

    알림 함수는 app.state.webhook_notifier(content -> 'ok' | 'skipped:..' | 'failed:..', 던지지 않음)로 바꿔
    끼울 수 있고, 없으면 recurrence.post_discord_message 다."""
    if not enabled():
        return False
    app.state.webhook_dedup = recurrence.Deduper(ttl=DEDUP_TTL_SECONDS)

    def record(decision: str, reason: str, *, bank: Optional[str] = None, webhook: Optional[dict] = None) -> None:
        audit.record(token=None, identity=IDENTITY, method="POST", tool=TOOL, attempted_bank=bank,
                     resolved_bank=bank, decision=decision, reason=reason,
                     extra={"webhook": webhook} if webhook else None)

    @app.post(PATH)
    async def webhook_receiver(request: Request):
        secret = os.environ.get(ENV_SECRET, "")
        raw = await request.body()
        if not secret:
            record("deny", "webhook-signature:secret-not-configured")
            return JSONResponse({"error": "webhook secret not configured"}, status_code=503)
        ok, why = verify_signature(secret, raw, request.headers.get(SIGNATURE_HEADER), clock())
        if not ok:
            record("deny", f"webhook-signature:{why}")
            return JSONResponse({"error": "invalid signature"}, status_code=401)

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            record("deny", "webhook-body:bad-json")
            return JSONResponse({"error": "bad json"}, status_code=400)

        event = _text(payload.get("event")) or ""
        body_id = _text(payload.get("event_id")) or ""
        header_id = request.headers.get(EVENT_ID_HEADER) or ""
        # 키는 헤더, 없으면 본문 event_id. 둘 다 있는데 다르면 거절한다(헤더는 서명 범위 밖이라 본문과 맞아야 한다).
        event_id = header_id or body_id
        if not event_id or (header_id and body_id and header_id != body_id):
            record("deny", "webhook-body:bad-event-id")
            return JSONResponse({"error": "event_id required; X-Hindsight-Event-Id and the body event_id must match"},
                                status_code=400)
        if event not in HANDLED_EVENTS:
            record("allow", f"ignored:{cut_field(event)[0]}")
            return JSONResponse({"status": "ignored"})

        bank_raw = _text(payload.get("bank_id")) or ""
        op_raw = _text(payload.get("operation_id")) or ""
        status = _text(payload.get("status"))
        bank, bank_cut = cut_field(bank_raw)
        operation_id, op_cut = cut_field(op_raw)
        if not app.state.webhook_dedup.first_time(event_id):
            record("allow", f"duplicate:{cut_field(event_id)[0]}", bank=bank or None)
            return JSONResponse({"status": "duplicate"})

        data = payload.get("data")
        alert = is_alert(event, status)
        # 감사 한 줄이 먼저다: 알림(디스코드 POST, 최대 15초) 도중 프로세스가 죽어도 이벤트 기록은 남는다.
        # 알림 결과는 알림이 있을 때만 뒤따르는 `webhook-notify` 줄에 적는다(notify 는 처리 줄에서 "pending").
        ident = {"event": event, "event_id": cut_field(event_id)[0], "operation_id": operation_id, "bank": bank,
                 **field_keys("bank", bank_cut), **field_keys("operation_id", op_cut)}
        try:
            record("webhook", f"{event}:{cut_field(status or '-')[0]}", bank=bank or None, webhook={
                **ident, "status": cut_field(status)[0] if status else None, "summary": summarize(event, data),
                "notify": "pending" if alert else "none"})
        except Exception:
            # 기록이 없으면 처리된 것이 아니다: 예약을 풀어야 upstream 재배달이 다시 처리된다(duplicate 로 삼켜지지 않게).
            app.state.webhook_dedup.forget(event_id)
            raise
        if alert:
            notifier: WebhookNotifier = getattr(app.state, "webhook_notifier", None) or recurrence.post_discord_message
            try:
                notify = await notifier(alert_text(event, status=status, bank=bank, operation_id=operation_id,
                                                   data=data))
            except Exception as exc:  # noqa: BLE001 - alerting is best-effort, the event is already recorded
                notify = f"failed:{type(exc).__name__}"
            record("webhook-notify", f"notify:{cut_field(notify)[0]}", bank=bank or None,
                   webhook={**ident, "notify": cut_field(notify)[0]})
        return JSONResponse({"status": "processed"})

    return True
