"""S3 열린 키 표 (issue #377, 아키텍처 v2 런 5 R5-B4, 플래그 HC_ITEM_OPEN_TABLE 기본 꺼짐). 네트워크 0, 임시 sqlite 픽스처.

고정하는 계약(행렬: 경우 x 플래그/표면/손잡이):
  (a) 꺼짐(미설정·"0"·빈 값·off·no·false·알 수 없는 값) x 손잡이 env 유무: MCP recall 응답 바이트가 이 변경 이전과
      같고(sha 고정값), 감사 줄 0, 저장소 열기 0. 코드 기본값·compose 배선 기본값이 꺼짐, 기본 경로가 정책 마운트 안.
  (b) `role_states`: 마지막 비참조 언급, 같은 시각은 열림, 참조만이면 미정(표에 없음), valid 만, t 이하만.
  (c) 손잡이: table_fmt a/b, header, attach status_query, status_terms, recent_done_days, max_rows, scope, 잘못된 값.
  (d) fail-open 네 경우(저장소 없음·열기 실패·스키마 버전 불일치·시간 초과): 원래 바이트 + 감사 사유.
  (e) 읽기 전용 열기: 쓰기 불가, recall 뒤 저장소 바이트·디렉터리 불변.
  (f) structuredContent·content[].text 두 표면(응답 모양 3종), 뒤 패스(prune·label·unescape)가 키를 지우거나 바꾸지 않음.
감사 줄에 기억 본문이 없다(BODY-SECRET 표식).
"""
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway import item_open_table as s3
from gateway.bank_policies import BankPolicies
from test_item_status import (BANK, BASELINE_SHA256, PROJECT, audit_rows, decode_element, elements_of, make_app,
                              mrow, payload_of, run as status_run, set_env, StatusUpstream, TOKEN)

REPO = Path(__file__).resolve().parent.parent
FIELD = s3.FIELD
ENV_BANK = "HC_ITEM_OPEN_TABLE_BANK"
QTS = "2026-10-20T14:59:59Z"            # = 2026-10-20 23:59:59 KST
T_DAY = "2026-10-20"
STATUS_Q = "남은 일 알려줘"             # 기본 어휘(STATUS_TERMS_DEFAULT)에 걸리는 상태 질의
# #376 과 맞춘 계약 상수: sha256(json.dumps({"hc": "hindsight-crew"}, sort_keys, ensure_ascii=False, 압축))
ALIASES_HC = {"hc": "hindsight-crew"}
ALIASES_HC_SHA = "9da1d8439ef4c752e1478cda51e7326ecd2b592f3b4c81f407b50590c0e1f181"
EMPTY_ALIASES_SHA = hashlib.sha256(b"{}").hexdigest()

# (memory_id, mentioned_at, state, {key: role})
BASE_MEMS = [
    ("m1", "2026-10-01T01:00:00Z", "valid", {"hindsight-crew#10": "open", "hindsight-crew#11": "open"}),
    ("m2", "2026-10-05T01:00:00Z", "valid", {"hindsight-crew#10": "done"}),          # #10 끝남
    ("m3", "2026-10-06T01:00:00Z", "valid", {"hindsight-crew#12": "ref"}),           # #12 미정
    ("m4", "2026-10-07T01:00:00Z", "valid", {"other-repo#3": "open"}),
    ("m5", "2026-10-25T01:00:00Z", "valid", {"hindsight-crew#11": "done"}),          # t 뒤
    ("m6", "2026-10-08T01:00:00Z", "invalidated", {"hindsight-crew#11": "done"}),    # valid 아님
    ("m7", "2026-10-19T01:00:00Z", "valid", {"hindsight-crew#13": "done"}),          # 최근 끝남
]


def make_store(path, mems=BASE_MEMS, *, schema_version="1", aliases_sha=EMPTY_ALIASES_SHA, drop_table=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(
        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);"
        "CREATE TABLE memories(memory_id TEXT PRIMARY KEY, document_id TEXT, mentioned_at TEXT, state TEXT NOT NULL,"
        " text_sha256 TEXT NOT NULL, judged_at TEXT NOT NULL);"
        "CREATE TABLE mention_roles(memory_id TEXT NOT NULL, key TEXT NOT NULL,"
        " role TEXT NOT NULL CHECK(role IN ('done','open','ref')), PRIMARY KEY(memory_id, key));")
    meta = {"schema_version": schema_version, "model": "m", "prompt_sha256": "p" * 64,
            "aliases_sha256": aliases_sha, "updated_at": "2026-11-01T00:00:00Z"}
    conn.executemany("INSERT INTO meta VALUES (?, ?)", list(meta.items()))
    for mid, at, state, roles in mems:
        conn.execute("INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?)", (mid, f"d-{mid}", at, state, "s" * 64, at))
        conn.executemany("INSERT INTO mention_roles VALUES (?, ?, ?)", [(mid, k, r) for k, r in roles.items()])
    if drop_table:
        conn.execute(f"DROP TABLE {drop_table}")
    conn.commit()
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch, tmp_path):
    for name in (s3.ENV_FLAG, s3.ENV_KNOBS, "HC_ITEM_STATUS", "HC_ITEM_LEDGER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(s3.ENV_CONFIG, str(tmp_path / "no-such-item-open-table.json"))
    monkeypatch.setenv(s3.ENV_DB, str(tmp_path / "store" / "item-roles.sqlite"))
    monkeypatch.setenv(ENV_BANK, BANK)                 # 저장소 뱅크 = 픽스처 뱅크(뱅크 한정 테스트만 바꾼다)
    s3._reset_cache()
    yield
    s3._reset_cache()


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path / "store" / "item-roles.sqlite")


@pytest.fixture
def opens(monkeypatch):
    """저장소 열기 횟수."""
    calls = []
    real = s3.connect_ro

    def counting(path):
        calls.append(path)
        return real(path)
    monkeypatch.setattr(s3, "connect_ro", counting)
    return calls


