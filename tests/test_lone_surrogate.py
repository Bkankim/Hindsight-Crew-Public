"""Issue #135: lone surrogate(U+D800-U+DFFF) 입력이 500도, 감사 줄 누락도 내지 않는다.

`"\\ud800"` 같은 lone surrogate 는 JSON 이스케이프(`\\ud800`)로도, 원시 CESU 바이트로도 들어와 str 로 살아남는다.
strict utf-8 쓰기·응답 직렬화·값 계산이 이것을 못 받아 UnicodeEncodeError -> 500 이 났고 해당 줄이 빠졌다.
이 파일은 세 경계를 한 번에 핀한다:
  ① 파일 경계: 감사 줄·원장 줄·pending triggers 줄에서 U+FFFD 로 치환하고, 치환이 있는 줄에만
     최상위 `surrogates_replaced` 를 단다. 파일에는 `\\udXXX` 이스케이프를 쓰지 않는다.
  ② 응답 경계: 게이트웨이 JSON 응답은 surrogate 만 `\\udXXX` 로 내보낸다(500 아님).
  ③ 계산 경계: query_token_bound 가 surrogate 를 3바이트로 센다.
  그리고 upstream 으로 가는 본문은 바뀌지 않고, #117 줄 상한과 소비자(model_audit)가 안전하다.
줄 바이트는 파일의 실제 바이트(read_bytes)로 잰다 - surrogate 가 든 entry 는 테스트 쪽 encode 가 못 받는다.
"""
import datetime as dt
import importlib
import json
import os
import re
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, UPSTREAM_KEY, bank_config_reply
from gateway import audit as audit_module
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit
from gateway.bank_policies import BankPolicies
from gateway.context_triggers import query_token_bound
from gateway.recurrence import PendingTriggers, RecallLedger
from test_retain_tag_audit import REPO_POLICY_FILE

BANK = "personal-alice"
AUTH = {"content-type": "application/json", "authorization": "Bearer tok-alice"}
CONTENT = "[decision] 재현 본문"
QUERY = "lone surrogate 재현 쿼리"
LINE_MAX_BYTES = 40 * 1024
SURROGATE = re.compile("[\ud800-\udfff]")
SURROGATE_ESCAPE = re.compile(rb"\\u[dD][89a-fA-F][0-9a-fA-F]{2}")      # Hangul escapes (\\ud604) stop at d7a3
INPUTS = ["\ud800", "a\udfff"]
INPUT_IDS = ["S1", "S2"]
MARKER = "surrogates_replaced"


class Upstream:
    """모의 upstream: 받은 원문 바이트를 남기고 경로·도구별로 그럴듯한 성공 응답을 준다."""

    def __init__(self):
        self.raw = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if (config := bank_config_reply(request)) is not None:   # #473 b upstream config read
            return config
        self.raw.append(request.content or b"")
        path = request.url.path
        recall = {"results": [{"id": "mem-1", "text": "x"}]}
        reflect = {"text": "answer", "based_on": {"mental_models": [{"id": "mm-1"}]},
                   "usage": {"input_tokens": 1}}
        if path == "/mcp":
            body = json.loads(request.content)
            el = body[0] if isinstance(body, list) else body
            tool = (el.get("params") or {}).get("name")
            if tool == "recall":
                enc = json.dumps(recall)
                result = {"content": [{"type": "text", "text": enc}],
                          "structuredContent": {"result": enc}, "isError": False}
            elif tool == "reflect":
                result = {"content": [{"type": "text", "text": json.dumps(reflect)}],
                          "structuredContent": {"result": reflect}, "isError": False}
            else:
                result = {"content": [{"type": "text", "text": json.dumps({"success": True})}],
                          "isError": False}
            data = json.dumps({"jsonrpc": "2.0", "id": el.get("id"), "result": result}).encode()
            return httpx.Response(200, content=b"event: message\r\ndata: " + data + b"\r\n\r\n",
                                  headers={"content-type": "text/event-stream",
                                           "mcp-session-id": "sess-upstream-1"})
        if path.endswith("/memories/recall"):
            return httpx.Response(200, json=recall)
        if path.endswith("/reflect"):
            return httpx.Response(200, json=reflect)
        return httpx.Response(200, json={"success": True, "bank_id": BANK, "items_count": 1})


