"""reflect include_based_on 기본값의 게이트웨이 경계 (issue #55).

채우기·지우기 규칙은 tests/test_reflect_based_on_default.py 가 고정한다. 여기는 app.py 가
  - include_based_on 이 없거나 null 일 때만 upstream 인자에 true 를 채우는지(명시값은 그대로, 비 dict 인자는 RULE:arguments-not-object 거부(#61)),
  - 채운 호출은 호출자 응답에서 based_on 을 지우고(structuredContent 와 text 블록 둘 다),
    호출자가 명시한 호출의 based_on 은 남기는지,
  - reflect-demand 계측은 upstream 원본의 based_on 으로 hit/miss 를 판정하는지,
  - 채운 사실을 enforce-autofill 1줄로 드러내는지,
  - 킬스위치 0 이면 지금 동작 그대로인지
를 응답 경로 3종(SSE 스트림 / 반 SSE JSON / 버퍼링 대상과 한 배치)마다 본다.

upstream 은 include_based_on 을 받은 그대로 따르는 가짜다(true 일 때만 based_on 을 싣는다).
채우기 여부를 app 을 거친 upstream 인자로 확인하므로 모듈 상수 대신 리터럴로 고정한다.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, UPSTREAM_KEY
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit

KILL_ENV = "HC_REFLECT_BASED_ON_DEFAULT"
TAGS = ["project:x"]
TEXT = "답변 본문"
BASED_ON = {"world": [{"id": "w1", "text": "근거 사실"}],
            "mental-models": [{"id": "demo-rules-x", "text": "모델 본문"}]}
AUTOFILL_REASON = "based-on-default:true"


def _payload(include):
    payload = {"text": TEXT, "usage": {"input_tokens": 100, "output_tokens": 7}}
    if include:
        payload["based_on"] = BASED_ON
    return payload


def _result(payload):
    blob = json.dumps(payload, indent=2)          # upstream 기본값 = ensure_ascii=True
    return {"content": [{"type": "text", "text": blob}], "structuredContent": {"result": blob},
            "isError": False}


def _wants_based_on(arguments):
    return isinstance(arguments, dict) and arguments.get("include_based_on") is True


class FakeUpstream:
    """요청 element 마다 include_based_on 을 따르는 reflect 응답을 만든다. 응답 경로는 path 로 고른다."""

    def __init__(self, path):
        self.path = path              # "sse" | "json"
        self.calls = []

    def handler(self, request):
        sent = json.loads(request.content.decode("utf-8"))
        self.calls.append(sent)
        batch = sent if isinstance(sent, list) else [sent]
        elements = []
        for el in batch:
            params = el.get("params") or {}
            if params.get("name") == "reflect":
                result = _result(_payload(_wants_based_on(params.get("arguments"))))
            else:
                result = _result({"results": []})
            elements.append({"jsonrpc": "2.0", "id": el["id"], "result": result})
        headers = {"mcp-session-id": "sess-upstream-1"}
        if self.path == "json":
            headers["content-type"] = "application/json"
            body = json.dumps(elements if isinstance(sent, list) else elements[0]).encode("utf-8")
        else:
            headers["content-type"] = "text/event-stream"
            body = b"".join(b"event: message\r\ndata: " + json.dumps(e).encode("utf-8") + b"\r\n\r\n"
                            for e in elements)
        return httpx.Response(200, content=body, headers=headers)

    @property
    def last(self):
        return self.calls[-1]


def _make(upstream, tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler),
                             base_url="http://upstream", timeout=5.0)
    log = tmp_path / "audit.log"
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(log)),
                     upstream_key=UPSTREAM_KEY, http_client=http)
    return TestClient(app), log


def _reflect(el_id=1, **args):
    return {"jsonrpc": "2.0", "id": el_id, "method": "tools/call",
            "params": {"name": "reflect", "arguments": {"query": "q", "tags": TAGS, **args}}}


def _raw_call(arguments, el_id=1):
    return {"jsonrpc": "2.0", "id": el_id, "method": "tools/call",
            "params": {"name": "reflect", "arguments": arguments}}


def _recall(el_id=2):
    return {"jsonrpc": "2.0", "id": el_id, "method": "tools/call",
            "params": {"name": "recall", "arguments": {"query": "q", "tags": TAGS}}}


def _post(client, body):
    return client.post("/mcp/personal-alice/", content=json.dumps(body),
                       headers={"content-type": "application/json",
                                "authorization": "Bearer tok-alice"})


def _elements(resp):
    """응답 본문(SSE 또는 JSON, 단일 또는 배치)을 {id: element} 로."""
    text = resp.content.decode("utf-8")
    if "event-stream" in resp.headers["content-type"]:
        parsed = [json.loads(ln[len("data:"):]) for ln in text.splitlines() if ln.startswith("data:")]
    else:
        body = json.loads(text)
        parsed = body if isinstance(body, list) else [body]
    return {el["id"]: el for el in parsed}


def _view(element):
    """structuredContent.result 와 text 블록 JSON 을 각각 파싱해 (structured, text_json) 로."""
    result = element["result"]
    return json.loads(result["structuredContent"]["result"]), json.loads(result["content"][0]["text"])


def _lines(log):
    return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _demand(log):
    return [e["reflect"] for e in _lines(log) if e["decision"] == "reflect-demand"]


def _autofills(log):
    return [e for e in _lines(log)
            if e["decision"] == "enforce-autofill" and e["reason"].startswith("based-on-default")]


# 인자 6종 x 기대 4종. sent: upstream 으로 간 arguments 전체, in_response: 호출자 응답의 based_on 유무,
# demand: reflect-demand 줄의 based_on, autofill: enforce-autofill 줄 유무.
# 킬스위치 행은 인자가 "없음"과 같고 env 만 다르다.
def _case(arguments, sent, *, in_response, demand, autofill, kill=False):
    return dict(arguments=arguments, sent=sent, in_response=in_response, demand=demand,
                autofill=autofill, kill=kill)


MATRIX = {
    "missing": _case({"query": "q", "tags": TAGS},
                     {"query": "q", "tags": TAGS, "include_based_on": True},
                     in_response=False, demand="hit", autofill=True),
    "null": _case({"query": "q", "tags": TAGS, "include_based_on": None},
                  {"query": "q", "tags": TAGS, "include_based_on": True},
                  in_response=False, demand="hit", autofill=True),
    "true": _case({"query": "q", "tags": TAGS, "include_based_on": True},
                  {"query": "q", "tags": TAGS, "include_based_on": True},
                  in_response=True, demand="hit", autofill=False),
    "false": _case({"query": "q", "tags": TAGS, "include_based_on": False},
                   {"query": "q", "tags": TAGS, "include_based_on": False},
                   in_response=False, demand="unknown", autofill=False),
    "arguments-none": _case(None, {"include_based_on": True},
                            in_response=False, demand="hit", autofill=True),
    # dict 가 아닌 인자("arguments-not-dict")는 #61 이후 규칙 거부라 upstream 에 닿지 않는다.
    # 이 행렬은 upstream 응답을 전제하므로 test_non_dict_arguments_are_rejected_not_filled 가 따로 다룬다.
    "kill-switch-0": _case({"query": "q", "tags": TAGS}, {"query": "q", "tags": TAGS},
                           in_response=False, demand="unknown", autofill=False, kill=True),
}
PATHS = ["sse", "json", "batch"]


def _send(client, path, arguments):
    """path 별 요청. batch 는 버퍼링 대상(recall)과 한 배치로 보낸다."""
    if path == "batch":
        return _post(client, [_raw_call(arguments, 1), _recall(2)])
    return _post(client, _raw_call(arguments, 1))


def _upstream_path(path):
    return "json" if path == "json" else "sse"


def _sent_arguments(upstream):
    last = upstream.last
    return (last[0] if isinstance(last, list) else last)["params"]["arguments"]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("name", sorted(MATRIX))
def test_matrix(tmp_path, monkeypatch, name, path):
    case = MATRIX[name]
    if case["kill"]:
        monkeypatch.setenv(KILL_ENV, "0")
    upstream = FakeUpstream(_upstream_path(path))
    client, log = _make(upstream, tmp_path)
    resp = _send(client, path, case["arguments"])
    assert resp.status_code == 200

    assert _sent_arguments(upstream) == case["sent"]

    reflect = _elements(resp)[1]
    structured, text_json = _view(reflect)
    assert ("based_on" in structured) is case["in_response"]
    assert ("based_on" in text_json) is case["in_response"]
    # 두 표현이 어긋나지 않고, based_on 외 나머지는 upstream 그대로다.
    assert structured == text_json
    assert {k: v for k, v in structured.items() if k != "based_on"} == _payload(False)

    demand = _demand(log)
    assert [d["based_on"] for d in demand] == [case["demand"]]
    assert demand[0]["mental_models"] == (["demo-rules-x"] if case["demand"] == "hit" else [])
    assert len(_autofills(log)) == (1 if case["autofill"] else 0)
    if case["autofill"]:
        line = _autofills(log)[0]
        assert line["tool"] == "reflect" and line["reason"] == AUTOFILL_REASON
    assert client.get("/healthz").json()["rewrite_failures"] == 0


@pytest.mark.parametrize("path", PATHS)
def test_non_dict_arguments_are_rejected_not_filled(tmp_path, path):
    """dict 가 아닌 인자는 채우지 않고 #61 규칙으로 거부한다(이전엔 빈 배열이 {} 로 바뀌어 전달됐다)."""
    upstream = FakeUpstream(_upstream_path(path))
    client, log = _make(upstream, tmp_path)
    resp = _send(client, path, [])
    assert resp.status_code == 200

    reflect = _elements(resp)[1]["result"]
    assert reflect["isError"] is True
    assert reflect["content"][0]["text"].startswith("RULE:arguments-not-object")
    assert upstream.calls == []                      # upstream 미호출
    assert _demand(log) == [] and _autofills(log) == []
    assert client.get("/healthz").json()["rewrite_failures"] == 0


