"""S1 읽기 시점 항목 상태 원장 (issue #363, 아키텍처 v2 런 5 R5-B1, 플래그 HC_ITEM_LEDGER 기본 꺼짐), mock upstream 만 쓴다.

고정하는 계약(행렬: 경우 x 플래그/표면/손잡이):
  (a) 꺼짐(미설정·"0"·빈 값·알 수 없는 값) x X1(HC_ITEM_STATUS) 꺼짐/켜짐: MCP recall 응답 바이트가 이 변경 이전
      (origin/main 17f5485)과 같다. 손잡이 env 가 있어도 같다. 코드 기본값과 docker-compose 배선 기본값이 꺼짐이다.
  (b) 켜짐: 키를 말하는 결과 행마다 인라인 주석 + 구조 필드 `gateway_item_ledger`(두 표면, 응답 모양 3종, 위치 2종).
      완료 근거 없는 키는 무주석, `query_timestamp` 뒤 완료 무시, X1 꺼짐에서도 동작.
  (c) 모드 3종(표시만/강등/제외), 시각 규칙, 키 상한, 조회 범위, 별칭 표 사용, 뱅크 tag_aliases 완료 판정.
  (d) X1 조합: X1 이 붙인 행은 바이트 그대로, `skip_if_x1` 은 S1 출력만 바꾼다.
  (e) 손잡이 전달: 파일 mtime 재로드, env 인라인 JSON 우선, 잘못된 설정은 기본값 + 경고.
  (f) 시간 초과·upstream 오류: 원래 바이트(fail-open)와 감사 사유.
  (g) 연막: 키 여러 개·키당 행 여러 개 고정 예시에서 S1 추가 글자 수 < X1 덧붙임 글자 수.
감사 줄에 기억 본문이 없다(BODY-SECRET 표식).
"""
import hashlib
import json
import logging
import os
import re
from pathlib import Path

import pytest

from gateway import item_ledger, item_status
from gateway.bank_policies import BankPolicies
from test_item_status import (BANK, BASE_LIST, BASE_ROWS, PROJECT, audit_rows, decode_element, elements_of, lrow,
                              mrow, payload_of, rows_of, run as status_run, set_env)

REPO = Path(__file__).resolve().parent.parent
FIELD = "gateway_item_ledger"
X1_MARK = item_status.MARK_KEY

# 변경 이전(origin/main 17f5485) 코드가 BASE_ROWS·BASE_LIST 입력에 돌려준 MCP recall 응답 바이트의 sha256.
SHA_X1_OFF = "24ec7baf3a02bfb00b8c28f9873b7305337de7a95dc265fbfb0c5c2b65ef462b"   # = test_item_status.BASELINE_SHA256
SHA_X1_ON = "47d13a034bc848dc1402686ececb91cf30fed56416f2aa1b3ac7e20ac82695fc"

DEFAULT_NOTE = "[hindsight-crew#251 끝남 2026-10-15]"
EVIDENCE_251 = ["hindsight-crew#251", "done", "2026-10-15", "done-251-new"]   # [키, 상태, 완료 날짜, 근거 id]


@pytest.fixture(autouse=True)
def _ledger_env(monkeypatch, tmp_path):
    """셸에서 새는 S1 변수를 지우고, 설정 파일 기본 경로를 이 테스트의 빈 경로로 돌린다(파일 없음 = 기본값)."""
    for name in (item_ledger.ENV_FLAG, item_ledger.ENV_KNOBS, item_status.ENV_FLAG):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(item_ledger.ENV_CONFIG, str(tmp_path / "no-such-item-ledger.json"))
    item_ledger._reset_cache()
    yield
    item_ledger._reset_cache()


def run(monkeypatch, tmp_path, *, ledger="1", x1=None, knobs=None, args=None, **kwargs):
    """S1·X1 플래그와 손잡이(env 인라인 JSON)를 정해 recall 한 번. -> (응답, 감사 로그, mock upstream)."""
    set_env(monkeypatch, item_ledger.ENV_FLAG, ledger)
    set_env(monkeypatch, item_ledger.ENV_KNOBS, None if knobs is None else json.dumps(knobs, ensure_ascii=False))
    return status_run(monkeypatch, tmp_path, flag=x1, args=args, **kwargs)


def sha(resp):
    return hashlib.sha256(resp.content).hexdigest()


def ledger_audit(log):
    return audit_rows(log, item_ledger.DECISION)


def texts(resp):
    return [r.get("text") for r in rows_of(resp)]


def ids(resp):
    return [r["id"] for r in rows_of(resp)]


# --------------------------------------------------------------------------
# (a) 꺼짐: 바이트 불변, 배선 기본값 꺼짐
# --------------------------------------------------------------------------

def test_code_default_is_off(monkeypatch):
    monkeypatch.delenv(item_ledger.ENV_FLAG, raising=False)
    assert item_ledger.enabled() is False