class Env:
    def __init__(self, tmp_path, strict):
        self.up = Upstream()
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.up.handler),
                                 base_url="http://upstream", timeout=5.0)
        self.audit_path = tmp_path / "audit.log"
        self.ledger_path = tmp_path / "ledger.jsonl"
        self.ledger = RecallLedger(str(self.ledger_path))
        self.app = create_app(
            acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(self.audit_path)),
            upstream_key=UPSTREAM_KEY, http_client=http,
            bank_policies=BankPolicies.from_file(REPO_POLICY_FILE) if strict else None,
            recall_ledger=self.ledger)
        # 500 을 예외로 올리지 않고 상태 코드로 본다
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def entries(self, path):
        """파일의 실제 바이트를 strict utf-8 로 읽는다(깨진 줄·surrogate 바이트가 있으면 여기서 실패)."""
        if not path.exists():
            return []
        text = path.read_bytes().decode("utf-8")
        return [json.loads(ln) for ln in text.splitlines()]

    def audit_entries(self):
        return self.entries(self.audit_path)

    def ledger_entries(self):
        return self.entries(self.ledger_path)


def mcp(c, name, args, method="tools/call"):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"name": name, "arguments": args}}
    return c.post(f"/mcp/{BANK}/", content=json.dumps(body),
                  headers={**AUTH, "mcp-session-id": "sess-upstream-1"})


def rest_retain(c, tags):
    return c.post(f"/v1/default/banks/{BANK}/memories", headers=AUTH,
                  content=json.dumps({"items": [{"content": CONTENT, "tags": tags}]}))


def rest_post(c, suffix, args):
    return c.post(f"/v1/default/banks/{BANK}/{suffix}", headers=AUTH, content=json.dumps(args))


def retain(surface, tags):
    return (lambda c: mcp(c, "retain", {"content": CONTENT, "tags": tags})) if surface == "mcp" \
        else (lambda c: rest_retain(c, tags))


def reflect(surface, args):
    return (lambda c: mcp(c, "reflect", args)) if surface == "mcp" \
        else (lambda c: rest_post(c, "reflect", args))


def recall(surface, args):
    return (lambda c: mcp(c, "recall", args)) if surface == "mcp" \
        else (lambda c: rest_post(c, "memories/recall", args))


def _cells():
    """(id, strict 뱅크 여부, 요청 함수, 기대 줄이 있는 파일, 기대 줄의 decision). 입력 S 는 세 번째 인자로 들어간다."""
    cells = []
    for surface in ("mcp", "rest"):
        cells.append((f"retain-tags|{surface}", False,
                      lambda s, surface=surface: retain(surface, [s]), "audit", "retain-tags"))
        cells.append((f"enforce-normalize|{surface}", True,
                      lambda s, surface=surface: retain(surface, ["kind:decision", "topic:x" + s]),
                      "audit", "enforce-normalize"))
        for field, make in (("tags", lambda s: {"tags": [s]}), ("tags_match", lambda s: {"tags_match": s}),
                            ("budget", lambda s: {"budget": s})):
            cells.append((f"reflect-demand|{surface}|{field}", False,
                          lambda s, surface=surface, make=make: reflect(surface, {"query": QUERY, **make(s)}),
                          "audit", "reflect-demand"))
        for field, make in (("tags", lambda s: {"query": QUERY, "tags": [s]}),
                            ("tags_match", lambda s: {"query": QUERY, "tags_match": s}),
                            ("budget", lambda s: {"query": QUERY, "budget": s}),
                            ("query", lambda s: {"query": QUERY + s})):
            cells.append((f"recall-ledger|{surface}|{field}", False,
                          lambda s, surface=surface, make=make: recall(surface, make(s)),
                          "ledger", None))
    # allow/deny 는 MCP 에서만 도달한다: REST 경로의 퍼센트 인코딩은 U+FFFD 로 디코딩되어 surrogate 가 안 온다
    cells.append(("deny|mcp|method", False,
                  lambda s: (lambda c: mcp(c, None, {}, method=s)), "audit", "deny"))
    cells.append(("deny|mcp|tool-name", False,
                  lambda s: (lambda c: mcp(c, "x" + s, {})), "audit", "deny"))
    return cells


