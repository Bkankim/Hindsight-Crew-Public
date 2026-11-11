"""S3 3상태 표 (issue #396, 런 7 R7-2, 플래그 HC_ITEM_OPEN_TABLE 기본 꺼짐). 네트워크 0, 임시 sqlite 픽스처.

고정하는 계약(코디네이터 결정 A: GitHub 근거 테이블이 있고 손잡이 github_evidence=use 일 때만 3상태 모드,
테이블 없음·ignore 면 지금 줄·payload 모양 그대로):
  (a) 상태 판정 행렬: 뱅크 열림/끝남 x GitHub 근거 9종 x 근거 시각 t 전/후 -> 줄 상태·사유(fmt b 줄).
  (b) 기준일 뒤 생성된 번호, 열린 PR, 종결 시각 없는 종결 근거 -> 미확인·충돌 사유.
  (c) 잘림: 열림 후보를 먼저 남기고(날짜 최신순) 잘림 줄에 상태별 생략 수.
  (d) 머리: 표 밖 키 금지 지시 없음, 옛 기본 머리는 HEADER_C7, 새 기본 손잡이 정규 sha256 고정,
      github_evidence 는 DEFAULTS 밖 선택 키(정규 sha256 에 안 들어감).
  (e) 범위 순서: v2 저장소에서 v1 결과 키가 없으면 저장소 미개봉, v1 키는 있는데 v2 키가 없으면 연 뒤 미부착.
  (f) github_evidence=ignore 면 근거 테이블이 있어도 테이블 없는 표와 같다.
  (g) role_states v2 동점(서로 다른 기억이 같은 시각에서 갈림) = 끝남, v1 은 열림 그대로.
  (h) payload `keys` = 열림 후보만, `counts`, 감사 줄 상태별 수·미확인 사유별 수·key_rules·근거 최신 fetched_at.
"""
import hashlib
import json
import re
import sqlite3

import pytest

from gateway import item_open_table as s3
from test_item_open_table import (_s3_env, make_store, opens, run, s3_audit, table_of,  # noqa: F401
                                  DEFAULTS_SHA256_R6, HEADER_C7_SHA256, KEYLESS_ROWS, QTS)
from test_item_status import mrow


@pytest.fixture(autouse=True)
def _pin_wall_clock(monkeypatch):
    """#402 근거 나이 상한: 읽는 시각을 픽스처 근거 fetched_at(2026-11-01) 1시간 뒤로 고정한다(상한 2시간 안)."""
    monkeypatch.setattr(s3, "_wall_clock", lambda: 1793494800.0)                 # 2026-11-01T01:00:00Z

DB_NAME = ("store", "item-roles.sqlite")
REPO = "hindsight-crew"
BEFORE, AFTER = "2026-10-10T00:00:00Z", "2026-10-25T00:00:00Z"      # 기준 시각 QTS(2026-10-20 23:59:59 KST) 전/후
CREATED = "2026-09-01T00:00:00Z"
FETCHED = "2026-11-01T00:00:00Z"
OPEN_KEY, DONE_KEY = f"{REPO}#20", f"{REPO}#21"
BANK_MEMS = [
    ("g1", "2026-10-01T01:00:00Z", "valid", {OPEN_KEY: "open", DONE_KEY: "open"}),
    ("g2", "2026-10-05T01:00:00Z", "valid", {DONE_KEY: "done"}),
]


def _path(tmp_path):
    return tmp_path.joinpath(*DB_NAME)


def add_github(path, *, repos=None, items=None, meta=None):
    """기존 저장소 파일에 #395 GitHub 근거 테이블(과 meta 추가 키)을 더한다."""
    from gateway import item_roles
    conn = sqlite3.connect(path)
    if meta:
        conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", list(meta.items()))
    if repos is not None or items is not None:
        item_roles._write_github(conn, {"repos": repos or {}, "items": items or {}})
    conn.commit()
    conn.close()


def ok_repo(fetched=FETCHED):
    return {REPO: {"owner": "o", "status": "ok", "fetched_at": fetched, "error_code": None}}


def item(kind, state, reason=None, closed=None, merged=None, created=CREATED, fetched=FETCHED):
    return {"kind": kind, "state": state, "state_reason": reason, "created_at": created, "closed_at": closed,
            "merged_at": merged, "fetched_at": fetched}