@pytest.mark.parametrize("knobs", [None, {"mode": "drop", "note_template": "[X {key}]"}])
@pytest.mark.parametrize("x1,expected", [(None, SHA_X1_OFF), ("1", SHA_X1_ON)])
@pytest.mark.parametrize("ledger", [None, "0", "", "off", "no", "false", "garbage"])
def test_flag_off_bytes_unchanged(monkeypatch, tmp_path, ledger, x1, expected, knobs):
    resp, log, up = run(monkeypatch, tmp_path, ledger=ledger, x1=x1, knobs=knobs)
    assert sha(resp) == expected
    assert FIELD.encode() not in resp.content
    assert ledger_audit(log) == []
    if x1 is None:
        assert [(c["method"], c["path"]) for c in up.calls] == [("POST", "/mcp")]   # 찾기 호출 0회


def _compose_gateway_env():
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    block = text.split("\n  gateway:\n", 1)[1].split("\n    environment:\n", 1)[1].split("\n    ports:\n", 1)[0]
    return dict(re.findall(r"^      ([A-Z0-9_]+): (.*)$", block, re.M))


@pytest.mark.parametrize("name,wired", [
    ("HC_ITEM_LEDGER", "${HC_ITEM_LEDGER:-0}"),
    ("HC_ITEM_LEDGER_CONFIG", "${HC_ITEM_LEDGER_CONFIG:-/app/policies/item-ledger.json}"),
    ("HC_ITEM_LEDGER_KNOBS", "${HC_ITEM_LEDGER_KNOBS:-}"),
])
def test_compose_wiring_defaults_off(name, wired):
    """정적 검사(docker compose config 는 시크릿을 펼치므로 쓰지 않는다): 배선이 있고 기본값이 꺼짐/파일 없음/빈 값."""
    env = _compose_gateway_env()
    assert env.get(name) == wired
    assert env["HC_ITEM_STATUS"] == "${HC_ITEM_STATUS:-0}"         # X1 배선은 그대로


def test_compose_default_path_is_inside_the_policies_mount():
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- ./policies:/app/policies:ro" in text
    assert item_ledger.DEFAULT_CONFIG_PATH == "/app/policies/item-ledger.json"
    assert not (REPO / "policies" / "item-ledger.json").exists()     # 리포 기본 = 파일 없음 = 기본 손잡이


@pytest.mark.parametrize("path", ["gateway/item_ledger.py", "gateway/item_status.py"])
def test_no_bench_import(path):
    """완료 판정은 gateway 쪽 함수만 쓴다(라벨 쪽 bench 모듈과 서로 import 하지 않는다)."""
    assert "bench" not in (REPO / path).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# (b) 켜짐: 키를 말하는 행마다 주석(두 표면, 응답 모양, 위치)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("position", ["prefix", "suffix"])
@pytest.mark.parametrize("shape", ["double", "direct", "direct-only"])
@pytest.mark.parametrize("flag", ["1", "true", "on"])
def test_on_annotates_rows_on_both_surfaces(monkeypatch, tmp_path, shape, position, flag):
    off, _, _ = run(monkeypatch, tmp_path / "off", ledger=None, shape=shape)
    resp, log, up = run(monkeypatch, tmp_path / "on", ledger=flag, shape=shape, knobs={"note_position": position})
    for element in elements_of(resp):
        decode_element(element)                          # 두 표면이 같다
    on_rows, off_rows = rows_of(resp), rows_of(off)
    assert [r["id"] for r in on_rows] == [r["id"] for r in off_rows]     # 표시만: 집합·순서 불변
    open_on, plain_on = on_rows
    original = off_rows[0]["text"]
    expected = f"{DEFAULT_NOTE} {original}" if position == "prefix" else f"{original} {DEFAULT_NOTE}"
    assert open_on["text"] == expected
    assert open_on[FIELD] == [EVIDENCE_251]
    assert {k: v for k, v in open_on.items() if k not in ("text", FIELD)} == \
           {k: v for k, v in off_rows[0].items() if k != "text"}
    assert plain_on == off_rows[1]                       # 키 없는 행은 그대로
    assert up.list_qs() == ["#251"]
    (audit,) = ledger_audit(log)
    assert audit["reason"] == "annotated"
    info = audit["item_ledger"]
    assert info["annotated"] == 1 and info["keys"] == ["hindsight-crew#251"]
    assert info["knobs_source"] == "env" and len(info["knobs_sha256"]) == 64
    assert "BODY-SECRET" not in log.read_text(encoding="utf-8")


def test_x1_off_ledger_on_works_alone(monkeypatch, tmp_path):
    resp, _, up = run(monkeypatch, tmp_path, ledger="1", x1=None)
    rows = rows_of(resp)
    assert len(rows) == len(BASE_ROWS) and not any(X1_MARK in r for r in rows)
    assert rows[0][FIELD] == [EVIDENCE_251]
    assert rows[0]["text"].startswith(DEFAULT_NOTE)