def run(monkeypatch, tmp_path, *, flag="1", knobs=None, args=None, **kwargs):
    set_env(monkeypatch, s3.ENV_FLAG, flag)
    set_env(monkeypatch, s3.ENV_KNOBS, None if knobs is None else json.dumps(knobs, ensure_ascii=False))
    base = {"query": STATUS_Q, "query_timestamp": QTS, "tags": [PROJECT]}
    return status_run(monkeypatch, tmp_path / "run", flag=None, args={**base, **(args or {})}, **kwargs)


def sha(resp):
    return hashlib.sha256(resp.content).hexdigest()


def s3_audit(log):
    return audit_rows(log, s3.DECISION)


def table_of(resp):
    return payload_of(resp).get(FIELD)


# --------------------------------------------------------------------------
# (a) 꺼짐
# --------------------------------------------------------------------------

def test_code_default_is_off(monkeypatch):
    monkeypatch.delenv(s3.ENV_FLAG, raising=False)
    assert s3.enabled() is False


@pytest.mark.parametrize("knobs", [None, {"table_fmt": "b", "attach": "always"}])
@pytest.mark.parametrize("flag", [None, "0", "", "off", "no", "false", "garbage"])
def test_flag_off_bytes_unchanged(monkeypatch, tmp_path, store, opens, flag, knobs):
    set_env(monkeypatch, s3.ENV_FLAG, flag)
    set_env(monkeypatch, s3.ENV_KNOBS, None if knobs is None else json.dumps(knobs))
    resp, log, up = status_run(monkeypatch, tmp_path / "run", flag=None)
    assert sha(resp) == BASELINE_SHA256
    assert FIELD.encode() not in resp.content
    assert s3_audit(log) == []
    assert opens == []
    assert [(c["method"], c["path"]) for c in up.calls] == [("POST", "/mcp")]


def _compose_gateway_env():
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    block = text.split("\n  gateway:\n", 1)[1].split("\n    environment:\n", 1)[1].split("\n    ports:\n", 1)[0]
    return dict(re.findall(r"^      ([A-Z0-9_]+): (.*)$", block, re.M))


@pytest.mark.parametrize("name,wired", [
    ("HC_ITEM_OPEN_TABLE", "${HC_ITEM_OPEN_TABLE:-0}"),
    ("HC_ITEM_OPEN_TABLE_CONFIG", "${HC_ITEM_OPEN_TABLE_CONFIG:-/app/policies/item-open-table.json}"),
    ("HC_ITEM_OPEN_TABLE_KNOBS", "${HC_ITEM_OPEN_TABLE_KNOBS:-}"),
    ("HC_ITEM_ROLES_DB", "${HC_ITEM_ROLES_DB:-/app/policies/item-roles.sqlite}"),
    ("HC_ITEM_OPEN_TABLE_BANK", "${HC_ITEM_OPEN_TABLE_BANK:-}"),
])
def test_compose_wiring_defaults_off(name, wired):
    env = _compose_gateway_env()
    assert env.get(name) == wired
    assert env["HC_ITEM_LEDGER"] == "${HC_ITEM_LEDGER:-0}"        # S1 배선 그대로


def test_default_paths_are_inside_the_policies_mount_and_absent_in_repo():
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- ./policies:/app/policies:ro" in text
    assert s3.DEFAULT_CONFIG_PATH == "/app/policies/item-open-table.json"
    assert s3.DEFAULT_DB_PATH == "/app/policies/item-roles.sqlite"
    assert not (REPO / "policies" / "item-open-table.json").exists()
    assert not (REPO / "policies" / "item-roles.sqlite").exists()


