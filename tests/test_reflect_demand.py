"""reflect demand telemetry (issue #11): metadata-only audit line per reflect call.

Contracts pinned here:
  1. One `reflect-demand` audit line per allowed reflect element, carrying tags + served
     mental-model ids + based_on state - and NEVER the query, context or answer text.
  2. The response bytes are passed through unchanged (telemetry is read-only).
  3. based_on omitted upstream -> "unknown" (not a miss); present but empty -> "miss".
  4. The REST surface (/v1/.../reflect) emits the same event with transport "rest".
  5. A non-reflect call emits no demand line.
"""
import json

import httpx
from fastapi.testclient import TestClient

from conftest import ACL_DATA, UPSTREAM_KEY
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit

SSE_CT = "text/event-stream"
SECRET_QUERY = "what should I do about the SECRET-QUERY-TEXT"
SECRET_ANSWER = "SECRET-ANSWER-TEXT you should ship it"


class Upstream:
    def __init__(self, body, content_type=SSE_CT, status=200):
        self.body, self.content_type, self.status = body, content_type, status

    def handler(self, request):
        return httpx.Response(self.status, content=self.body,
                              headers={"content-type": self.content_type,
                                       "mcp-session-id": "sess-upstream-1"})


def sse(payload):
    return b"event: message\r\ndata: " + json.dumps(payload).encode("utf-8") + b"\r\n\r\n"


def reflect_result(el_id, reflect_payload):
    return {"jsonrpc": "2.0", "id": el_id,
            "result": {"content": [{"type": "text", "text": json.dumps(reflect_payload)}],
                       "structuredContent": {"result": reflect_payload}, "isError": False}}


def make(upstream, tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler),
                             base_url="http://upstream", timeout=5.0)
    log = tmp_path / "audit.log"
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(log)),
                     upstream_key=UPSTREAM_KEY, http_client=http)
    return TestClient(app), log


def demand_lines(log):
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()
            if json.loads(ln)["decision"] == "reflect-demand"]


def call_reflect(client, args, el_id=7):
    body = {"jsonrpc": "2.0", "id": el_id, "method": "tools/call",
            "params": {"name": "reflect", "arguments": args}}
    return client.post("/mcp/personal-alice/", content=json.dumps(body),
                       headers={"content-type": "application/json",
                                "authorization": "Bearer tok-alice"})


def test_mcp_reflect_hit_logs_tags_and_models_without_text(tmp_path):
    payload = {"text": SECRET_ANSWER,
               "based_on": {"world": [{"id": "w1", "text": "raw fact text"}],
                            "mental-models": [{"id": "demo-rules-cypress-knot", "text": "model body"}]},
               "usage": {"input_tokens": 9237, "output_tokens": 10}}
    upstream = Upstream(sse(reflect_result(7, payload)))
    client, log = make(upstream, tmp_path)
    resp = call_reflect(client, {"query": SECRET_QUERY, "tags": ["project:cypress-knot"],
                                 "budget": "low", "include_based_on": True})
    assert resp.status_code == 200
    assert resp.content == upstream.body            # read-only: bytes untouched
    lines = demand_lines(log)
    assert len(lines) == 1
    ev = lines[0]["reflect"]
    assert ev == {"transport": "mcp", "tags": ["project:cypress-knot"], "tags_match": None,
                  "budget": "low", "ok": True, "based_on": "hit",
                  "mental_models": ["demo-rules-cypress-knot"], "input_tokens": 9237}
    assert lines[0]["reason"] == "based_on:hit"
    raw = log.read_text(encoding="utf-8")
    assert SECRET_QUERY not in raw and SECRET_ANSWER not in raw and "raw fact text" not in raw


def test_mcp_reflect_without_based_on_is_unknown_not_miss(tmp_path):
    upstream = Upstream(sse(reflect_result(7, {"text": SECRET_ANSWER, "usage": {"input_tokens": 3}})))
    client, log = make(upstream, tmp_path)
    call_reflect(client, {"query": SECRET_QUERY, "tags": ["topic:demo-brand"]})
    ev = demand_lines(log)[0]["reflect"]
    assert ev["based_on"] == "unknown" and ev["mental_models"] == [] and ev["ok"] is True


def test_mcp_reflect_with_empty_based_on_is_miss(tmp_path):
    upstream = Upstream(sse(reflect_result(7, {"text": "x", "based_on": {"world": [], "mental-models": []}})))
    client, log = make(upstream, tmp_path)
    call_reflect(client, {"query": "q", "tags": ["topic:x", "topic:y"], "tags_match": "all"})
    ev = demand_lines(log)[0]["reflect"]
    assert ev["based_on"] == "miss" and ev["tags"] == ["topic:x", "topic:y"] and ev["tags_match"] == "all"


def test_mcp_reflect_is_error_is_logged_not_ok(tmp_path):
    body = sse({"jsonrpc": "2.0", "id": 7,
                "result": {"content": [{"type": "text", "text": "upstream failed"}], "isError": True}})
    upstream = Upstream(body)
    client, log = make(upstream, tmp_path)
    resp = call_reflect(client, {"query": "q", "tags": ["project:z"]})
    assert resp.content == body
    ev = demand_lines(log)[0]["reflect"]
    assert ev["ok"] is False and ev["based_on"] == "unknown"