MULTI_ROWS = [
    mrow("open-251-a", text="[인계] hindsight-crew#251 남은 일 BODY-SECRET-A", at="2026-10-01T00:00:00Z"),
    mrow("open-252", text="[할일] hindsight-crew#252 BODY-SECRET-B", tags=("kind:todo", PROJECT), at="2026-10-02T00:00:00Z"),
    mrow("both", text="[인계] hindsight-crew#251 과 hindsight-crew#252 BODY-SECRET-C", at="2026-10-03T00:00:00Z"),
    mrow("open-253", text="[인계] hindsight-crew#253 미완 BODY-SECRET-D", at="2026-10-04T00:00:00Z"),
    mrow("open-251-b", text="[인계] hindsight-crew#251 또 남은 일 BODY-SECRET-E", at="2026-10-05T00:00:00Z"),
]
MULTI_LIST = [
    lrow("done-251-new"),
    lrow("done-252", text="[완료] hindsight-crew#252 끝 BODY-SECRET-F", at="2026-10-20T01:00:00Z"),
]
EVIDENCE_252 = ["hindsight-crew#252", "done", "2026-10-20", "done-252"]


def test_every_row_mentioning_a_key_is_annotated_and_keys_without_evidence_are_not(monkeypatch, tmp_path):
    resp, _, up = run(monkeypatch, tmp_path, rows=MULTI_ROWS, list_rows=MULTI_LIST)
    by_id = {r["id"]: r for r in rows_of(resp)}
    assert by_id["open-251-a"][FIELD] == [EVIDENCE_251]
    assert by_id["open-251-b"][FIELD] == [EVIDENCE_251]
    assert by_id["open-252"][FIELD] == [EVIDENCE_252]
    assert by_id["both"][FIELD] == [EVIDENCE_251, EVIDENCE_252]          # 키 순서(정규 repo, 번호)
    assert by_id["both"]["text"].startswith(
        "[hindsight-crew#251 끝남 2026-10-15] [hindsight-crew#252 끝남 2026-10-20] ")
    assert FIELD not in by_id["open-253"]                                 # 완료 근거 없는 키는 무주석
    assert by_id["open-253"]["text"] == MULTI_ROWS[3]["text"]
    assert sorted(up.list_qs()) == ["#251", "#252", "#253"]


def test_evidence_row_itself_is_not_annotated(monkeypatch, tmp_path):
    rows = [*BASE_ROWS, mrow("done-251-new", text=BASE_LIST[1]["text"], tags=("kind:completion", PROJECT),
                             at="2026-10-15T10:00:00Z")]
    resp, _, _ = run(monkeypatch, tmp_path, rows=rows)
    by_id = {r["id"]: r for r in rows_of(resp)}
    assert by_id["open-251"][FIELD] == [EVIDENCE_251]
    assert FIELD not in by_id["done-251-new"]


@pytest.mark.parametrize("list_rows", [
    [],                                                                      # 후보 없음
    [lrow("handoff-only", tags=("kind:handoff", PROJECT))],                  # 완료 계열 아님
    [lrow("other-project", tags=("kind:completion", "project:other"))],      # 조회 범위 밖
    [lrow("invalidated", state="invalidated")],                              # 무효화된 행
    [lrow("other-key", text="[완료] hindsight-crew#2510 끝")],               # 부분 일치만 하는 다른 키
])
def test_no_evidence_leaves_bytes_unchanged(monkeypatch, tmp_path, list_rows):
    resp, log, _ = run(monkeypatch, tmp_path, list_rows=list_rows)
    assert sha(resp) == SHA_X1_OFF
    (audit,) = ledger_audit(log)
    assert audit["reason"] == "no-evidence"


# --------------------------------------------------------------------------
# query_timestamp
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cutoff,list_rows,expected", [
    ("2026-10-31T00:00:00Z", BASE_LIST, "done-251-new"),
    ("2026-10-13T00:00:00+09:00", BASE_LIST, "done-251-old"),          # 뒤 완료 무시, 그 이전 최신
    ("2026-10-15T10:00:00Z", BASE_LIST, "done-251-new"),               # 같은 시각은 이하
    ("2026-10-11T23:59:59+09:00", BASE_LIST, None),                    # 모든 완료가 뒤
    ("2026-10-31T00:00:00Z", [lrow("untimed", at=None)], None),         # 기준 시각이 있으면 시각 없는 근거 제외
    (None, [lrow("untimed", at=None)], "untimed"),                     # 기준 시각이 없으면 시각 없는 근거도 쓴다
])
def test_query_timestamp_cutoff(monkeypatch, tmp_path, cutoff, list_rows, expected):
    args = {"query_timestamp": cutoff} if cutoff else None
    resp, _, _ = run(monkeypatch, tmp_path, args=args, list_rows=list_rows)
    row = rows_of(resp)[0]
    if expected is None:
        assert FIELD not in row
        assert sha(resp) == SHA_X1_OFF
    else:
        assert [e[3] for e in row[FIELD]] == [expected]