def evidence(kind, when):
    """GitHub 근거 종류 -> (repos, items) for 번호 20·21. when = 종결 시각(t 전/후)."""
    def both(row):
        return ok_repo(), {(REPO, 20): row, (REPO, 21): row}
    if kind == "completed":
        return both(item("issue", "closed", "completed", closed=when))
    if kind == "merged-pr":
        return both(item("pr", "closed", None, closed=when, merged=when))
    if kind == "unmerged-pr":
        return both(item("pr", "closed", None, closed=when))
    if kind == "not-planned":
        return both(item("issue", "closed", "not_planned", closed=when))
    if kind == "reopened":
        return both(item("issue", "open", "reopened"))
    if kind == "repo-missing":
        return {REPO: {"owner": "o", "status": "missing", "fetched_at": FETCHED, "error_code": "404"}}, {}
    if kind == "repo-error":
        return {REPO: {"owner": "o", "status": "error", "fetched_at": FETCHED, "error_code": "http"}}, {}
    if kind == "no-number":
        return ok_repo(), {}
    if kind == "no-table":
        return None, None
    raise AssertionError(kind)


def row_of(text, key):
    """표 text 에서 키 줄 -> 키 뒤 칸들(없으면 None)."""
    for line in text.splitlines():
        cells = line.split(" | ")
        if cells[0] == key:
            return tuple(cells[1:])
    return None


# 기대 줄(fmt b). 열림 후보 = (상태, 마지막 열림 언급 KST 날짜), 확인된 종결·종결(미계획) = (상태, 출처, 종결 날짜),
# 미확인·충돌 = (상태, 사유 코드), 테이블 없음 = 지금 모양(열림, 날짜). None = 줄 없음.
C_DAY, O_DAY = "2026-10-10", "2026-10-01"
OPEN_ROW = ("열림 후보", O_DAY)
EXPECTED = {
    # (뱅크, 근거, 시각): 기대 줄
    ("open", "completed", "before"): ("확인된 종결", "github", C_DAY),
    ("open", "completed", "after"): OPEN_ROW,                       # t 뒤 종결은 쓰지 않는다(그때는 열림)
    ("open", "merged-pr", "before"): ("확인된 종결", "github", C_DAY),
    ("open", "merged-pr", "after"): ("미확인·충돌", "pr-open"),     # t 시점 열린 PR
    ("open", "unmerged-pr", "before"): ("종결(미계획)", "github", C_DAY),
    ("open", "unmerged-pr", "after"): ("미확인·충돌", "pr-open"),
    ("open", "not-planned", "before"): ("종결(미계획)", "github", C_DAY),
    ("open", "not-planned", "after"): OPEN_ROW,
    ("open", "reopened", "before"): OPEN_ROW,
    ("open", "reopened", "after"): OPEN_ROW,
    ("open", "repo-missing", "before"): ("미확인·충돌", "repo-missing"),
    ("open", "repo-missing", "after"): ("미확인·충돌", "repo-missing"),
    ("open", "repo-error", "before"): ("미확인·충돌", "repo-error"),
    ("open", "repo-error", "after"): ("미확인·충돌", "repo-error"),
    ("open", "no-number", "before"): ("미확인·충돌", "no-number"),
    ("open", "no-number", "after"): ("미확인·충돌", "no-number"),
    ("open", "no-table", "before"): ("열림", O_DAY),                 # 결정 A: 테이블 없으면 지금 모양
    ("open", "no-table", "after"): ("열림", O_DAY),
    ("done", "completed", "before"): None,
    ("done", "completed", "after"): ("미확인·충돌", "done-but-open"),
    ("done", "merged-pr", "before"): None,
    ("done", "merged-pr", "after"): ("미확인·충돌", "done-but-open"),
    ("done", "unmerged-pr", "before"): None,
    ("done", "unmerged-pr", "after"): ("미확인·충돌", "done-but-open"),
    ("done", "not-planned", "before"): None,
    ("done", "not-planned", "after"): ("미확인·충돌", "done-but-open"),
    ("done", "reopened", "before"): ("미확인·충돌", "done-but-open"),
    ("done", "reopened", "after"): ("미확인·충돌", "done-but-open"),
    ("done", "repo-missing", "before"): None,                       # 미확인은 끝남과 충돌하지 않는다
    ("done", "repo-missing", "after"): None,
    ("done", "repo-error", "before"): None,
    ("done", "repo-error", "after"): None,
    ("done", "no-number", "before"): None,
    ("done", "no-number", "after"): None,
    ("done", "no-table", "before"): None,
    ("done", "no-table", "after"): None,
}
KINDS = ["completed", "merged-pr", "unmerged-pr", "not-planned", "reopened", "repo-missing", "repo-error",
         "no-number", "no-table"]


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("bank", ["open", "done"])
def test_state_matrix(monkeypatch, tmp_path, bank, kind, when):
    path = make_store(_path(tmp_path), BANK_MEMS)
    repos, items = evidence(kind, BEFORE if when == "before" else AFTER)
    add_github(path, repos=repos, items=items)
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": "b"})
    table = table_of(resp)
    key = OPEN_KEY if bank == "open" else DONE_KEY
    expected = EXPECTED[(bank, kind, when)]
    assert row_of(table["text"], key) == expected
    assert (key in table["keys"]) is (expected is not None and expected[0] in ("열림 후보", "열림"))
    if kind == "no-table":
        assert "counts" not in table                                  # 결정 A: 지금 payload 모양
    else:
        assert sum(table.get("counts", {}).values()) == len([k for k in (OPEN_KEY, DONE_KEY)
                                                     if row_of(table["text"], k) is not None])