def test_no_bench_import():
    assert "bench" not in (REPO / "gateway/item_open_table.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# (b) role_states
# --------------------------------------------------------------------------

def _at(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def M(key, role, at, mid="x"):
    return s3.Mention(key, role, _at(at), mid)


CUT = _at(QTS)


@pytest.mark.parametrize("name,mentions,expected", [
    ("last-nonref-done", [M("r#1", "open", "2026-10-01T00:00:00Z"), M("r#1", "done", "2026-10-02T00:00:00Z")],
     {"r#1": "끝남"}),
    ("last-nonref-open", [M("r#1", "done", "2026-10-01T00:00:00Z"), M("r#1", "open", "2026-10-02T00:00:00Z")],
     {"r#1": "열림"}),
    ("tie-open-wins", [M("r#1", "done", "2026-10-02T00:00:00Z"), M("r#1", "open", "2026-10-02T00:00:00Z")],
     {"r#1": "열림"}),
    ("tie-open-wins-reversed", [M("r#1", "open", "2026-10-02T00:00:00Z"), M("r#1", "done", "2026-10-02T00:00:00Z")],
     {"r#1": "열림"}),
    ("ref-ignored", [M("r#1", "open", "2026-10-01T00:00:00Z"), M("r#1", "ref", "2026-10-03T00:00:00Z")],
     {"r#1": "열림"}),
    ("ref-only-undetermined", [M("r#1", "ref", "2026-10-01T00:00:00Z")], {}),
    ("after-t-ignored", [M("r#1", "open", "2026-10-01T00:00:00Z"), M("r#1", "done", "2026-10-21T00:00:00Z")],
     {"r#1": "열림"}),
    ("exactly-t-counts", [M("r#1", "open", "2026-10-01T00:00:00Z"), M("r#1", "done", QTS)], {"r#1": "끝남"}),
    ("only-after-t", [M("r#1", "open", "2026-10-21T00:00:00Z")], {}),
])
def test_role_states_rule(name, mentions, expected):
    assert {k: v.state for k, v in s3.role_states(mentions, CUT).items()} == expected


def test_role_states_date_is_kst_day_of_deciding_mention():
    st = s3.role_states([M("r#1", "open", "2026-10-01T16:00:00Z")], CUT)   # = 10-02 01:00 KST
    assert st["r#1"].day == "2026-10-02"


def test_read_store_keeps_only_valid_nonref_mentions(store):
    got = s3.read_store(str(store), 1.0)
    assert {(m.memory_id, m.key) for m in got.mentions} == {
        ("m1", "hindsight-crew#10"), ("m1", "hindsight-crew#11"), ("m2", "hindsight-crew#10"),
        ("m4", "other-repo#3"), ("m5", "hindsight-crew#11"), ("m7", "hindsight-crew#13")}
    states = s3.role_states(got.mentions, CUT)
    assert {k: v.state for k, v in states.items()} == {
        "hindsight-crew#10": "끝남", "hindsight-crew#11": "열림", "other-repo#3": "열림", "hindsight-crew#13": "끝남"}


# --------------------------------------------------------------------------
# 켜짐: 기본 동작, 두 표면, 뒤 패스
# --------------------------------------------------------------------------

DEFAULT_TEXT = ("<item_open_table>\n"
                f"기준일 {T_DAY} 현재 뱅크 기억(GitHub 근거가 있으면 함께)으로 정리한 작업 키 후보다(기준일 이전 근거만). "
                "열린 키도 이미 끝났을 수 있고 여기 없는 키도 열려 있을 수 있으니 "
                "다른 기억과 함께 판단하라.\n줄: 키 | 상태\n"   # R7-2(#396) 기본 머리
                "hindsight-crew#11 | 열림\n"
                "</item_open_table>")


@pytest.mark.parametrize("shape", ["double", "direct", "direct-only"])
@pytest.mark.parametrize("flag", ["1", "true", "yes", "on"])
def test_on_attaches_table_on_both_surfaces(monkeypatch, tmp_path, store, shape, flag):
    off, _, _ = run(monkeypatch, tmp_path / "off", flag=None, shape=shape)
    resp, log, up = run(monkeypatch, tmp_path / "on", flag=flag, shape=shape)
    for element in elements_of(resp):
        decode_element(element)                                  # 두 표면이 같다
    on_payload, off_payload = payload_of(resp), payload_of(off)
    assert on_payload["results"] == off_payload["results"]       # 결과 행 불변
    assert on_payload[FIELD] == {"t": T_DAY, "fmt": "a", "keys": ["hindsight-crew#11"],
                                 "truncated": 0, "text": DEFAULT_TEXT}            # rows 없음(#387 이중 적재 제거)
    if shape != "direct-only":
        assert all(FIELD in json.loads(b["text"]) for b in elements_of(resp)[0]["result"]["content"])
    assert [(c["method"], c["path"]) for c in up.calls] == [("POST", "/mcp")]     # upstream 추가 호출 0
    (audit,) = s3_audit(log)
    assert audit["reason"] == "attached"
    info = audit["item_open_table"]
    assert info["rows"] == 1 and info["elements"] == 1 and info["knobs_source"] == "default"
    assert "aliases_mismatch" not in info
    assert "BODY-SECRET" not in log.read_text(encoding="utf-8")


def test_table_is_not_appended_outside_json(monkeypatch, tmp_path, store):
    resp, _, _ = run(monkeypatch, tmp_path)
    for b in elements_of(resp)[0]["result"]["content"]:
        json.loads(b["text"])                                    # 텍스트 사본은 그대로 JSON 하나다
    assert resp.content.count(b"<item_open_table>") == 2         # 두 표면에 한 번씩


@pytest.mark.parametrize("env", [
    {},
    {"HC_RECALL_KIND_LABEL": "1"},
    {"HC_RECALL_PRUNE": "0"},
    {"HC_UNESCAPE": "0"},
    {"HC_RECALL_KIND_LABEL": "1", "HC_ITEM_STATUS": "1", "HC_ITEM_LEDGER": "1"},
])
def test_back_passes_keep_the_field(monkeypatch, tmp_path, store, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    resp, _, _ = run(monkeypatch, tmp_path)
    expected = s3.payload_table(s3.build_table(s3.role_states(s3.read_store(str(store), 1.0).mentions, CUT), CUT,
                                               {"hindsight-crew"}, s3.DEFAULTS))
    assert table_of(resp) == expected


def test_x1_and_s1_fixed_bytes_still_hold_with_s3_off(monkeypatch, tmp_path, store):
    """S3 꺼짐 + X1 켜짐: test_item_ledger 의 SHA_X1_ON 과 같다(S3 블록이 X1·S1 을 바꾸지 않는다)."""
    from test_item_ledger import SHA_X1_ON
    resp, _, _ = status_run(monkeypatch, tmp_path / "x1", flag="1")
    assert sha(resp) == SHA_X1_ON


def test_batch_each_element_gets_its_own_table(monkeypatch, tmp_path, store):
    monkeypatch.setenv(s3.ENV_FLAG, "1")
    up = StatusUpstream()
    client, log = make_app(up, tmp_path)
    body = [{"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "recall", "arguments": {
                "query": STATUS_Q, "query_timestamp": QTS, "tags": [PROJECT]}}},
            {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "recall", "arguments": {
                "query": STATUS_Q, "query_timestamp": "2026-10-03T00:00:00Z", "tags": [PROJECT]}}}]
    with client:
        resp = client.post(f"/mcp/{BANK}/", json=body,
                           headers={"authorization": f"Bearer {TOKEN}", "content-type": "application/json"})
    tables = {e["id"]: decode_element(e)[FIELD] for e in elements_of(resp)}
    assert tables[11]["keys"] == ["hindsight-crew#11"]
    assert tables[12]["t"] == "2026-10-03"
    assert tables[12]["keys"] == ["hindsight-crew#10", "hindsight-crew#11"]
    assert len(s3_audit(log)) == 1


