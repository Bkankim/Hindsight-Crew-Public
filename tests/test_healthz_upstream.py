"""`/healthz` 가 업스트림 상태를 드러내는지 검증한다.

왜 필요한가: 2026-07-26 메인 `hindsight` 컨테이너가 약 2시간 정지해 있는 동안
`/healthz` 는 계속 `200 {"ok":true}` 를 반환했다. 이 엔드포인트가 자기 자신만
설명했기 때문이고, 그래서 recall 이 죽은 것을 아무도 몰랐다. 무증상 장애는
장애보다 나쁘다.

설계상 중요한 점은 `ok` 를 건드리지 않았다는 것이다. `/healthz` 는 게이트웨이
자신의 liveness 이고, 업스트림이 죽었다고 게이트웨이를 재시작하는 것은 잘못된
처방이다. 그래서 업스트림 상태는 별도 필드로만 노출한다. 아래 테스트가 그
경계를 고정한다.
"""
from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from conftest import ACL_DATA, UPSTREAM_KEY

from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit


def _client(handler, tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="http://upstream", timeout=5.0)
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=http)
    return TestClient(app)


def test_healthy_upstream_is_reported(tmp_path):
    def handler(request):
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "healthy", "database": "connected"})

    body = _client(handler, tmp_path).get("/healthz").json()
    assert body["ok"] is True
    assert body["rewrite_failures"] == 0
    assert body["upstream_ok"] is True
    assert body["upstream"] == "ok"


def test_upstream_error_status_is_visible_but_ok_stays_true(tmp_path):
    """업스트림이 5xx를 내도 `ok` 는 True 다.

    이게 이 변경의 핵심 경계다. `ok` 를 False 로 떨어뜨리면 게이트웨이를
    감시하는 쪽이 게이트웨이를 재시작하기 시작하는데, 정작 고장난 것은
    업스트림이다. 보이게 만들되 처방을 오도하지 않는다.
    """
    def handler(request):
        return httpx.Response(503, json={"detail": "database unavailable"})

    body = _client(handler, tmp_path).get("/healthz").json()
    assert body["ok"] is True
    assert body["upstream_ok"] is False
    assert body["upstream"] == "status_503"


def test_unreachable_upstream_reports_class_name_only(tmp_path):
    """도달 불가는 예외 클래스명만 남긴다 - 업스트림 본문은 절대 싣지 않는다.

    위협모델상 recall 응답 본문이 로그·모니터로 새면 안 되므로, 진단 문자열은
    거친 분류로 제한한다.
    """
    def handler(request):
        raise httpx.ConnectError("all connection attempts failed")

    body = _client(handler, tmp_path).get("/healthz").json()
    assert body["ok"] is True
    assert body["upstream_ok"] is False
    assert body["upstream"] == "ConnectError"
    assert "connection attempts" not in str(body)


def test_probe_is_cached_so_health_polling_is_not_upstream_load(tmp_path):
    """헬스 폴링이 업스트림 부하로 번지지 않아야 한다.

    모니터가 초당 여러 번 때려도 업스트림에는 TTL 안에서 한 번만 간다.
    """
    hits = []

    def handler(request):
        hits.append(request.url.path)
        return httpx.Response(200, json={"status": "healthy"})

    client = _client(handler, tmp_path)
    for _ in range(5):
        assert client.get("/healthz").json()["upstream_ok"] is True
    assert len(hits) == 1, f"expected a single cached probe, got {len(hits)}"