@pytest.mark.parametrize("fmt,line", [
    ("a", f"{OPEN_KEY} | 확인된 종결 | {C_DAY}"),                  # fmt a: 키·상태, 확인된 종결만 날짜
    ("b", f"{OPEN_KEY} | 확인된 종결 | github | {C_DAY}"),
])
def test_confirmed_closed_row_format(monkeypatch, tmp_path, fmt, line):
    path = make_store(_path(tmp_path), BANK_MEMS)
    repos, items = evidence("completed", BEFORE)
    add_github(path, repos=repos, items=items)
    resp, _, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": fmt})
    assert line in table_of(resp)["text"].splitlines()


@pytest.mark.parametrize("fmt,line", [("a", f"{OPEN_KEY} | 미확인·충돌 | no-number"), ("b", f"{OPEN_KEY} | 미확인·충돌 | no-number"),
                                      ("a", f"{OPEN_KEY} | 열림 후보 | {O_DAY}")])
def test_fmt_a_rows_are_key_and_state(monkeypatch, tmp_path, fmt, line):
    path = make_store(_path(tmp_path), BANK_MEMS)
    items = {} if "미확인" in line else {(REPO, 20): item("issue", "open")}
    add_github(path, repos=ok_repo(), items=items)
    resp, _, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": fmt})
    assert line in table_of(resp)["text"].splitlines()


@pytest.mark.parametrize("row,reason", [
    (item("issue", "open", created="2026-10-25T00:00:00Z"), "no-number"),     # 기준일 뒤 생성 = 그때 없던 번호
    (item("issue", "closed", "completed", closed=BEFORE, created="2026-10-21T00:00:00Z"), "no-number"),
    (item("pr", "open"), "pr-open"),
    (item("issue", "closed", "completed", closed=None), "no-closed-at"),
    (item("issue", "closed", "weird"), "unknown-reason"),
])
def test_unknown_reasons(monkeypatch, tmp_path, row, reason):
    path = make_store(_path(tmp_path), BANK_MEMS)
    add_github(path, repos=ok_repo(), items={(REPO, 20): row})
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": "b"})
    table = table_of(resp)
    assert row_of(table["text"], OPEN_KEY) == ("미확인·충돌", reason)
    assert OPEN_KEY not in table["keys"]
    info = s3_audit(log)[0]["item_open_table"]
    assert info["unknown_reasons"] == {reason: 1}