@pytest.mark.parametrize("path", PATHS)
def test_filled_response_text_is_identical_to_unfilled(tmp_path, monkeypatch, path):
    """채운 호출의 text 블록·structuredContent 문자열이 채우지 않은(킬스위치 0) 호출과 글자 단위로 같다.

    둘 다 #23/#26 이스케이프 해제가 켜진 채라 같은 한글 표기로 나온다."""

    def run(root, kill):
        if kill:
            monkeypatch.setenv(KILL_ENV, "0")
        else:
            monkeypatch.delenv(KILL_ENV, raising=False)
        client, _ = _make(FakeUpstream(_upstream_path(path)), root)
        return _elements(_send(client, path, {"query": "q", "tags": TAGS}))[1]["result"]

    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    filled = run(tmp_path / "on", kill=False)
    unfilled = run(tmp_path / "off", kill=True)
    assert filled == unfilled
    assert "based_on" not in filled["content"][0]["text"]


@pytest.mark.parametrize("path", ["sse", "json"])
def test_kill_switch_is_byte_identical_to_upstream(tmp_path, monkeypatch, path):
    monkeypatch.setenv(KILL_ENV, "0")
    monkeypatch.setenv("HC_UNESCAPE", "0")
    upstream = FakeUpstream(path)
    client, log = _make(upstream, tmp_path)
    resp = _post(client, _reflect())
    expected = upstream.handler(httpx.Request("POST", "http://x", content=json.dumps(_reflect()).encode()))
    assert resp.content == expected.content
    assert "include_based_on" not in upstream.calls[0]["params"]["arguments"]
    assert _autofills(log) == []