def test_untimed_evidence_has_null_date(monkeypatch, tmp_path):
    resp, _, _ = run(monkeypatch, tmp_path, list_rows=[lrow("untimed", at=None)])
    row = rows_of(resp)[0]
    assert row[FIELD] == [["hindsight-crew#251", "done", None, "untimed"]]
    assert row["text"].startswith("[hindsight-crew#251 끝남 날짜 없음] ")


def test_unreadable_query_timestamp_leaves_bytes_unchanged(monkeypatch, tmp_path):
    resp, _, up = run(monkeypatch, tmp_path, args={"query_timestamp": "not-a-time"})
    assert FIELD.encode() not in resp.content
    assert up.list_qs() == []


# --------------------------------------------------------------------------
# (c) 모드·시각 규칙·키 상한·조회 범위·별칭
# --------------------------------------------------------------------------

MODE_ROWS = [
    mrow("open-old", text="[인계] hindsight-crew#251 오래된 남은 일", at="2026-10-10T00:00:00Z"),
    mrow("plain", text="[발견] 키 없는 기억", tags=("kind:gotcha", PROJECT)),
    mrow("open-after", text="[인계] hindsight-crew#251 완료 뒤 다시 남은 일", at="2026-10-20T00:00:00Z"),
    mrow("open-untimed", text="[인계] hindsight-crew#251 시각 없는 남은 일", at=None),
]


@pytest.mark.parametrize("mode,time_rule,expected_ids", [
    ("annotate", "strict", ["open-old", "plain", "open-after", "open-untimed"]),
    ("annotate", "lenient", ["open-old", "plain", "open-after", "open-untimed"]),
    ("demote", "strict", ["plain", "open-after", "open-untimed", "open-old"]),
    ("demote", "lenient", ["plain", "open-after", "open-old", "open-untimed"]),
    ("drop", "strict", ["plain", "open-after", "open-untimed"]),
    ("drop", "lenient", ["plain", "open-after"]),
])
def test_modes(monkeypatch, tmp_path, mode, time_rule, expected_ids):
    """강등·제외 대상 = 완료 근거(10-15)보다 오래된 열림 행. lenient 는 시각 없는 열림 행도 낡은 행으로 본다."""
    resp, log, _ = run(monkeypatch, tmp_path, rows=MODE_ROWS, knobs={"mode": mode, "time_rule": time_rule})
    rows = rows_of(resp)
    assert [r["id"] for r in rows] == expected_ids
    for r in rows:
        if r["id"] != "plain":
            assert r[FIELD] == [EVIDENCE_251]          # 남은 행은 모드와 무관하게 주석
            assert r["text"].startswith(DEFAULT_NOTE)
    info = ledger_audit(log)[0]["item_ledger"]
    assert info["mode"] == mode
    assert info["dropped"] == 4 - len(expected_ids)


def test_mode_never_touches_completion_rows(monkeypatch, tmp_path):
    rows = [mrow("old-done", text="[완료] hindsight-crew#251 1차 완료", tags=("kind:completion", PROJECT),
                 at="2026-10-01T00:00:00Z"), *BASE_ROWS]
    resp, _, _ = run(monkeypatch, tmp_path, rows=rows, knobs={"mode": "drop"})
    assert ids(resp) == ["old-done", "plain"]              # 완료 계열 행은 낡아도 빼지 않는다


@pytest.mark.parametrize("limit,expected_qs", [(1, ["#251"]), (2, ["#251", "#252"]), (8, ["#251", "#252", "#253"])])
def test_max_keys(monkeypatch, tmp_path, limit, expected_qs):
    resp, _, up = run(monkeypatch, tmp_path, rows=MULTI_ROWS, list_rows=MULTI_LIST, knobs={"max_keys": limit})
    assert sorted(up.list_qs()) == expected_qs
    by_id = {r["id"]: r for r in rows_of(resp)}
    assert (FIELD in by_id["open-252"]) is (limit >= 2)


NO_TAG_ROW = mrow("open-251-notag", text="[인계] acme/hindsight-crew#251 남은 일", tags=("kind:handoff",))  # 소유자 표기라 태그 없이도 키