def test_payload_and_audit_fields(monkeypatch, tmp_path):
    mems = [("g1", "2026-10-01T01:00:00Z", "valid", {f"{REPO}#{n}": "open" for n in (1, 2, 3, 4, 5)})]
    path = make_store(_path(tmp_path), mems)
    items = {(REPO, 1): item("issue", "open"),
             (REPO, 2): item("issue", "closed", "completed", closed=BEFORE),
             (REPO, 3): item("issue", "closed", "duplicate", closed=BEFORE),
             (REPO, 4): item("pr", "open")}                          # 5 = 번호 없음
    add_github(path, repos=ok_repo("2026-11-01T00:30:00Z"), items=items, meta={"key_rules": "v2"})
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"over_max_rows": "keep"})      # #410: #396 감사 고정
    table = table_of(resp)
    assert table["keys"] == [f"{REPO}#1"]
    assert table["counts"] == {"열림 후보": 1, "확인된 종결": 1, "종결(미계획)": 1, "미확인·충돌": 2}
    assert list(table)[:5] == ["t", "fmt", "keys", "truncated", "text"]
    (audit,) = s3_audit(log)
    info = audit["item_open_table"]
    assert audit["reason"] == "attached"
    assert info["counts"] == table["counts"]
    assert info["unknown_reasons"] == {"no-number": 1, "pr-open": 1}
    assert info["key_rules"] == "v2"
    assert info["github_fetched_at"] == "2026-11-01T00:30:00Z"
    assert info["github_evidence"] == "use"


def test_truncation_keeps_open_candidates_first(monkeypatch, tmp_path):
    # 1..3 열림 후보(날짜 10-01..10-03), 4..6 확인된 종결(뱅크 날짜는 더 최신 10-11..10-13), 7 미확인
    mems = [(f"k{n}", f"2026-10-{(n if n <= 3 else n + 7):02d}T01:00:00Z", "valid", {f"{REPO}#{n}": "open"})
            for n in range(1, 8)]
    path = make_store(_path(tmp_path), mems)
    items = {(REPO, n): item("issue", "open") for n in (1, 2, 3)}
    items.update({(REPO, n): item("issue", "closed", "completed", closed=BEFORE) for n in (4, 5, 6)})
    add_github(path, repos=ok_repo(), items=items)
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"max_rows": 2, "over_max_rows": "keep"})     # #410: #396 잘림 고정
    table = table_of(resp)
    assert table["keys"] == [f"{REPO}#2", f"{REPO}#3"]              # 열림 후보 안에서 날짜 최신순
    assert table["truncated"] == 5
    assert table["counts"] == {"열림 후보": 2}
    last = table["text"].splitlines()[-2]
    assert last == "(상한 2줄: 생략 열림 후보 1개, 확인된 종결 3개, 미확인·충돌 1개)"
    assert s3_audit(log)[0]["item_open_table"]["truncated"] == 5


def test_truncation_fills_with_other_states_after_open_candidates(monkeypatch, tmp_path):
    mems = [(f"k{n}", f"2026-10-0{n}T01:00:00Z", "valid", {f"{REPO}#{n}": "open"}) for n in (1, 2, 3)]
    path = make_store(_path(tmp_path), mems)
    add_github(path, repos=ok_repo(), items={(REPO, 1): item("issue", "open")})
    resp, _, _ = run(monkeypatch, tmp_path, knobs={"max_rows": 2, "table_fmt": "b", "over_max_rows": "keep"})  # #410
    table = table_of(resp)
    assert table["keys"] == [f"{REPO}#1"]
    assert row_of(table["text"], f"{REPO}#3") == ("미확인·충돌", "no-number")      # 남은 칸은 날짜 최신순
    assert row_of(table["text"], f"{REPO}#2") is None
    assert table["text"].splitlines()[-2] == "(상한 2줄: 생략 미확인·충돌 1개)"


# --------------------------------------------------------------------------
# (d) 머리·손잡이
# --------------------------------------------------------------------------

C7_TEXT = ("기준일 {t} 현재 뱅크 기억 기준 열린 작업 키다(기준일 이전 기억만). "
           "여기 없는 키는 뱅크 기억 기준 열림이 아니거나 질의 범위 밖이다. "
           "이 표에 없는 작업 키는 열린 목록에 넣지 마라.\n줄: {cols}")
FORBID_RE = re.compile(r"(넣지|포함하지|올리지|쓰지|추가하지)\s*(마|말)|표에\s*없는")
# 새 기본 손잡이(새 기본 머리 + 나머지 R6 기본값)의 정규 sha256.
DEFAULTS_SHA256_R7_2 = "3df18d9421c93380677fb80b67add04b13797c368ef21552de7e9f3f6029b61b"
HEADER_R7_2_SHA256 = "1bb66704f417d47b753a2c0814b74f4086e6a04599af6261a2c70793e5387569"