def test_missing_query_timestamp_uses_now(monkeypatch, tmp_path, store):
    resp, _, _ = run(monkeypatch, tmp_path, args={"query_timestamp": None})
    table = table_of(resp)
    assert table["t"] >= "2026-11-01"
    assert table["keys"] == []                                                      # 끝남은 기본 표에 없다


@pytest.mark.parametrize("bad", ["not-a-time", "2026-13-01"])
def test_unreadable_query_timestamp_attaches_nothing(monkeypatch, tmp_path, store, opens, bad):
    resp, log, _ = run(monkeypatch, tmp_path, args={"query_timestamp": bad})
    assert sha(resp) == hashlib.sha256(run(monkeypatch, tmp_path / "off", flag=None,
                                           args={"query_timestamp": bad})[0].content).hexdigest()
    (audit,) = s3_audit(log)
    assert audit["reason"] == "not-attached" and audit["item_open_table"]["bad_query_timestamp"] == 1
    assert opens == []


def test_error_result_is_untouched(monkeypatch, tmp_path, store):
    off, _, _ = run(monkeypatch, tmp_path / "off", flag=None, is_error=True)
    resp, _, _ = run(monkeypatch, tmp_path / "on", is_error=True)
    assert resp.content == off.content


# --------------------------------------------------------------------------
# (c) 손잡이
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fmt,line,cols", [("a", "hindsight-crew#11 | 열림", "키 | 상태"),
                                           ("b", "hindsight-crew#11 | 열림 | 2026-10-01", "키 | 상태 | 날짜")])
def test_table_fmt(monkeypatch, tmp_path, store, fmt, line, cols):
    resp, _, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": fmt})
    table = table_of(resp)
    assert table["fmt"] == fmt
    assert table["text"].splitlines()[3:5] == [line, "</item_open_table>"]
    assert table["text"].splitlines()[1].startswith("기준일") and f"\n줄: {cols}\n" in table["text"]
    assert table["keys"] == ["hindsight-crew#11"] and "rows" not in table      # 구조 필드는 fmt 와 무관


def test_header_template(monkeypatch, tmp_path, store):
    resp, _, _ = run(monkeypatch, tmp_path, knobs={"header": "열린 키 {t} [{cols}] {{x}}"})
    assert table_of(resp)["text"] == ("<item_open_table>\n열린 키 2026-10-20 [키 | 상태] {x}\n"
                                      "hindsight-crew#11 | 열림\n</item_open_table>")


@pytest.mark.parametrize("bad", ["", "x" * 401, "{key}", "{t:>5}", "{t!r}", "{", 5])
def test_header_invalid_falls_back_with_warning(monkeypatch, tmp_path, store, bad):
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"header": bad})
    assert table_of(resp)["text"] == DEFAULT_TEXT
    assert s3_audit(log)[0]["item_open_table"]["knob_warnings"] == ["header"]


@pytest.mark.parametrize("query,terms,attached", [
    ("남은 일 알려줘", None, True),
    ("What is the STATUS now", None, True),
    ("openapi 스키마 정리", None, False),          # #387: 옛 어휘 `open` 은 기본에서 뺐다
    ("openapi 스키마 정리", s3.STATUS_TERMS_V1, True),
    ("어제 회의 요약", None, False),
    ("어제 회의 요약", ["회의"], True),
    ("남은 일", ["회의"], False),
])
def test_attach_status_query(monkeypatch, tmp_path, store, opens, query, terms, attached):
    knobs = {"attach": "status_query", **({"status_terms": terms} if terms else {})}
    resp, log, _ = run(monkeypatch, tmp_path, knobs=knobs, args={"query": query})
    assert (FIELD in payload_of(resp)) is attached
    assert s3_audit(log)[0]["reason"] == ("attached" if attached else "not-attached")
    assert bool(opens) is attached                             # 붙이지 않으면 저장소도 열지 않는다


@pytest.mark.parametrize("bad", [[], ["x"] * 41, [""], ["  "], ["x" * 41], "열림", [1]])
def test_status_terms_invalid(bad):
    values, warnings = s3.parse_knobs(json.dumps({"status_terms": bad}))
    assert warnings == ["status_terms"] and values["status_terms"] == s3.STATUS_TERMS_DEFAULT


@pytest.mark.parametrize("days,expected", [
    (0, [["hindsight-crew#11", "열림", "2026-10-01"]]),
    (1, [["hindsight-crew#11", "열림", "2026-10-01"]]),                                  # 10-20 만
    (2, [["hindsight-crew#11", "열림", "2026-10-01"], ["hindsight-crew#13", "끝남", "2026-10-19"]]),
    (16, [["hindsight-crew#10", "끝남", "2026-10-05"], ["hindsight-crew#11", "열림", "2026-10-01"],
          ["hindsight-crew#13", "끝남", "2026-10-19"]]),
    (15, [["hindsight-crew#11", "열림", "2026-10-01"], ["hindsight-crew#13", "끝남", "2026-10-19"]]),  # 10-06 부터
])
def test_recent_done_days(monkeypatch, tmp_path, store, days, expected):
    resp, _, _ = run(monkeypatch, tmp_path, knobs={"recent_done_days": days, "table_fmt": "b"})
    table = table_of(resp)
    assert table["keys"] == [k for k, st, _ in expected if st == "열림"]      # 끝남 키는 keys 에 없다(#387-1)
    assert table["text"].splitlines()[3:-1] == [" | ".join(r) for r in expected]        # 상태·날짜는 text 만


def test_max_rows_keeps_latest_and_reports_truncation(monkeypatch, tmp_path):
    mems = [(f"k{i}", f"2026-10-{i:02d}T01:00:00Z", "valid", {f"hindsight-crew#{i}": "open"}) for i in range(1, 6)]
    make_store(tmp_path / "store" / "item-roles.sqlite", mems)
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"max_rows": 2})
    table = table_of(resp)
    assert table["keys"] == ["hindsight-crew#4", "hindsight-crew#5"]
    assert table["truncated"] == 3
    assert table["text"].splitlines()[-2] == "(상한 2줄: 날짜가 오래된 키 3개 생략)"
    assert s3_audit(log)[0]["item_open_table"]["truncated"] == 3