@pytest.mark.parametrize("scope,rows,args,annotated", [
    ("row_or_request", [NO_TAG_ROW], {"tags": [PROJECT]}, True),        # 행에 project 없으면 요청 tags
    ("row_or_request", [NO_TAG_ROW], None, False),                     # 둘 다 없으면 조회 안 함
    ("row", [NO_TAG_ROW], {"tags": [PROJECT]}, False),
    ("row", BASE_ROWS, None, True),
    ("request", BASE_ROWS, None, False),                               # 요청 tags 만 본다
    ("request", BASE_ROWS, {"tags": [PROJECT]}, True),
])
def test_scope(monkeypatch, tmp_path, scope, rows, args, annotated):
    resp, _, up = run(monkeypatch, tmp_path, rows=rows, args=args, knobs={"scope": scope})
    assert (FIELD in rows_of(resp)[0]) is annotated
    assert bool(up.list_qs()) is annotated


ALIAS_POLICIES = BankPolicies.from_dict({"defaults": {}, "banks": {BANK: {
    "tag_prefixes": ["project", "kind", "topic", "user"],
    "item_repo_aliases": {"hc": "hindsight-crew"},
    "tag_aliases": {"kind:done": "kind:completion"}}}})


@pytest.mark.parametrize("use_alias,annotated", [(True, True), (False, False)])
def test_alias_table_knob(monkeypatch, tmp_path, use_alias, annotated):
    rows = [mrow("open-alias", text="[인계] hc#251 남은 일")]
    resp, _, _ = run(monkeypatch, tmp_path, rows=rows, bank_policies=ALIAS_POLICIES,
                     knobs={"use_alias_table": use_alias})
    row = rows_of(resp)[0]
    assert (row.get(FIELD) == [EVIDENCE_251]) is annotated


@pytest.mark.parametrize("policies,annotated", [(ALIAS_POLICIES, True), (None, False)])
def test_completion_family_follows_bank_tag_aliases(monkeypatch, tmp_path, policies, annotated):
    resp, _, _ = run(monkeypatch, tmp_path, bank_policies=policies,
                     list_rows=[lrow("done-old-kind", tags=("kind:done", PROJECT))])
    assert (FIELD in rows_of(resp)[0]) is annotated


# --------------------------------------------------------------------------
# (d) X1 조합
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["annotate", "demote", "drop"])
def test_x1_rows_untouched_when_both_on(monkeypatch, tmp_path, mode):
    x1_only, _, _ = run(monkeypatch, tmp_path / "x1", ledger=None, x1="1")
    assert sha(x1_only) == SHA_X1_ON
    both, _, _ = run(monkeypatch, tmp_path / "both", ledger="1", x1="1", knobs={"mode": mode})
    x1_rows = [r for r in rows_of(x1_only) if r.get(X1_MARK)]
    assert x1_rows and [r for r in rows_of(both) if r.get(X1_MARK)] == x1_rows   # X1 행 바이트 그대로
    assert rows_of(both)[-1] == rows_of(x1_only)[-1]                             # 위치도(끝)
    originals = [r for r in rows_of(both) if not r.get(X1_MARK)]
    if mode == "drop":
        assert [r["id"] for r in originals] == ["plain"]
    else:
        assert originals[-1 if mode == "demote" else 0][FIELD] == [EVIDENCE_251]


def test_x1_combo_skip_if_x1_changes_only_s1_output(monkeypatch, tmp_path):
    resp, log, _ = run(monkeypatch, tmp_path, ledger="1", x1="1", knobs={"x1_combo": "skip_if_x1"})
    assert sha(resp) == SHA_X1_ON                     # X1 이 이미 근거를 붙인 키는 S1 이 주석을 달지 않는다
    assert not any(r["decision"] == item_ledger.DECISION and r["reason"] == "annotated" for r in ledger_audit(log))


def test_x1_combo_skip_if_x1_still_annotates_keys_x1_did_not_cover(monkeypatch, tmp_path):
    rows = [*BASE_ROWS, mrow("done-251-new", text=BASE_LIST[1]["text"], tags=("kind:completion", PROJECT),
                             at="2026-10-15T10:00:00Z")]
    resp, _, _ = run(monkeypatch, tmp_path, rows=rows, list_rows=[BASE_LIST[1]], ledger="1", x1="1",
                     knobs={"x1_combo": "skip_if_x1"})
    assert not any(r.get(X1_MARK) for r in rows_of(resp))       # 근거가 이미 결과에 있어 X1 은 덧붙이지 않았다
    assert rows_of(resp)[0][FIELD] == [EVIDENCE_251]


# --------------------------------------------------------------------------
# (e) 손잡이 전달
# --------------------------------------------------------------------------

def write_config(path, data, *, mtime=None):
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if mtime is not None:
        os.utime(path, ns=(mtime, mtime))