def test_non_reflect_call_emits_no_demand_line(tmp_path):
    upstream = Upstream(sse({"jsonrpc": "2.0", "id": 1, "result": {"content": [], "isError": False}}))
    client, log = make(upstream, tmp_path)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "recall", "arguments": {"query": "q", "tags": ["project:z"]}}}
    client.post("/mcp/personal-alice/", content=json.dumps(body),
                headers={"content-type": "application/json", "authorization": "Bearer tok-alice"})
    assert demand_lines(log) == []


def test_rest_reflect_emits_rest_event(tmp_path):
    payload = {"text": SECRET_ANSWER, "based_on": {"mental_models": [{"id": "demo-rules-branding"}]},
               "usage": {"input_tokens": 42}}
    upstream = Upstream(json.dumps(payload).encode("utf-8"), content_type="application/json")
    client, log = make(upstream, tmp_path)
    resp = client.post("/v1/default/banks/personal-alice/reflect",
                       content=json.dumps({"query": SECRET_QUERY, "tags": ["topic:demo-brand"],
                                           "budget": "low"}),
                       headers={"content-type": "application/json",
                                "authorization": "Bearer tok-alice"})
    assert resp.status_code == 200 and resp.content == upstream.body
    ev = demand_lines(log)[0]["reflect"]
    assert ev == {"transport": "rest", "tags": ["topic:demo-brand"], "tags_match": None,
                  "budget": "low", "ok": True, "based_on": "hit",
                  "mental_models": ["demo-rules-branding"], "input_tokens": 42}
    assert SECRET_QUERY not in log.read_text(encoding="utf-8")


# ---- 태그 개수 상한 (issue #103): reflect-demand 줄의 tags도 #99의 상한·키 관례를 따른다 ----

import pytest

TAG_CAP = 64        # gateway.audit.TAG_LOG_COUNT_MAX와 같은 값 - 상수를 바꾸면 이 테스트가 알려야 한다


class RecordingUpstream(Upstream):
    """upstream이 실제로 받은 요청 본문을 남긴다 - 감사 상한이 전달 태그를 건드리지 않음을 보이려는 것."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.bodies = []

    def handler(self, request):
        self.bodies.append(json.loads(request.content))
        return super().handler(request)


def reflect_with_tags(tmp_path, surface, tags):
    payload = {"text": SECRET_ANSWER, "based_on": {"mental_models": [{"id": "demo-rules-branding"}]},
               "usage": {"input_tokens": 42}}
    if surface == "mcp":
        upstream = RecordingUpstream(sse(reflect_result(7, payload)))
        client, log = make(upstream, tmp_path)
        assert call_reflect(client, {"query": SECRET_QUERY, "tags": tags}).status_code == 200
        sent = upstream.bodies[-1]["params"]["arguments"]["tags"]
    else:
        upstream = RecordingUpstream(json.dumps(payload).encode("utf-8"), content_type="application/json")
        client, log = make(upstream, tmp_path)
        resp = client.post("/v1/default/banks/personal-alice/reflect",
                           content=json.dumps({"query": SECRET_QUERY, "tags": tags}),
                           headers={"content-type": "application/json",
                                    "authorization": "Bearer tok-alice"})
        assert resp.status_code == 200 and resp.content == upstream.body
        sent = upstream.bodies[-1]["tags"]
    (line,) = demand_lines(log)
    return line, sent


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("n", [5000, 65, 64, 63])
def test_reflect_demand_tags_are_capped_with_count_and_omitted_while_upstream_keeps_all(
        tmp_path, surface, n):
    tags = [f"topic:t{i}" for i in range(n)]

    line, sent_upstream = reflect_with_tags(tmp_path, surface, tags)

    assert sent_upstream == tags                                        # upstream으로 가는 태그는 그대로
    ev = line["reflect"]
    assert ev["tags"] == tags[:TAG_CAP]
    if n <= TAG_CAP:                                                    # 경계: 정확히 64개면 키 추가 없음
        assert "tags_count" not in ev and "tags_omitted" not in ev
    else:
        assert ev["tags_count"] == n and ev["tags_omitted"] == n - TAG_CAP
    assert ev["based_on"] == "hit" and ev["mental_models"] == ["demo-rules-branding"]
    assert len(json.dumps(line)) < 8 * 1024


@pytest.mark.parametrize("surface", ["mcp", "rest"])
def test_reflect_demand_counts_strings_only_and_caps_each_tag_length(tmp_path, surface):
    tags = [{"note": "x"}] * 10 + ["t" * 100_000] + [f"t{i}" for i in range(TAG_CAP + 5)]

    line, _ = reflect_with_tags(tmp_path, surface, tags)

    ev = line["reflect"]
    assert len(ev["tags"]) == TAG_CAP and len(ev["tags"][0]) == 256
    assert ev["tags_count"] == TAG_CAP + 6 and ev["tags_omitted"] == 6
    assert len(json.dumps(line)) < 8 * 1024
