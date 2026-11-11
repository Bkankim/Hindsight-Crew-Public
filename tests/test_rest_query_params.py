"""REST 프록시의 쿼리 파라미터 보존 - 반복 키 붕괴 회귀 방지.

`dict(request.query_params)`는 반복 키의 마지막 값만 남긴다(?tags=a&tags=b → tags=b).
hindsight의 리스트형 파라미터(tags 등)가 조용히 넓어진 필터로 전달되어 200 OK로
그럴듯한 오답을 돌려주던 실사고(2026-09-18, 이슈 #10 감사 스크립트)가 동기다.
게이트웨이는 반복 키를 순서까지 그대로 upstream에 전달해야 한다.
"""
from fastapi.testclient import TestClient


def test_repeated_query_params_are_forwarded_intact(make_app, recorder):
    app = make_app()
    with TestClient(app) as client:
        r = client.get(
            "/v1/default/banks/personal-alice/memories/list"
            "?tags=project:x&tags=kind:decision&tags_match=all_strict&limit=1",
            headers={"Authorization": "Bearer tok-alice"},
        )
    assert r.status_code == 200
    q = recorder.last["query"]
    assert "tags=project%3Ax" in q and "tags=kind%3Adecision" in q, q
    assert q.index("tags=project%3Ax") < q.index("tags=kind%3Adecision"), q
    assert "tags_match=all_strict" in q and "limit=1" in q


def test_single_value_params_unchanged(make_app, recorder):
    app = make_app()
    with TestClient(app) as client:
        r = client.get(
            "/v1/default/banks/personal-alice/operations?status=pending&type=consolidation",
            headers={"Authorization": "Bearer tok-alice"},
        )
    assert r.status_code == 200
    q = recorder.last["query"]
    assert "status=pending" in q and "type=consolidation" in q