def test_rows_sorted_by_key_number(monkeypatch, tmp_path):
    mems = [("a", "2026-10-01T01:00:00Z", "valid", {"hindsight-crew#100": "open", "hindsight-crew#9": "open"})]
    make_store(tmp_path / "store" / "item-roles.sqlite", mems)
    resp, _, _ = run(monkeypatch, tmp_path)
    assert table_of(resp)["keys"] == ["hindsight-crew#9", "hindsight-crew#100"]


@pytest.mark.parametrize("scope,tags,policies,expected", [
    ("request_project", [PROJECT], None, ["hindsight-crew#11"]),
    ("request_project", ["project:other-repo"], None, ["other-repo#3"]),
    ("request_project", [PROJECT, "project:other-repo"], None, ["hindsight-crew#11", "other-repo#3"]),
    ("request_project", None, None, ["hindsight-crew#11", "other-repo#3"]),                   # project 없음 = all
    ("request_project", ["kind:handoff"], None, ["hindsight-crew#11", "other-repo#3"]),
    ("request_project", ["project:*"], None, ["hindsight-crew#11", "other-repo#3"]),           # glob 은 범위 아님
    ("request_project", ["project:hc"], "hc", ["hindsight-crew#11"]),                          # 별칭 정규화
    ("request_project", ["project:hc"], None, []),                                             # 별칭 없으면 hc 는 다른 repo
    ("all", [PROJECT], None, ["hindsight-crew#11", "other-repo#3"]),
])
def test_scope(monkeypatch, tmp_path, store, scope, tags, policies, expected):
    bp = None
    if policies:
        bp = BankPolicies.from_dict({"defaults": {}, "banks": {BANK: {"item_repo_aliases": ALIASES_HC}}})
    resp, _, _ = run(monkeypatch, tmp_path, knobs={"scope": scope}, args={"tags": tags}, bank_policies=bp)
    assert table_of(resp)["keys"] == expected


@pytest.mark.parametrize("raw,warnings", [
    ("not json", ["invalid-json"]),
    ("[1]", ["not-an-object"]),
    (json.dumps({"table_fmt": "c", "max_rows": 0, "recent_done_days": 31, "timeout_ms": 50, "scope": "x",
                 "attach": "x", "bogus!": 1}),
     ["table_fmt", "max_rows", "recent_done_days", "timeout_ms", "scope", "attach", "unknown:bogus"]),
    (json.dumps({"max_rows": True}), ["max_rows"]),
])
def test_parse_knobs_invalid(raw, warnings):
    values, got = s3.parse_knobs(raw)
    assert got == warnings and values == s3.DEFAULTS


def test_knobs_file_reload_and_env_priority(monkeypatch, tmp_path, store):
    cfg = tmp_path / "cfg.json"
    monkeypatch.setenv(s3.ENV_CONFIG, str(cfg))
    cfg.write_text(json.dumps({"table_fmt": "b"}), encoding="utf-8")
    resp, log, _ = run(monkeypatch, tmp_path / "1")
    assert table_of(resp)["fmt"] == "b"
    assert s3_audit(log)[0]["item_open_table"]["knobs_source"] == "file"
    cfg.write_text(json.dumps({"table_fmt": "a", "max_rows": 7}), encoding="utf-8")
    os.utime(cfg, ns=(1, 1))
    assert s3.load_knobs().values["max_rows"] == 7
    resp, log, _ = run(monkeypatch, tmp_path / "2", knobs={"table_fmt": "b"})
    info = s3_audit(log)[0]["item_open_table"]
    assert info["knobs_source"] == "env" and table_of(resp)["fmt"] == "b"
    assert info["knobs_sha256"] == s3.Knobs({**s3.DEFAULTS, "table_fmt": "b"}, "env").sha256


# --------------------------------------------------------------------------
# (d) fail-open
# --------------------------------------------------------------------------

def _no_store(tmp_path):
    pass


def _garbage(tmp_path):
    p = tmp_path / "store" / "item-roles.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"this is not a sqlite database" * 10)


def _bad_version(tmp_path):
    make_store(tmp_path / "store" / "item-roles.sqlite", schema_version="2")


def _missing_table(tmp_path):
    make_store(tmp_path / "store" / "item-roles.sqlite", drop_table="mention_roles")


def _no_version(tmp_path):
    p = make_store(tmp_path / "store" / "item-roles.sqlite")
    conn = sqlite3.connect(p)
    conn.execute("DELETE FROM meta WHERE k='schema_version'")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("setup,reason", [
    (_no_store, "no-store"),
    (_garbage, "open-failed:DatabaseError"),
    (_bad_version, "schema-mismatch"),
    (_missing_table, "schema-mismatch"),
    (_no_version, "schema-mismatch"),
    ("timeout", "timeout"),
])
def test_fail_open(monkeypatch, tmp_path, setup, reason):
    if setup == "timeout":
        make_store(tmp_path / "store" / "item-roles.sqlite")
        ticks = iter(range(0, 10 ** 6, 10))                       # 부를 때마다 10초씩 흐르는 시계
        monkeypatch.setattr(s3, "_clock", lambda: next(ticks))
    else:
        setup(tmp_path)
    off, _, _ = run(monkeypatch, tmp_path / "off", flag=None)
    resp, log, _ = run(monkeypatch, tmp_path)
    assert resp.content == off.content
    assert sha(resp) == hashlib.sha256(off.content).hexdigest()
    (audit,) = s3_audit(log)
    assert audit["reason"] == reason