@pytest.mark.parametrize("path", ["sse", "batch"])
def test_filled_korean_text_is_unescaped_and_stripped_together(tmp_path, path):
    """#23/#26 이스케이프 해제와 지우기가 함께 돈다: 한글은 풀리고 based_on 은 없다."""
    upstream = FakeUpstream("sse")
    client, _ = _make(upstream, tmp_path)
    body = _reflect() if path == "sse" else [_reflect(1), _recall(2)]
    reflect = _elements(_post(client, body))[1]["result"]
    text = reflect["content"][0]["text"]
    assert "\\u" not in text and TEXT in text and "based_on" not in text
    assert reflect["structuredContent"]["result"] == text


@pytest.mark.parametrize("path", ["sse", "json", "batch"])
def test_batch_of_filled_and_explicit_reflects_keeps_only_the_explicit_one(tmp_path, path):
    """H3: 지우기는 채운 element id 에만 적용한다. 호출자가 true 를 명시한 호출의 based_on 은 남는다."""
    upstream = FakeUpstream("json" if path == "json" else "sse")
    client, log = _make(upstream, tmp_path)
    body = [_reflect(1), _reflect(2, include_based_on=True)]
    if path == "batch":
        body.append(_recall(3))
    by_id = _elements(_post(client, body))
    assert "based_on" not in _view(by_id[1])[0] and "based_on" not in _view(by_id[1])[1]
    assert "based_on" in _view(by_id[2])[0] and "based_on" in _view(by_id[2])[1]
    assert [d["based_on"] for d in _demand(log)] == ["hit", "hit"]
    assert len(_autofills(log)) == 1