def test_header_constants():
    assert getattr(s3, "HEADER_C7", None) == C7_TEXT
    assert hashlib.sha256(C7_TEXT.encode("utf-8")).hexdigest() == HEADER_C7_SHA256
    assert s3.DEFAULTS["header"] == s3.HEADER_DEFAULT != C7_TEXT
    assert hashlib.sha256(s3.HEADER_DEFAULT.encode("utf-8")).hexdigest() == HEADER_R7_2_SHA256
    assert not FORBID_RE.search(s3.HEADER_DEFAULT)
    assert "후보" in s3.HEADER_DEFAULT and "끝났을 수 있" in s3.HEADER_DEFAULT
    for legend in getattr(s3, "LEGEND", {"a": "넣지 마라"}).values():
        assert not FORBID_RE.search(legend)
    assert s3._valid_header(s3.HEADER_DEFAULT)
    assert s3.Knobs(s3._defaults(), "default").sha256 == DEFAULTS_SHA256_R7_2
    # 런 6 R6-1 재현값(bench/r6_endtoend.py knobs): 머리 HEADER_R6_1 명시 + 나머지 기본값
    assert s3.Knobs({**s3._defaults(), "header": s3.HEADER_R6_1}, "default").sha256 == DEFAULTS_SHA256_R6


@pytest.mark.parametrize("value", ["use", "ignore"])
def test_github_evidence_is_an_optional_knob(value):
    assert "github_evidence" not in s3.DEFAULTS and "github_evidence" not in s3._defaults()
    values, warnings = s3.parse_knobs(json.dumps({"github_evidence": value}))
    assert warnings == [] and values["github_evidence"] == value
    values, warnings = s3.parse_knobs(json.dumps({"github_evidence": "x"}))
    assert warnings == ["github_evidence"] and "github_evidence" not in values


def test_ignore_knob_matches_no_table(monkeypatch, tmp_path):
    no_table = make_store(tmp_path / "a" / "item-roles.sqlite", BANK_MEMS)
    monkeypatch.setenv(s3.ENV_DB, str(no_table))
    plain, _, _ = run(monkeypatch, tmp_path / "plain", knobs={"table_fmt": "b"})
    path = make_store(_path(tmp_path), BANK_MEMS)
    repos, items = evidence("completed", BEFORE)
    add_github(path, repos=repos, items=items)
    monkeypatch.setenv(s3.ENV_DB, str(path))
    resp, log, _ = run(monkeypatch, tmp_path / "ign", knobs={"table_fmt": "b", "github_evidence": "ignore"})
    assert table_of(resp) == table_of(plain)
    info = s3_audit(log)[0]["item_open_table"]
    assert "knob_warnings" not in info and info["github_evidence"] == "ignore"
    assert "counts" not in info


# --------------------------------------------------------------------------
# (e) 범위 순서 (v2 저장소)
# --------------------------------------------------------------------------

V1_ONLY_ROWS = [mrow("pr", text="[인계] PR #3 머지 대기", tags=("kind:handoff", "project:other-repo"))]
V2_ROWS = [mrow("bare", text="[인계] #3 남은 일", tags=("kind:handoff", "project:other-repo"))]
UPSTREAM_ROWS = [mrow("up", text="[인계] upstream #3 확인", tags=("kind:handoff", "project:other-repo"))]
SCOPE_MEMS = [("s1", "2026-10-07T01:00:00Z", "valid", {"other-repo#3": "open"})]