CELLS = _cells()


def surrogate_free(value) -> bool:
    return not SURROGATE.search(json.dumps(value, ensure_ascii=False))


@pytest.mark.parametrize("s", INPUTS, ids=INPUT_IDS)
@pytest.mark.parametrize("cell", CELLS, ids=[c[0] for c in CELLS])
def test_lone_surrogate_never_500s_and_the_line_is_kept(cell, s, tmp_path):
    _, strict, build, where, decision = cell
    env = Env(tmp_path, strict)

    resp = build(s)(env.client)

    assert resp.status_code != 500, resp.text
    lines = env.ledger_entries() if where == "ledger" else [
        e for e in env.audit_entries() if e["decision"] == decision]
    assert lines, f"기대한 줄이 없다: {where}/{decision}"
    for entry in lines:
        assert surrogate_free(entry)
        assert "�" in json.dumps(entry, ensure_ascii=False)
        assert isinstance(entry[MARKER], int) and not isinstance(entry[MARKER], bool) and entry[MARKER] >= 1
    if where == "ledger":
        # 치환된 행은 더 이상 실제 recall 을 재생하지 않으므로 파일에서 재사용 불가다(recurrence 의 lossy 관례).
        assert all(entry["reusable"] is False for entry in lines)
        # in-memory 이벤트는 원본과 원래 reusable 값을 유지한다(재발 탐지 초안이 재생한다)
        events = env.ledger.recent(identity="alice", bank=BANK, since=0.0)
        assert events and all(ev.reusable is True for ev in events)
        assert all(SURROGATE.search(json.dumps(ev.as_json(), ensure_ascii=False)) for ev in events)
    # 파일의 모든 줄이 strict utf-8 이고 surrogate 가 없다(부분 쓰기·이스케이프 없음)
    for path in (env.audit_path, env.ledger_path):
        if path.exists():
            text = path.read_bytes().decode("utf-8")
            assert not SURROGATE.search(text)
            assert not SURROGATE_ESCAPE.search(text.encode("utf-8")), "파일에 \\udXXX 이스케이프를 쓰면 안 된다"
    # 마커는 치환이 일어난 줄에만 있다: 치환 없는 줄(allow 등)에는 없다
    for entry in env.audit_entries() + env.ledger_entries():
        if MARKER in entry:
            assert "�" in json.dumps(entry, ensure_ascii=False)


@pytest.mark.parametrize("s", INPUTS, ids=INPUT_IDS)
@pytest.mark.parametrize("cell", [c for c in CELLS if not c[0].startswith("deny")],
                         ids=[c[0] for c in CELLS if not c[0].startswith("deny")])
def test_upstream_body_keeps_the_escape_and_is_canonical(cell, s, tmp_path):
    """upstream 은 수정 전과 같은 본문을 받는다: 모두 ASCII, surrogate 는 `\\udXXX` 이스케이프, 원시 CESU 바이트 없음."""
    _, strict, build, _, _ = cell
    env = Env(tmp_path, strict)

    build(s)(env.client)

    assert env.up.raw, "upstream 이 호출되지 않았다"
    for raw in env.up.raw:
        assert raw.isascii()
        assert b"\xed\xa0" not in raw and b"\xed\xbf" not in raw
        assert json.dumps(json.loads(raw)).encode("ascii") == raw     # 게이트웨이의 canonical 재직렬화 그대로
    escape = json.dumps(s)[1:-1].encode("ascii")                      # `\ud800` / `a\udfff`
    name = cell[0]
    if not name.startswith("enforce-normalize"):                       # 정규화는 surrogate 를 떼고 보낸다
        assert any(escape in raw for raw in env.up.raw)
    else:
        assert not any(SURROGATE_ESCAPE.search(raw) for raw in env.up.raw)