def test_error_result_is_not_touched(tmp_path):
    """isError 응답은 지울 것이 없어 그대로 나가고, 계측은 ok false / unknown 이다."""
    body = (b"event: message\r\ndata: " + json.dumps(
        {"jsonrpc": "2.0", "id": 1,
         "result": {"content": [{"type": "text", "text": "upstream failed"}], "isError": True}}
    ).encode() + b"\r\n\r\n")

    class ErrorUpstream(FakeUpstream):
        def handler(self, request):
            self.calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client, log = _make(ErrorUpstream("sse"), tmp_path)
    result = _elements(_post(client, _reflect()))[1]["result"]
    assert result["isError"] is True and result["content"][0]["text"] == "upstream failed"
    demand = _demand(log)
    assert demand[0]["ok"] is False and demand[0]["based_on"] == "unknown"


def test_non_reflect_tool_is_not_filled(tmp_path):
    upstream = FakeUpstream("sse")
    client, log = _make(upstream, tmp_path)
    _post(client, _recall(1))
    assert "include_based_on" not in upstream.last["params"]["arguments"]
    assert _autofills(log) == []


def test_rest_reflect_is_untouched(tmp_path):
    """REST 표면(/v1/.../reflect)은 채우지도 지우지도 않는다 - 운영 스크립트가 REST 원형을 쓴다."""
    seen = []
    body = json.dumps({"text": TEXT}).encode("utf-8")

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://upstream")
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=http)
    resp = TestClient(app).post("/v1/default/banks/personal-alice/reflect",
                                content=json.dumps({"query": "q", "tags": TAGS}),
                                headers={"content-type": "application/json",
                                         "authorization": "Bearer tok-alice"})
    assert resp.status_code == 200 and resp.content == body
    assert "include_based_on" not in json.loads(seen[-1].content)


@pytest.mark.parametrize("path", PATHS)
def test_strip_failure_fails_open_with_the_original_and_a_visible_reason(tmp_path, monkeypatch, path):
    """지우기가 예외를 던지면 호출은 실패하지 않는다: 원본(근거 포함)이 나가고, 카운터와 audit 1줄이 남고,
    계측은 그대로 hit 다 - reflect 는 약 100초라 계측 결함이 호출을 죽이면 안 된다."""
    from gateway import reflect_based_on_default

    def boom(element, ids):
        raise RuntimeError("strip exploded")

    monkeypatch.setattr(reflect_based_on_default, "strip_mcp_element", boom)
    client, log = _make(FakeUpstream(_upstream_path(path)), tmp_path)
    resp = _send(client, path, {"query": "q", "tags": TAGS})
    assert resp.status_code == 200
    structured, text_json = _view(_elements(resp)[1])
    assert structured["based_on"] == BASED_ON and text_json["based_on"] == BASED_ON
    failures = [e for e in _lines(log) if e["decision"] == "rewrite-failure"]
    # 배치는 합성 변환 실패 + 단독 strip 재시도 실패로 2줄, 스트림은 이벤트 1건 실패로 1줄이다.
    expected = 2 if path == "batch" else 1
    assert len(failures) == expected and all("RuntimeError" in e["reason"] for e in failures)
    assert client.get("/healthz").json()["rewrite_failures"] == expected
    assert [d["based_on"] for d in _demand(log)] == ["hit"]


# --------------------------------------------------------------------------
# final-review 반영 (issue #55)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", PATHS)
def test_unescape_off_restores_old_reflect_passthrough_without_filling(tmp_path, monkeypatch, path):
    """#6: HC_UNESCAPE=0 은 #26 비상 레버다. 기본 호출을 채우지 않아 reflect 가 옛 바이트로 통과한다."""
    monkeypatch.setenv("HC_UNESCAPE", "0")
    upstream = FakeUpstream(_upstream_path(path))
    client, log = _make(upstream, tmp_path)
    resp = _send(client, path, {"query": "q", "tags": TAGS})
    assert _sent_arguments(upstream) == {"query": "q", "tags": TAGS}
    assert _autofills(log) == []
    assert [d["based_on"] for d in _demand(log)] == ["unknown"]
    assert _elements(resp)[1]["result"] == _result(_payload(False))
    if path != "batch":
        expected = upstream.handler(httpx.Request(
            "POST", "http://x", content=json.dumps(_reflect()).encode())).content
        assert resp.content == expected