@pytest.mark.parametrize("key_rules,rows,expect", [
    ("v2", KEYLESS_ROWS, ("no-store-open", "no-result-keys")),
    ("v2", V1_ONLY_ROWS, ("opened", "no-result-keys-v2")),         # v1 키는 있는데 v2 키 없음 -> 연 뒤 미부착
    ("v2", UPSTREAM_ROWS, ("opened", "no-result-keys-v2")),        # 업스트림 문맥 = 참조 고정, 범위로 치지 않는다
    ("v2", V2_ROWS, ("attached", None)),
    ("v1", V1_ONLY_ROWS, ("attached", None)),                      # v1 저장소는 지금 범위 그대로
    (None, V1_ONLY_ROWS, ("attached", None)),
])
def test_scope_order_for_v2_store(monkeypatch, tmp_path, opens, key_rules, rows, expect):
    path = make_store(_path(tmp_path), SCOPE_MEMS)
    if key_rules:
        add_github(path, meta={"key_rules": key_rules})
    resp, log, _ = run(monkeypatch, tmp_path, args={"tags": None}, rows=rows)
    (audit,) = s3_audit(log)
    info = audit["item_open_table"]
    what, why = expect
    if what == "attached":
        assert audit["reason"] == "attached" and table_of(resp)["keys"] == ["other-repo#3"]
        assert len(opens) == 1
    else:
        assert audit["reason"] == "not-attached" and info["not_attached"] == why
        assert table_of(resp) is None
        assert len(opens) == (1 if what == "opened" else 0)


def test_scope_v2_public_functions():
    assert s3.result_repos_v2(V1_ONLY_ROWS, {}) == set()
    assert s3.result_repos_v2(UPSTREAM_ROWS, {}) == set()
    assert s3.result_repos_v2(V2_ROWS, {}) == {"other-repo"}
    knobs = s3._defaults()
    assert s3.table_scope_v2({"tags": None}, V1_ONLY_ROWS, knobs, {}, "v1") == ({"other-repo"}, "result")
    assert s3.table_scope_v2({"tags": None}, V1_ONLY_ROWS, knobs, {}, "v2") == (None, "no-result-keys-v2")
    assert s3.table_scope_v2({"tags": None}, V2_ROWS, knobs, {}, "v2") == ({"other-repo"}, "result")
    assert s3.table_scope_v2({"tags": None}, KEYLESS_ROWS, knobs, {}, "v2") == (None, "no-result-keys")
    assert s3.table_scope_v2({"tags": ["project:z"]}, V1_ONLY_ROWS, knobs, {}, "v2") == ({"z"}, "request")


# --------------------------------------------------------------------------
# (g) role_states 동점 규칙
# --------------------------------------------------------------------------

TIE_MEMS = [("t1", "2026-10-02T01:00:00Z", "valid", {OPEN_KEY: "done"}),
            ("t2", "2026-10-02T01:00:00Z", "valid", {OPEN_KEY: "open"})]


@pytest.mark.parametrize("key_rules,is_open", [("v2", False), ("v1", True), (None, True)])
def test_tie_rule_by_key_rules(monkeypatch, tmp_path, key_rules, is_open):
    mems = TIE_MEMS + [("t3", "2026-10-01T01:00:00Z", "valid", {f"{REPO}#99": "open"})]
    path = make_store(_path(tmp_path), mems)
    if key_rules:
        add_github(path, meta={"key_rules": key_rules})
    resp, _, _ = run(monkeypatch, tmp_path)
    assert (OPEN_KEY in table_of(resp)["keys"]) is is_open


@pytest.mark.parametrize("order", [0, 1])
def test_role_states_v2_tie_pure(order):
    from datetime import datetime, timezone
    t = datetime(2026, 10, 2, tzinfo=timezone.utc)
    ms = [s3.Mention("r#1", "done", t, "a"), s3.Mention("r#1", "open", t, "b")]
    ms = ms if order == 0 else ms[::-1]
    cut = datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert s3.role_states(ms, cut)["r#1"].state == s3.STATE_OPEN
    assert s3.role_states(ms, cut, key_rules=None)["r#1"].state == s3.STATE_OPEN
    assert s3.role_states(ms, cut, key_rules="v2")["r#1"].state == s3.STATE_DONE


def test_build_table_four_args_keeps_rows_shape():
    """동결 벤치(bench/r5_*·r6_*)와 bench/r7_eval.py 의 4인자 호출: 줄 모양 [키, 상태, 날짜], 상태 열림."""
    from datetime import datetime, timezone
    cut = datetime(2026, 10, 20, 14, 59, 59, tzinfo=timezone.utc)
    states = {"a#1": s3.KeyState(s3.STATE_OPEN, cut.replace(day=1))}
    table = s3.build_table(states, cut, None, s3._defaults())
    assert table["rows"] == [["a#1", "열림", "2026-10-01"]] and "counts" not in table