# ---------------------------------------------------------------- 응답 경계

def _normalized_sent(rest_json):
    return [rw["sent"] for entry in rest_json["gateway_normalized_tags"] for rw in entry["rewrites"]]


@pytest.mark.parametrize("s", INPUTS, ids=INPUT_IDS)
def test_rest_normalized_retain_discloses_the_original_sent_string(s, tmp_path):
    env = Env(tmp_path, strict=True)

    resp = retain("rest", ["kind:decision", "topic:x" + s])(env.client)

    assert resp.status_code == 200
    assert _normalized_sent(resp.json()) == ["topic:x" + s]       # 응답의 \udXXX 이스케이프가 원문으로 복원된다
    assert SURROGATE_ESCAPE.search(resp.content) and b"\xed" not in resp.content
    assert env.app.state.rewrite_failures == 0


@pytest.mark.parametrize("s", INPUTS, ids=INPUT_IDS)
def test_mcp_normalized_retain_keeps_the_annotation(s, tmp_path):
    env = Env(tmp_path, strict=True)

    resp = retain("mcp", ["kind:decision", "topic:x" + s])(env.client)

    assert resp.status_code == 200
    assert env.app.state.rewrite_failures == 0
    assert not [e for e in env.audit_entries() if e["decision"] == "rewrite-failure"]
    payload = json.loads(next(ln[5:] for ln in resp.text.splitlines() if ln.startswith("data:")))
    texts = [c["text"] for c in payload["result"]["content"]]
    assert any("topic:x" in t and "->" in t for t in texts), texts      # 정규화 주석이 살아 있다
    assert any(("topic:x" + s) in t for t in texts)


def test_deny_response_carries_the_escape_not_a_500(tmp_path):
    env = Env(tmp_path, strict=False)

    resp = mcp(env.client, "x\ud800", {})

    assert resp.status_code == 403
    assert json.loads(resp.content)["reason"].endswith("x\ud800")
    assert b"\\ud800" in resp.content


# ---------------------------------------------------------------- #117 줄 상한

@pytest.mark.parametrize("surface", ["mcp", "rest"])
def test_surrogate_64x256_retain_line_stays_under_the_bound(surface, tmp_path):
    env = Env(tmp_path, strict=False)
    tags = [f"t{i:02d}" + "\ud800" * 253 for i in range(64)]          # 정확히 256자

    resp = retain(surface, tags)(env.client)

    assert resp.status_code == 200
    raw_lines = [ln for ln in env.audit_path.read_bytes().splitlines() if b'"retain-tags"' in ln]
    assert len(raw_lines) == 1
    assert len(raw_lines[0]) <= LINE_MAX_BYTES                          # 파일의 실제 바이트
    entry = json.loads(raw_lines[0])
    assert entry["retain_tags"]["sent_omitted"] == 43                   # 수정 전 측정과 같다
    assert entry[MARKER] >= 1 and surrogate_free(entry)


# ---------------------------------------------------------------- 소비자

def test_model_audit_report_survives_strict_utf8_output(tmp_path, monkeypatch):
    env = Env(tmp_path, strict=False)
    resp = reflect("mcp", {"query": QUERY, "tags": ["topic:x\ud800"]})(env.client)
    assert resp.status_code == 200
    scripts = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
    monkeypatch.syspath_prepend(scripts)
    model_audit = importlib.import_module("model_audit")
    monkeypatch.setattr(model_audit, "GATEWAY_AUDIT_LOG", str(env.audit_path))
    monkeypatch.setattr(model_audit, "BANK", BANK)

    report = model_audit.demand_signal(set(), dt.datetime(2000, 1, 1))

    assert [t["tag"] for t in report["tags"]] == ["topic:x�"]
    json.dumps(report, ensure_ascii=False).encode("utf-8")              # print() 의 strict 인코딩이 예외 없이 끝난다