def test_fail_open_on_slow_store_thread(monkeypatch, tmp_path, store):
    """저장소 읽기가 스레드에서 상한을 넘기면 기다리지 않고 원래 바이트."""
    import time as _time

    def slow(path, timeout_s):
        _time.sleep(0.4)
        raise AssertionError("not reached in time")
    monkeypatch.setattr(s3, "read_store", slow)
    off, _, _ = run(monkeypatch, tmp_path / "off", flag=None)
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"timeout_ms": 100})
    assert resp.content == off.content
    assert s3_audit(log)[0]["reason"] == "timeout"


def test_unexpected_exception_is_fail_open(monkeypatch, tmp_path, store):
    monkeypatch.setattr(s3, "role_states", lambda *a: 1 / 0)
    off, _, _ = run(monkeypatch, tmp_path / "off", flag=None)
    resp, log, _ = run(monkeypatch, tmp_path)
    assert resp.content == off.content
    assert s3_audit(log)[0]["reason"] == "failed:ZeroDivisionError"


# --------------------------------------------------------------------------
# (e) 읽기 전용
# --------------------------------------------------------------------------

def test_read_only_connection_cannot_write(store):
    conn = s3.connect_ro(str(store))
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO meta VALUES ('x', 'y')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("PRAGMA journal_mode=WAL").fetchall()
            conn.execute("CREATE TABLE z(a)")
    finally:
        conn.close()


def test_recall_leaves_store_bytes_and_directory_unchanged(monkeypatch, tmp_path, store):
    before = hashlib.sha256(store.read_bytes()).hexdigest()
    files = sorted(os.listdir(store.parent))
    resp, _, _ = run(monkeypatch, tmp_path)
    assert table_of(resp) is not None
    assert hashlib.sha256(store.read_bytes()).hexdigest() == before
    assert sorted(os.listdir(store.parent)) == files            # -journal/-wal/-shm 없음


def test_replaced_store_is_read_fresh_each_recall(monkeypatch, tmp_path, store):
    """S4 는 새 파일을 써서 os.replace 로 바꿔 넣는다. 다음 recall 은 새 내용을 본다."""
    resp, _, _ = run(monkeypatch, tmp_path / "1")
    assert table_of(resp)["keys"] == ["hindsight-crew#11"]
    new = make_store(tmp_path / "store" / "next.sqlite",
                     [("n1", "2026-10-02T00:00:00Z", "valid", {"hindsight-crew#77": "open"})])
    os.replace(new, store)
    resp, _, _ = run(monkeypatch, tmp_path / "2")
    assert table_of(resp)["keys"] == ["hindsight-crew#77"]


# --------------------------------------------------------------------------
# 별칭 표 sha256 (#376 과 맞춘 계약)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("aliases", [ALIASES_HC, {"HC": "Hindsight-Crew"}, {" hc ": "hindsight-crew"}])
def test_aliases_sha256_contract_constant(aliases):
    assert s3.aliases_sha256(aliases) == ALIASES_HC_SHA


@pytest.mark.parametrize("meta_sha,policies,mismatch", [
    (EMPTY_ALIASES_SHA, None, False),
    (ALIASES_HC_SHA, ALIASES_HC, False),
    (EMPTY_ALIASES_SHA, ALIASES_HC, True),
    ("0" * 64, None, True),
])
def test_aliases_mismatch_is_a_warning_only(monkeypatch, tmp_path, meta_sha, policies, mismatch):
    make_store(tmp_path / "store" / "item-roles.sqlite", aliases_sha=meta_sha)
    bp = BankPolicies.from_dict({"defaults": {}, "banks": {BANK: {"item_repo_aliases": policies}}}) if policies else None
    resp, log, _ = run(monkeypatch, tmp_path, bank_policies=bp)
    assert table_of(resp) is not None                           # 표는 붙는다
    info = s3_audit(log)[0]["item_open_table"]
    assert info.get("aliases_mismatch", False) is mismatch


# --------------------------------------------------------------------------
# 뱅크 한정 (final-review #377-1): 저장소는 한 뱅크 전용, HC_ITEM_OPEN_TABLE_BANK 와 요청 뱅크가 같을 때만
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bank_env,reason", [
    (None, "bank-unset"),
    ("", "bank-unset"),
    ("   ", "bank-unset"),
    ("personal-bob", "other-bank"),
    (BANK.upper(), "other-bank"),                  # 정확히 같아야 한다
    (BANK, "attached"),
])
def test_bank_scope(monkeypatch, tmp_path, store, opens, bank_env, reason):
    set_env(monkeypatch, ENV_BANK, bank_env)
    off, _, _ = run(monkeypatch, tmp_path / "off", flag=None)
    resp, log, _ = run(monkeypatch, tmp_path)
    (audit,) = s3_audit(log)
    assert audit["reason"] == reason
    if reason == "attached":
        assert table_of(resp)["keys"] == ["hindsight-crew#11"]
        assert len(opens) == 1
    else:
        assert resp.content == off.content                   # 원본 바이트
        assert opens == []                                   # 저장소를 열지 않는다


def test_bank_env_is_not_read_when_flag_off(monkeypatch, tmp_path, store, opens):
    set_env(monkeypatch, ENV_BANK, "personal-bob")
    resp, log, _ = status_run(monkeypatch, tmp_path / "run", flag=None)
    assert sha(resp) == BASELINE_SHA256 and s3_audit(log) == [] and opens == []


# --------------------------------------------------------------------------
# (g) R6-1(#387): payload 축소 - 새 모양, 상태 질의일 때만, 요청·결과 project 범위
# --------------------------------------------------------------------------