@pytest.mark.parametrize("path", ["sse", "json"])
@pytest.mark.parametrize("el_id", [True, 1.5])
def test_reflect_without_addressable_id_is_not_filled(tmp_path, el_id, path):
    """#4: element_id 가 None 으로 정규화되는 id(bool·float)는 응답을 다시 찾을 수 없어 채우지 않는다."""
    upstream = FakeUpstream(_upstream_path(path))
    client, log = _make(upstream, tmp_path)
    body = {"jsonrpc": "2.0", "id": el_id, "method": "tools/call",
            "params": {"name": "reflect", "arguments": {"query": "q", "tags": TAGS}}}
    _post(client, body)
    assert upstream.last["params"]["arguments"] == {"query": "q", "tags": TAGS}
    assert _autofills(log) == []


def test_batch_fallback_strips_when_another_element_breaks_the_composed_rewrite(tmp_path, monkeypatch):
    """#1: 배치의 recall 변환이 예외를 던지면 합성 변환은 통째로 원본으로 돌아간다. 채운 reflect 의
    based_on 은 단독 strip 으로 여전히 빠지고, 실패는 1줄 기록된다."""
    from gateway import recall_prune

    real = recall_prune.prune_mcp_element

    def flaky(element, ids, bank):
        if isinstance(element, dict) and element.get("id") == 2:
            raise RuntimeError("prune exploded")
        return real(element, ids, bank)

    monkeypatch.setattr(recall_prune, "prune_mcp_element", flaky)
    client, log = _make(FakeUpstream("sse"), tmp_path)
    resp = _post(client, [_reflect(1), _recall(2)])
    assert resp.status_code == 200
    by_id = _elements(resp)
    structured, text_json = _view(by_id[1])
    assert "based_on" not in structured and "based_on" not in text_json
    assert by_id[2]["result"] == _result({"results": []})       # 고장난 element 는 원본 그대로
    failures = [e for e in _lines(log) if e["decision"] == "rewrite-failure"]
    assert len(failures) == 1 and "RuntimeError" in failures[0]["reason"]
    assert [d["based_on"] for d in _demand(log)] == ["hit"]


def test_unterminated_final_event_is_stripped_too(tmp_path):
    """#2: 종결 빈 줄 없이 끝난 마지막 이벤트도 채운 호출이면 based_on 이 빠진다(꼬리도 fail-open 변환)."""
    class TruncatedUpstream(FakeUpstream):
        def handler(self, request):
            resp = super().handler(request)
            assert resp.content.endswith(b"\r\n\r\n")
            return httpx.Response(200, content=resp.content[:-2], headers=resp.headers)

    client, log = _make(TruncatedUpstream("sse"), tmp_path)
    resp = _post(client, _reflect())
    structured, text_json = _view(_elements(resp)[1])
    assert "based_on" not in structured and "based_on" not in text_json
    assert [d["based_on"] for d in _demand(log)] == ["hit"]


@pytest.mark.parametrize("path", ["sse", "json"])
def test_unescape_exception_keeps_the_successful_strip(tmp_path, monkeypatch, path):
    """#3: strip 뒤 unescape 단계가 예외를 던지면 원본(근거 포함)이 아니라 strip 된 바이트가 나간다."""
    from gateway import unescape

    def boom(content_type, body, ids):
        raise RuntimeError("unescape exploded")

    monkeypatch.setattr(unescape, "unescape_mcp_body", boom)
    client, log = _make(FakeUpstream(_upstream_path(path)), tmp_path)
    resp = _post(client, _reflect())
    structured, text_json = _view(_elements(resp)[1])
    assert "based_on" not in structured and "based_on" not in text_json
    failures = [e for e in _lines(log) if e["decision"] == "rewrite-failure"]
    assert len(failures) == 1 and "RuntimeError" in failures[0]["reason"]
    assert client.get("/healthz").json()["rewrite_failures"] == 1
