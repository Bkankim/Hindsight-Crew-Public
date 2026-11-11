"""이슈 #458 (b): 이 프로젝트가 만드는 upstream 키 형식(`hs_` + hex, scripts/seed-demo.sh)도
공통 시크릿 여과(gateway.context_triggers.scrub_secrets)에 걸린다.

값은 전부 실행 중에 조립한 합성 값이다(리포에 키 모양 리터럴을 두지 않는다).
여과기 단위 + 기록(recall 원장 파일, 게이트웨이 REST 경유) + 주입(build_injection) +
통지(재발 초안 Discord 본문, 웹훅 경보 본문) 경로를 함께 본다.
"""
import json

import pytest
from fastapi.testclient import TestClient

from gateway import context_triggers, recurrence, webhooks

# seed-demo.sh: KEY="hs_$(openssl rand -hex 24)" -> hs_ + 48 hex. 합성 값만 쓴다.
FAKE_HEX = "0a1b2c3d4e5f6789" * 3
FAKE_KEY = "hs_" + FAKE_HEX
FAKE_KEY_UPPER = "hs_" + FAKE_HEX.upper()


@pytest.mark.parametrize("text", [
    FAKE_KEY,
    f"key={FAKE_KEY}",
    f"Bearer {FAKE_KEY} 로 호출했다",
    f"{FAKE_KEY}로 붙였다",                       # 한글 조사가 바로 붙는 꼴
    f"HC_HINDSIGHT_API_KEY={FAKE_KEY_UPPER}",
    f"앞 '{FAKE_KEY}' 뒤",
])
def test_scrub_secrets_erases_hs_hex_key(text):
    out = context_triggers.scrub_secrets(text)
    assert FAKE_HEX not in out.lower()
    assert "[redacted]" in out


def test_scrub_secrets_keeps_short_hs_identifiers():
    """`hs_get`·`hs_call` 같은 짧은 식별자는 키가 아니다(벤치 코드에 실제로 있는 이름)."""
    text = "hs_get(path) 와 hs_call(c) 를 부른다"
    assert context_triggers.scrub_secrets(text) == text


def test_recall_ledger_file_has_no_hs_key(tmp_path):
    path = tmp_path / "recall-queries.log"
    ledger = recurrence.RecallLedger(str(path))
    ledger.record(identity="alice", bank="personal-alice", transport="rest",
                  query=f"upstream 키 {FAKE_KEY} 가 왜 401 인가", tags=["project:x", FAKE_KEY, f"note:{FAKE_KEY}"],
                  budget=f"low {FAKE_KEY}")
    raw = path.read_text(encoding="utf-8")
    assert raw.strip()
    assert FAKE_HEX not in raw
    assert "project:x" in raw                     # a clean tag is kept as sent


def test_rest_recall_through_gateway_does_not_log_hs_key(make_app, recorder, tmp_path):
    """게이트웨이 REST recall -> 원장 기록 경로 끝에서 끝까지(HC_RECALL_QUERY_LOG는 conftest가 tmp로)."""
    import httpx

    from conftest import Recorder
    base = Recorder.handler

    def handler(request):
        base(recorder, request)
        return httpx.Response(200, json={"results": [{"id": "mem-1", "text": "x"}]})

    recorder.handler = handler
    c = TestClient(make_app())
    r = c.post("/v1/default/banks/personal-alice/memories/recall",
               content=json.dumps({"query": f"키 {FAKE_KEY} 문제"}),
               headers={"authorization": "Bearer tok-alice", "content-type": "application/json"})
    assert r.status_code == 200
    raw = (tmp_path / "recall-queries.log").read_text(encoding="utf-8")
    assert "personal-alice" in raw
    assert FAKE_HEX not in raw


def test_injection_text_has_no_hs_key():
    memories = [{"id": "mem-00000001", "text": f"upstream 키는 {FAKE_KEY} 였다",
                 "mentioned_at": "2026-11-01T00:00:00Z"}]
    text, used, _tokens = context_triggers.build_injection(memories, max_memories=3, max_tokens=500)
    assert used == ["mem-00000001"]
    assert FAKE_HEX not in text


def test_discord_draft_message_has_no_hs_key():
    draft = {"draft_id": 1, "signal": "gotcha", "bank": "personal-alice",
             "evidence": {"new_content_preview": f"키 {FAKE_KEY} 노출", "rescue_query_source": "recall-log"},
             "trigger": {"recall": {"query": f"q {FAKE_KEY}"}, "all_of": ["x"]}}
    assert FAKE_HEX not in recurrence.discord_message(draft)


def test_webhook_alert_text_has_no_hs_key():
    text = webhooks.alert_text(webhooks.CONSOLIDATION, status="failed", bank="personal-alice",
                               operation_id="op-1", data={"error_message": f"auth {FAKE_KEY} rejected"})
    assert FAKE_HEX not in text


# --- rework #458-1 -------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", [
    "api_key%3D",          # URL 인코딩 "="
    "api_key%3d",
    "err\\n",              # 직렬화된 본문 속 문자열 이스케이프(백슬래시 + 문자)
    "col\\t",
], ids=["pct3D", "pct3d", "esc-n", "esc-t"])
def test_scrub_secrets_erases_hs_key_after_url_encoding_or_escape(prefix):
    out = context_triggers.scrub_secrets(prefix + FAKE_KEY)
    assert FAKE_HEX not in out.lower()
    assert "[redacted]" in out


@pytest.mark.parametrize("text", [
    "graphs_" + FAKE_HEX,      # 단어 속 "hs_": 키가 아니다(대조군)
    "paths_" + FAKE_HEX,
])
def test_scrub_secrets_keeps_word_internal_hs(text):
    assert context_triggers.scrub_secrets(text) == text


def test_ledger_row_with_scrubbed_tag_is_not_reusable(tmp_path):
    """여과로 tag가 바뀐 행은 재사용 불가: 초안이 "[redacted]"를 recall 필터로 재생하지 않는다."""
    path = tmp_path / "recall-queries.log"
    ledger = recurrence.RecallLedger(str(path))
    clean = ledger.record(identity="alice", bank="personal-alice", transport="rest",
                          query="cloudflare 로그인 실패 원인", tags=["project:x"], result_ids=["mem-1"])
    dirty = ledger.record(identity="alice", bank="personal-alice", transport="rest",
                          query="cloudflare 로그인 실패 원인", tags=["project:x", f"note:{FAKE_KEY}"],
                          result_ids=["mem-2"])
    assert clean.reusable is True
    assert dirty.reusable is False
    assert all(FAKE_HEX not in t for t in dirty.tags)
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert [r["reusable"] for r in rows] == [True, False]
    # 그 기억을 구한 recall로도 쓰이지 않는다(rescue_for는 reusable 행만 고른다).
    assert ledger.rescue_for(identity="alice", bank="personal-alice", memory_ids=["mem-2"]) is None
    assert ledger.rescue_for(identity="alice", bank="personal-alice", memory_ids=["mem-1"]) is clean