# 손잡이 기본값의 정규 sha256(키 정렬·압축 JSON). 런 5 기본값(attach always, scope request_project, 옛 어휘)과 다르다.
DEFAULTS_SHA256_R6 = "0ca334da8e68e695b1248da30df7616e7818c97ead3041e4d8cb9026c01883b7"
RUN5_DEFAULTS = {**s3.DEFAULTS, "attach": "always", "scope": "request_project", "status_terms": s3.STATUS_TERMS_V1}
KEYED_ROW = "open-251"                     # BASE_ROWS 첫 행: hindsight-crew#251 을 언급
KEYLESS_ROWS = [mrow("plain", text="[발견] 키 없는 기억 BODY-SECRET", tags=("kind:gotcha",))]


def test_new_defaults_constants():
    assert s3.DEFAULTS["attach"] == "status_query"
    assert s3.DEFAULTS["scope"] == "request_or_result_project"
    assert s3.DEFAULTS["status_terms"] == s3.STATUS_TERMS_DEFAULT != s3.STATUS_TERMS_V1
    assert s3.STATUS_TERMS_V1 == ["열림", "남은", "미완", "할 일", "진행", "대기",
                                  "open", "todo", "pending", "remaining", "backlog", "status"]
    assert "open" not in s3.STATUS_TERMS_DEFAULT
    for term in ("할 일", "남은", "진행 중", "끝난", "미완", "상태", "목록"):
        assert term in s3.STATUS_TERMS_DEFAULT
    assert s3._valid_terms(s3.STATUS_TERMS_DEFAULT)
    # R6-3(#391) 뒤 기본 머리가 바뀌었으므로 R6-1 기본값은 머리를 HEADER_R6_1 로 명시해야 재현된다.
    assert s3.Knobs({**s3._defaults(), "header": s3.HEADER_R6_1}, "default").sha256 == DEFAULTS_SHA256_R6
    assert s3.Knobs(RUN5_DEFAULTS, "default").sha256 != DEFAULTS_SHA256_R6


@pytest.mark.parametrize("scope", ["request_or_result_project", "request_project", "all"])
def test_parse_knobs_accepts_scopes(scope):
    values, warnings = s3.parse_knobs(json.dumps({"scope": scope}))
    assert warnings == [] and values["scope"] == scope


@pytest.mark.parametrize("bad", ["result", "request_or_result", "", None, 1])
def test_parse_knobs_rejects_bad_scope(bad):
    values, warnings = s3.parse_knobs(json.dumps({"scope": bad}))
    assert warnings == ["scope"] and values["scope"] == "request_or_result_project"


def test_payload_table_drops_rows_but_build_table_keeps_them():
    states = {"hindsight-crew#11": s3.KeyState(s3.STATE_OPEN, CUT.replace(day=1)),
              "other-repo#3": s3.KeyState(s3.STATE_OPEN, CUT.replace(day=7))}
    table = s3.build_table(states, CUT, None, {**s3.DEFAULTS, "table_fmt": "b"})
    assert table["rows"] == [["hindsight-crew#11", "열림", "2026-10-01"], ["other-repo#3", "열림", "2026-10-07"]]
    out = s3.payload_table(table)
    assert out == {"t": T_DAY, "fmt": "b", "keys": ["hindsight-crew#11", "other-repo#3"], "truncated": 0,
                   "text": table["text"]}
    assert list(out) == ["t", "fmt", "keys", "truncated", "text"]


# 행렬: 상태 어휘 유무 x 요청 project 태그 유무 x 결과 키 유무 -> 부착 여부·범위(기본 손잡이).
# 요청 태그는 other-repo(저장소 열린 키 other-repo#3), 결과 키는 hindsight-crew#251(열린 키 hindsight-crew#11).
@pytest.mark.parametrize("result_keys", [True, False], ids=["result-keys", "no-result-keys"])
@pytest.mark.parametrize("project_tag", [True, False], ids=["tag", "no-tag"])
@pytest.mark.parametrize("status_term", [True, False], ids=["status", "general"])
def test_attach_and_scope_matrix(monkeypatch, tmp_path, store, opens, status_term, project_tag, result_keys):
    rows = None if result_keys else KEYLESS_ROWS
    args = {"query": STATUS_Q if status_term else "어제 회의 요약",
            "tags": ["project:other-repo"] if project_tag else None}
    off, _, _ = run(monkeypatch, tmp_path / "off", flag=None, args=args, rows=rows)
    resp, log, _ = run(monkeypatch, tmp_path, args=args, rows=rows)
    (audit,) = s3_audit(log)
    info = audit["item_open_table"]
    assert info["scope"] == "request_or_result_project" and info["attach"] == "status_query"
    if not status_term:
        expected, why = None, "no-status-term"                 # 태그가 있어도 붙이지 않는다(코디네이터 결정)
    elif project_tag:
        expected, why = ["other-repo#3"], "request"
    elif result_keys:
        expected, why = ["hindsight-crew#11"], "result"
    else:
        expected, why = None, "no-result-keys"                 # 뱅크 전체는 담지 않는다
    if expected is None:
        assert resp.content == off.content                     # 원본 바이트
        assert audit["reason"] == "not-attached" and info["not_attached"] == why
        assert opens == []                                     # 저장소를 열지 않는다
    else:
        table = table_of(resp)
        assert table["keys"] == expected and "rows" not in table
        assert audit["reason"] == "attached" and info["scope_from"] == {why: 1}
        assert "not_attached" not in info
        assert len(opens) == 1
    assert "BODY-SECRET" not in log.read_text(encoding="utf-8")


@pytest.mark.parametrize("scope,tags,expected", [
    ("request_project", None, ["hindsight-crew#11", "other-repo#3"]),      # 런 5: 태그 없으면 뱅크 전체
    ("all", None, ["hindsight-crew#11", "other-repo#3"]),
    ("request_or_result_project", None, ["hindsight-crew#11"]),            # 결과 키 repo 만
    ("request_or_result_project", ["project:*"], ["hindsight-crew#11"]),   # glob 은 요청 범위가 아니다 -> 결과
    ("request_or_result_project", ["project:other-repo"], ["other-repo#3"]),
])
def test_scope_values_without_request_project(monkeypatch, tmp_path, store, scope, tags, expected):
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"scope": scope}, args={"tags": tags})
    assert table_of(resp)["keys"] == expected
    assert s3_audit(log)[0]["item_open_table"]["scope"] == scope