# --------------------------------------------------------------------------
# (i) rework #396-1: 근거 테이블 읽기 범위 - ignore 면 읽지 않고, use 면 범위 안 키의 행만 읽는다
# --------------------------------------------------------------------------

@pytest.fixture
def sql_log(monkeypatch):
    """저장소 연결에서 실행된 SQL 문 목록(sqlite trace)."""
    stmts = []
    real = s3.connect_ro

    def tracing(path):
        conn = real(path)
        conn.set_trace_callback(stmts.append)
        return conn
    monkeypatch.setattr(s3, "connect_ro", tracing)
    return stmts


def _gh_stmts(stmts):
    return [s for s in stmts if "github_" in s and "sqlite_master" not in s]


def _wide_store(tmp_path, extra_repos=30, per_repo=40):
    """범위 안(hindsight-crew) 근거 + 범위 밖 리포 근거를 많이 넣은 저장소."""
    path = make_store(_path(tmp_path), BANK_MEMS + [("w1", "2026-10-03T01:00:00Z", "valid", {"other-repo#3": "open"})])
    repos = dict(ok_repo())
    items = {(REPO, 20): item("issue", "closed", "completed", closed=BEFORE), (REPO, 21): item("issue", "open"),
             (REPO, 999): item("issue", "open")}                                   # 999 = 뱅크에 없는 번호
    for r in range(extra_repos):
        name = f"far-repo-{r}"
        repos[name] = {"owner": "o", "status": "ok", "fetched_at": "2026-11-03T00:00:00Z", "error_code": None}
        items.update({(name, n): item("issue", "open") for n in range(1, per_repo + 1)})
    repos["other-repo"] = {"owner": "o", "status": "ok", "fetched_at": FETCHED, "error_code": None}
    items[("other-repo", 3)] = item("issue", "open")
    add_github(path, repos=repos, items=items)
    return path


def test_ignore_does_not_read_github_tables(monkeypatch, tmp_path, sql_log):
    _wide_store(tmp_path)
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": "b", "github_evidence": "ignore"})
    assert table_of(resp)["keys"] == [OPEN_KEY]                        # 지금 모양(열림)
    assert _gh_stmts(sql_log) == []                                    # 근거 테이블 조회 0


def test_use_reads_only_in_scope_rows_and_keeps_result(monkeypatch, tmp_path, sql_log):
    path = _wide_store(tmp_path)
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": "b"})
    table = table_of(resp)
    stmts = _gh_stmts(sql_log)                                         # 게이트웨이 경로의 근거 조회만(아래 기준 읽기 전)
    # 기준: 저장소 전체 근거로 만든 표(범위 밖 행을 읽어도 결과는 같아야 한다)
    full = s3.read_store(str(path), 5.0)
    from gateway import item_roles
    ref_github = item_roles.load_github(str(path))
    cut = s3.item_status._instant(QTS)
    ref = s3.build_table(s3.role_states(full.mentions, cut), cut, {REPO}, {**s3._defaults(), "table_fmt": "b"},
                         github=ref_github)
    assert table["text"] == ref["text"] and table["counts"] == ref["counts"]
    assert stmts and all("WHERE" in s for s in stmts)                  # 근거 조회는 모두 걸러서 읽는다
    joined = "\n".join(stmts)
    assert "far-repo" not in joined and "other-repo" not in joined     # 범위 밖 리포는 묻지 않는다


def test_read_store_github_scope_shapes(tmp_path):
    path = str(_wide_store(tmp_path))
    none = s3.read_store(path, 5.0, github_repos=None)
    assert none.github is None
    some = s3.read_store(path, 5.0, github_repos={REPO})
    assert set(some.github["repos"]) == {REPO}
    assert set(some.github["items"]) == {(REPO, 20), (REPO, 21)}       # 뱅크 언급 키만(999 는 안 읽음)
    every = s3.read_store(path, 5.0)                                   # 기본(4인자 호환): 언급된 키 전체 리포
    assert set(every.github["items"]) == {(REPO, 20), (REPO, 21), ("other-repo", 3)}
    bare = make_store(tmp_path / "bare" / "item-roles.sqlite", BANK_MEMS)
    assert s3.read_store(str(bare), 5.0, github_repos={REPO}).github is None   # 테이블 없음 = None