def test_config_file_is_reloaded_on_mtime_change(monkeypatch, tmp_path):
    cfg = tmp_path / "item-ledger.json"
    monkeypatch.setenv(item_ledger.ENV_CONFIG, str(cfg))
    resp, log, _ = run(monkeypatch, tmp_path / "default")
    assert rows_of(resp)[0]["text"].startswith(DEFAULT_NOTE)       # 파일 없음 = 기본 손잡이
    assert ledger_audit(log)[0]["item_ledger"]["knobs_source"] == "default"
    write_config(cfg, {"note_template": "<<{key} done {date}>>"}, mtime=1_000_000_000_000_000_000)
    resp, log, _ = run(monkeypatch, tmp_path / "first")
    assert rows_of(resp)[0]["text"].startswith("<<hindsight-crew#251 done 2026-10-15>> ")
    assert ledger_audit(log)[0]["item_ledger"]["knobs_source"] == "file"
    write_config(cfg, {"note_template": "{{{key}|{evidence_id}}}", "note_position": "suffix"},
                 mtime=1_000_000_001_000_000_000)
    resp, _, _ = run(monkeypatch, tmp_path / "second")
    assert rows_of(resp)[0]["text"].endswith(" {hindsight-crew#251|done-251-new}")


def test_unchanged_file_is_not_reparsed(monkeypatch, tmp_path):
    cfg = tmp_path / "item-ledger.json"
    write_config(cfg, {"mode": "drop"}, mtime=1_000_000_000_000_000_000)
    monkeypatch.setenv(item_ledger.ENV_CONFIG, str(cfg))
    first = item_ledger.load_knobs()
    second = item_ledger.load_knobs()
    assert first is second and first.values["mode"] == "drop"


def test_env_knobs_take_precedence_over_file(monkeypatch, tmp_path):
    cfg = tmp_path / "item-ledger.json"
    write_config(cfg, {"mode": "drop"})
    monkeypatch.setenv(item_ledger.ENV_CONFIG, str(cfg))
    resp, log, _ = run(monkeypatch, tmp_path / "r", rows=MODE_ROWS, knobs={"mode": "annotate"})
    assert ids(resp) == [r["id"] for r in MODE_ROWS]
    assert ledger_audit(log)[0]["item_ledger"]["knobs_source"] == "env"


def test_empty_env_knobs_fall_back_to_file(monkeypatch, tmp_path):
    cfg = tmp_path / "item-ledger.json"
    write_config(cfg, {"mode": "drop"})
    monkeypatch.setenv(item_ledger.ENV_CONFIG, str(cfg))
    monkeypatch.setenv(item_ledger.ENV_KNOBS, "  ")                 # compose 기본값 `${HC_ITEM_LEDGER_KNOBS:-}` = 빈 값
    knobs = item_ledger.load_knobs()
    assert knobs.source == "file" and knobs.values["mode"] == "drop"


BAD_CONFIGS = [
    ("not-json", "{mode: drop", ["invalid-json"], {}),
    ("not-object", "[1, 2]", ["not-an-object"], {}),
    ("bad-mode", {"mode": "explode"}, ["mode"], {}),
    ("bad-max-keys", {"max_keys": 0, "mode": "drop"}, ["max_keys"], {"mode": "drop"}),
    ("bool-max-keys", {"max_keys": True}, ["max_keys"], {}),
    ("bad-template-field", {"note_template": "[{body}]"}, ["note_template"], {}),
    ("bad-template-format", {"note_template": "[{key"}, ["note_template"], {}),
    ("empty-template", {"note_template": ""}, ["note_template"], {}),
    ("bad-position", {"note_position": "middle"}, ["note_position"], {}),
    ("bad-scope", {"scope": "everything"}, ["scope"], {}),
    ("bad-time-rule", {"time_rule": "none"}, ["time_rule"], {}),
    ("bad-alias", {"use_alias_table": "yes"}, ["use_alias_table"], {}),
    ("bad-combo", {"x1_combo": "replace"}, ["x1_combo"], {}),
    ("bad-timeout", {"timeout_ms": 5}, ["timeout_ms"], {}),
    ("unknown-key", {"colour": "red", "mode": "demote"}, ["unknown:colour"], {"mode": "demote"}),
]


@pytest.mark.parametrize("source", ["env", "file"])
@pytest.mark.parametrize("case,content,warnings,kept", BAD_CONFIGS, ids=[c[0] for c in BAD_CONFIGS])
def test_invalid_config_falls_back_to_defaults_with_warning(monkeypatch, tmp_path, caplog, source, case, content,
                                                            warnings, kept):
    raw = content if isinstance(content, str) else json.dumps(content)
    if source == "env":
        monkeypatch.setenv(item_ledger.ENV_KNOBS, raw)
    else:
        cfg = tmp_path / "item-ledger.json"
        write_config(cfg, raw)
        monkeypatch.setenv(item_ledger.ENV_CONFIG, str(cfg))
    with caplog.at_level(logging.WARNING, logger=item_ledger.__name__):
        knobs = item_ledger.load_knobs()
    assert knobs.warnings == warnings
    assert knobs.values == {**item_ledger.DEFAULTS, **kept}       # 잘못된 키만 기본값(전체 무효면 전부 기본값)
    assert any(item_ledger.ENV_FLAG in rec.getMessage() for rec in caplog.records)