def test_result_keys_are_alias_normalized(monkeypatch, tmp_path):
    bp = BankPolicies.from_dict({"defaults": {}, "banks": {BANK: {"item_repo_aliases": ALIASES_HC}}})
    rows = [mrow("a", text="[인계] hc#5 남은 일", tags=("kind:handoff",))]
    make_store(tmp_path / "store" / "item-roles.sqlite", aliases_sha=ALIASES_HC_SHA)
    resp, log, _ = run(monkeypatch, tmp_path, args={"tags": None}, rows=rows, bank_policies=bp)
    assert table_of(resp)["keys"] == ["hindsight-crew#11"]
    assert s3_audit(log)[0]["item_open_table"]["scope_from"] == {"result": 1}


def test_result_repos_and_table_scope_pure():
    rows = [mrow("a", text="x #3 y", tags=("project:other-repo",)), "not-a-row", mrow("b", text="no key", tags=())]
    assert s3.result_repos(rows, {}) == {"other-repo"}
    assert s3.result_repos(None, {}) == set()
    knobs = s3._defaults()
    assert s3.table_scope({"tags": None}, rows, knobs, {}) == ({"other-repo"}, "result")
    assert s3.table_scope({"tags": ["project:z"]}, rows, knobs, {}) == ({"z"}, "request")
    assert s3.table_scope({}, [], knobs, {}) == (None, "no-result-keys")
    assert s3.table_scope({}, [], {**knobs, "scope": "request_project"}, {}) == (None, "all")
    assert s3.table_scope({"tags": ["project:z"]}, rows, {**knobs, "scope": "all"}, {}) == (None, "all")


def test_batch_not_attached_reasons_are_counted(monkeypatch, tmp_path, store, opens):
    monkeypatch.setenv(s3.ENV_FLAG, "1")
    up = StatusUpstream(rows=KEYLESS_ROWS)
    client, log = make_app(up, tmp_path)
    body = [{"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "recall", "arguments": {
                "query": "어제 회의 요약", "query_timestamp": QTS}}},
            {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "recall", "arguments": {
                "query": STATUS_Q, "query_timestamp": QTS}}}]
    with client:
        resp = client.post(f"/mcp/{BANK}/", json=body,
                           headers={"authorization": f"Bearer {TOKEN}", "content-type": "application/json"})
    assert all(FIELD not in decode_element(e) for e in elements_of(resp))
    (audit,) = s3_audit(log)
    assert audit["reason"] == "not-attached"
    assert audit["item_open_table"]["not_attached"] == {"no-result-keys": 1, "no-status-term": 1}
    assert opens == []


def test_payload_keys_are_open_keys_only():
    """rework #387-1: recent_done_days>0 이면 text 에 최근 끝남 키가 들어가지만 keys 는 열린 키만 담는다."""
    states = {"hindsight-crew#11": s3.KeyState(s3.STATE_OPEN, CUT.replace(day=1)),
              "hindsight-crew#13": s3.KeyState(s3.STATE_DONE, CUT.replace(day=19))}
    table = s3.build_table(states, CUT, None, {**s3.DEFAULTS, "recent_done_days": 7, "table_fmt": "b"})
    assert [r[0] for r in table["rows"]] == ["hindsight-crew#11", "hindsight-crew#13"]
    out = s3.payload_table(table)
    assert out["keys"] == ["hindsight-crew#11"]
    assert "hindsight-crew#13 | 끝남 | 2026-10-19" in out["text"]


# --------------------------------------------------------------------------
# (h) R6-3(#391): 기본 머리 C7 교체 - 표 밖 키 지시 한 문장, 옛 머리는 HEADER_R6_1
# --------------------------------------------------------------------------

# 머리 텍스트 sha256(끝 줄바꿈 없음). #388 하네스 C7_HEADER_SHA256 과 같은 값.
HEADER_C7_SHA256 = "9155cd04ccf246252113ebecd2be77c7a04c7d809ae5e9311c5160b35e11db58"
# 새 기본 손잡이 정규 sha256. #388 C7 측정 감사 줄 knobs_sha256 과 같은 값(r6-report.md 2절).
DEFAULTS_SHA256_R6_3 = "24044afd737184e2303e35a75e94d4aee7a2702eb32999c1c79d855c0627dfb8"
HEADER_R6_1_TEXT = ("기준일 {t} 현재 뱅크 기억 기준 열린 작업 키다(기준일 이전 기억만). "
                    "여기 없는 키는 뱅크 기억 기준 열림이 아니거나 질의 범위 밖이다.\n줄: {cols}")


def test_header_c7_default_constants():
    assert hashlib.sha256(s3.HEADER_C7.encode("utf-8")).hexdigest() == HEADER_C7_SHA256   # R7-2(#396): C7 은 상수로
    assert s3.HEADER_R6_1.encode("utf-8") == HEADER_R6_1_TEXT.encode("utf-8")
    first, cols = HEADER_R6_1_TEXT.split("\n")
    assert s3.HEADER_C7 == first + " 이 표에 없는 작업 키는 열린 목록에 넣지 마라.\n" + cols
    assert s3._valid_header(s3.HEADER_DEFAULT) and s3._valid_header(s3.HEADER_R6_1)
    assert s3.DEFAULTS["header"] == s3.HEADER_DEFAULT
    assert s3.Knobs({**s3._defaults(), "header": s3.HEADER_C7}, "default").sha256 == DEFAULTS_SHA256_R6_3
    assert s3.parse_knobs("{}") == (s3._defaults(), [])