# ---------------------------------------------------------------- 공용 헬퍼·다른 파일 경계·계산 경계

def test_helper_replaces_only_surrogates_and_counts_them():
    replace = audit_module.replace_surrogates
    assert replace("a\ud800b\udfffc\ud83d") == ("a�b�c�", 3)
    plain = '{"k":"한글 ☃ \U0001f600 \\ud800 \\u0001"}'
    assert replace(plain) == (plain, 0)                                 # 유효 문자·이스케이프 텍스트는 그대로


def test_valid_input_lines_are_byte_identical_and_unmarked(tmp_path):
    audit = Audit(path=str(tmp_path / "a.log"))
    entry = audit.record(token="t", identity="alice", method="tools/call", tool="retain",
                         attempted_bank=BANK, resolved_bank=BANK, decision="allow",
                         reason="한글 ☃ \U0001f600 \x01 \\ud800", extra={"x": {"y": ["z"]}})
    raw = (tmp_path / "a.log").read_bytes()
    assert raw == (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    assert MARKER not in json.loads(raw)


def test_audit_record_marks_the_line_and_keeps_the_in_memory_entry(tmp_path):
    audit = Audit(path=str(tmp_path / "a.log"))
    entry = audit.record(token=None, identity="alice", method="m\ud800", tool="x\udfff",
                         attempted_bank=BANK, resolved_bank=BANK, decision="deny", reason="r")
    (written,) = [json.loads(ln) for ln in (tmp_path / "a.log").read_bytes().decode("utf-8").splitlines()]
    assert written["method"] == "m�" and written["tool"] == "x�" and written[MARKER] == 2
    assert entry["method"] == "m\ud800"                                 # 호출자가 받는 값은 원본


def test_ledger_row_is_replaced_and_the_in_memory_event_keeps_the_original(tmp_path):
    ledger = RecallLedger(str(tmp_path / "l.jsonl"))
    ev = ledger.record(identity="alice", bank=BANK, transport="mcp", query=QUERY + "\ud800",
                       tags=["t\udfff"], tags_match=None, budget=None, max_tokens=None,
                       result_ids=["m1"], session_id="s", client="c")
    (written,) = [json.loads(ln) for ln in (tmp_path / "l.jsonl").read_bytes().decode("utf-8").splitlines()]
    assert written["query"].endswith("�") and written["tags"] == ["t�"] and written[MARKER] == 2
    assert ev.query.endswith("\ud800") and ev.tags == ("t\udfff",)     # 재발 탐지 초안이 재생할 원본


def test_pending_triggers_row_is_written_with_replacement(tmp_path):
    pending = PendingTriggers(str(tmp_path / "p.jsonl"))

    assert pending.append({"id": "d1", "query": "q\ud800", "tags": ["t\udfff"]}) is True

    (row,) = [json.loads(ln) for ln in (tmp_path / "p.jsonl").read_bytes().decode("utf-8").splitlines()]
    assert row == {"id": "d1", "query": "q�", "tags": ["t�"]}


def test_query_token_bound_counts_a_surrogate_as_three_bytes():
    assert query_token_bound("a\ud800") == query_token_bound("a�") == 4
    assert query_token_bound("\udfff" * 5) == 15


def test_ledger_row_without_surrogate_is_byte_identical_and_keeps_reusable(tmp_path):
    ledger = RecallLedger(str(tmp_path / "l.jsonl"))
    ev = ledger.record(identity="alice", bank=BANK, transport="mcp", query=QUERY, tags=["한글 ☃"],
                       result_ids=["m1"], session_id="s", client="c")
    raw = (tmp_path / "l.jsonl").read_bytes()
    assert raw == (json.dumps(ev.as_json(), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    row = json.loads(raw)
    assert row["reusable"] is True and MARKER not in row