def test_invalid_config_warning_reaches_audit_and_request_still_annotated(monkeypatch, tmp_path):
    monkeypatch.setenv(item_ledger.ENV_KNOBS, '{"mode": "explode"}')
    set_env(monkeypatch, item_ledger.ENV_FLAG, "1")
    resp, log, _ = status_run(monkeypatch, tmp_path, flag=None)
    assert rows_of(resp)[0][FIELD] == [EVIDENCE_251]            # 기본값(표시만)으로 동작
    info = ledger_audit(log)[0]["item_ledger"]
    assert info["knob_warnings"] == ["mode"] and info["mode"] == "annotate"


def test_knobs_sha256_is_canonical(monkeypatch):
    monkeypatch.setenv(item_ledger.ENV_KNOBS, '{"mode":"drop"}')
    a = item_ledger.load_knobs().sha256
    item_ledger._reset_cache()
    monkeypatch.setenv(item_ledger.ENV_KNOBS, '{ "mode" : "drop" }')
    assert item_ledger.load_knobs().sha256 == a
    item_ledger._reset_cache()
    monkeypatch.setenv(item_ledger.ENV_KNOBS, '{"mode":"demote"}')
    assert item_ledger.load_knobs().sha256 != a


# --------------------------------------------------------------------------
# (f) fail-open
# --------------------------------------------------------------------------

@pytest.mark.parametrize("x1,expected,up_kwargs,knobs,reason", [
    (None, SHA_X1_OFF, {"list_delay": 0.5}, {"timeout_ms": 100}, "timeout"),
    (None, SHA_X1_OFF, {"list_status": 500}, {}, "lookup-failed:http-500"),
    (None, SHA_X1_OFF, {"list_raises": True}, {}, "lookup-failed:transport"),
    # X1 켜짐: upstream 오류는 X1 도 함께 fail-open 이라 S1 단독 확인이 안 된다. X1 상한은 넉넉히, S1 상한만 짧게 건다.
    ("1", SHA_X1_ON, {"list_delay": 0.5}, {"timeout_ms": 100}, "timeout"),
])
def test_failures_fail_open(monkeypatch, tmp_path, up_kwargs, knobs, reason, x1, expected):
    set_env(monkeypatch, item_status.ENV_TIMEOUT_MS, "5000" if x1 else None)
    resp, log, _ = run(monkeypatch, tmp_path, x1=x1, knobs=knobs, **up_kwargs)
    assert sha(resp) == expected
    (audit,) = ledger_audit(log)
    assert audit["reason"] == reason


def test_exception_inside_fails_open(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(item_ledger, "_annotate_payload", boom)
    resp, log, _ = run(monkeypatch, tmp_path)
    assert sha(resp) == SHA_X1_OFF
    assert ledger_audit(log)[0]["reason"] == "failed:RuntimeError"


# --------------------------------------------------------------------------
# (g) 연막: S1 추가 글자 수 < X1 덧붙임 글자 수
# --------------------------------------------------------------------------

def _uuid(n):
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def _smoke_rows():
    """키 3개, 키당 열림 행 2~3개. 행 모양은 MCP recall 행(본문 길이는 운영 행 규모를 흉내 낸 가짜 문장)."""
    filler = "작업 맥락 설명 문장이 이어진다. " * 6
    rows, lists = [], []
    for i, (number, count) in enumerate(((401, 3), (402, 2), (403, 2))):
        for j in range(count):
            rows.append({**mrow(_uuid(100 + 10 * i + j), text=f"[인계] hindsight-crew#{number} 남은 일 {j} {filler}",
                                at=f"2026-10-0{j + 1}T00:00:00Z", doc=_uuid(500 + 10 * i + j)),
                         "occurred_start": "2026-10-01T00:00:00Z", "occurred_end": "2026-10-01T00:00:00Z"})
        lists.append({**lrow(_uuid(900 + i), text=f"[완료] hindsight-crew#{number} 정리 끝 {filler}",
                             at="2026-10-15T10:00:00Z", doc=_uuid(950 + i)),
                      "occurred_start": "2026-10-15T10:00:00Z", "occurred_end": "2026-10-15T10:00:00Z"})
    return rows, lists


def test_smoke_s1_adds_fewer_characters_than_x1(monkeypatch, tmp_path):
    rows, lists = _smoke_rows()
    off, _, _ = run(monkeypatch, tmp_path / "off", ledger=None, rows=rows, list_rows=lists)
    x1, _, _ = run(monkeypatch, tmp_path / "x1", ledger=None, x1="1", rows=rows, list_rows=lists)
    s1, _, _ = run(monkeypatch, tmp_path / "s1", ledger="1", rows=rows, list_rows=lists)
    base = len(json.dumps(payload_of(off), ensure_ascii=False))
    x1_extra = len(json.dumps(payload_of(x1), ensure_ascii=False)) - base
    s1_extra = len(json.dumps(payload_of(s1), ensure_ascii=False)) - base
    assert sum(1 for r in rows_of(s1) if FIELD in r) == len(rows)       # 7행 전부 주석
    assert sum(1 for r in rows_of(x1) if r.get(X1_MARK)) == 3
    print(f"smoke chars: x1_extra={x1_extra} s1_extra={s1_extra}")
    assert 0 < s1_extra < x1_extra


# --------------------------------------------------------------------------
# final-review #363-1 재작업
# --------------------------------------------------------------------------

def test_permission_only_change_reloads_config(monkeypatch, tmp_path):
    """읽기 실패(권한 000)로 기본값이 된 뒤 chmod 만 고쳐도(mtime·크기 그대로) 다음 읽기에 반영된다(#363-1 #1)."""
    cfg = tmp_path / "item-ledger.json"
    write_config(cfg, {"mode": "drop"}, mtime=1_000_000_000_000_000_000)
    monkeypatch.setenv(item_ledger.ENV_CONFIG, str(cfg))
    os.chmod(cfg, 0)
    try:
        first = item_ledger.load_knobs()
        assert first.warnings == ["unreadable"] and first.values["mode"] == "annotate"
    finally:
        os.chmod(cfg, 0o644)
    second = item_ledger.load_knobs()
    assert second.source == "file" and second.values["mode"] == "drop" and second.warnings == []


def test_same_size_same_mtime_edit_reloads_config(monkeypatch, tmp_path):
    """크기와 mtime 이 같은 편집(같은 틱, mtime 보존 복사)도 다시 읽는다(#363-1 #5)."""
    cfg = tmp_path / "item-ledger.json"
    mtime = 1_000_000_000_000_000_000
    write_config(cfg, '{"note_position":"prefix"}', mtime=mtime)
    monkeypatch.setenv(item_ledger.ENV_CONFIG, str(cfg))
    assert item_ledger.load_knobs().values["note_position"] == "prefix"
    write_config(cfg, '{"note_position":"suffix"}', mtime=mtime)       # 같은 6자, 같은 mtime
    assert item_ledger.load_knobs().values["note_position"] == "suffix"


PARTLY_DONE_ROWS = [
    mrow("one-open", text="[인계] hindsight-crew#251 과 hindsight-crew#253 남은 일", at="2026-10-01T00:00:00Z"),
    mrow("both-done", text="[인계] hindsight-crew#251 과 hindsight-crew#252 남은 일", at="2026-10-01T00:00:00Z"),
    mrow("plain", text="[발견] 키 없는 기억", tags=("kind:gotcha", PROJECT)),
]


@pytest.mark.parametrize("mode,expected_ids", [
    ("demote", ["one-open", "plain", "both-done"]),
    ("drop", ["one-open", "plain"]),
])
def test_row_with_a_still_open_key_is_not_demoted_or_dropped(monkeypatch, tmp_path, mode, expected_ids):
    """낡음 = 행이 말하는 키가 모두 행보다 새 완료 근거를 가질 때. 열린 키(#253)를 함께 말하는 행은 주석만(#363-1 #2)."""
    resp, _, _ = run(monkeypatch, tmp_path, rows=PARTLY_DONE_ROWS, list_rows=MULTI_LIST, knobs={"mode": mode})
    by_id = {r["id"]: r for r in rows_of(resp)}
    assert [r["id"] for r in rows_of(resp)] == expected_ids
    assert by_id["one-open"][FIELD] == [EVIDENCE_251]


def test_skip_if_x1_extracts_x1_row_keys_with_bank_aliases(monkeypatch, tmp_path):
    """X1 행 키는 X1 과 같은 뱅크 별칭 정규화로 뽑는다(use_alias_table 과 무관, #363-1 #6)."""
    rows = [mrow("open-251", text="[인계] hindsight-crew#251 남은 일")]
    lists = [lrow("done-alias", text="[완료] hc#251 끝", at="2026-10-15T10:00:00Z"),
             lrow("done-full", text="[완료] hindsight-crew#251 정리 끝", at="2026-10-12T10:00:00Z")]
    resp, _, _ = run(monkeypatch, tmp_path, rows=rows, list_rows=lists, ledger="1", x1="1",
                     bank_policies=ALIAS_POLICIES, knobs={"x1_combo": "skip_if_x1", "use_alias_table": False})
    out = rows_of(resp)
    assert [r["id"] for r in out if r.get(X1_MARK)] == ["done-alias"]     # X1 은 별칭 표기 행을 덧붙였다
    assert FIELD not in out[0]                                              # 같은 키라 S1 주석 생략
