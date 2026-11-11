"""S3 3상태 표 결함 수정 (issue #402, 런 7 R7-2b, 플래그 HC_ITEM_OPEN_TABLE 기본 꺼짐). 네트워크 0, 임시 sqlite 픽스처.

고정하는 계약(3상태 모드 = 저장소에 GitHub 근거 테이블이 있고 github_evidence=use 일 때만 바뀐다):
  (a) 결과 행 끝남 키 반증 줄: 뱅크 끝남/열림 x 결과 행에 있음/없음 x GitHub 근거 x 손잡이 done_closed_rows
      result/off -> 줄 상태(또는 줄 없음). 반증 줄 = `확인된 종결`, 종결 시각 <= t 인 종결 근거만.
  (b) 기본 형식 a: 열림 후보 = 키 | 상태 | 마지막 열림 언급 날짜, 확인된 종결 = 키 | 상태 | 종결 날짜,
      종결(미계획) = 키 | 상태, 미확인·충돌 = 키 | 상태 | 사유 코드. 열 설명·범례에 사유 코드.
      테이블 없음·ignore 렌더와 4인자 build_table 은 지금과 바이트 동일.
  (c) 근거 나이 상한: 리포 행 fetched_at(없으면 그 리포 항목 행 최댓값)이 읽는 시각(주입 벽시계)보다
      github_max_age_hours(기본 2, 0 = 검사 안 함) 넘게 오래되면 그 리포 줄은 전부 미확인·충돌 stale.
  (d) 새 손잡이 둘은 DEFAULTS 밖 선택 키(정규 sha256 불변), 감사 줄에 두 값(없으면 기본값).
"""
import hashlib
import json
from datetime import datetime, timezone

import pytest

from gateway import item_open_table as s3
from test_item_open_table import (_s3_env, make_store, run, s3_audit, table_of,  # noqa: F401
                                  DEFAULTS_SHA256_R6, PROJECT)
from test_item_open_table_github import (BANK_MEMS, BEFORE, AFTER, C_DAY, O_DAY, DONE_KEY, OPEN_KEY, REPO,
                                         DEFAULTS_SHA256_R7_2, _path, add_github, item, ok_repo, row_of)
from test_item_status import mrow

FETCHED_EPOCH = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()     # 픽스처 근거 fetched_at
HOUR = 3600.0
D_DAY = "2026-10-05"                    # DONE_KEY 뱅크 끝남 날짜(KST)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """읽는 시각(벽시계) 주입. 기본은 근거를 받은 지 1시간 뒤(상한 2시간 안)."""
    now = {"t": FETCHED_EPOCH + HOUR}
    monkeypatch.setattr(s3, "_wall_clock", lambda: now["t"], raising=False)
    return now


def rows_with(*nums, upstream=False):
    """recall 결과 행: 번호마다 한 행(요청 project 태그로 bare 번호가 hindsight-crew 키가 된다)."""
    word = "upstream " if upstream else ""
    return [mrow(f"r{n}", text=f"[인계] {word}#{n} 확인", tags=("kind:handoff", PROJECT)) for n in nums] or \
        [mrow("r0", text="[인계] 키 없는 행", tags=("kind:handoff", PROJECT))]


def both(row):
    return ok_repo(), {(REPO, 20): row, (REPO, 21): row}


GH = {
    "completed": lambda: both(item("issue", "closed", "completed", closed=BEFORE)),
    "merged-pr": lambda: both(item("pr", "closed", None, closed=BEFORE, merged=BEFORE)),
    "not-planned": lambda: both(item("issue", "closed", "not_planned", closed=BEFORE)),
    "open": lambda: both(item("issue", "open")),
    "no-number": lambda: (ok_repo(), {}),
    "repo-error": lambda: ({REPO: {"owner": "o", "status": "error", "fetched_at": "2026-11-01T00:00:00Z",
                                   "error_code": "http"}}, {}),
    "closed-after-t": lambda: both(item("issue", "closed", "completed", closed=AFTER)),
}
CLOSED_ROW = ("확인된 종결", "github", C_DAY)
# 뱅크 열림 키는 결과 행·손잡이와 무관하게 #396 판정 그대로(fmt b 줄).
OPEN_EXPECTED = {
    "completed": CLOSED_ROW, "merged-pr": CLOSED_ROW, "not-planned": ("종결(미계획)", "github", C_DAY),
    "open": ("열림 후보", O_DAY), "no-number": ("미확인·충돌", "no-number"), "repo-error": ("미확인·충돌", "repo-error"),
    "closed-after-t": ("열림 후보", O_DAY),
}


def expected_row(bank, in_rows, gh, mode):
    if bank == "open":
        return OPEN_EXPECTED[gh]
    if gh in ("open", "closed-after-t"):
        return ("미확인·충돌", "done-but-open")                  # 지금 충돌 줄 그대로
    if gh in ("completed", "merged-pr") and in_rows and mode != "off":
        return CLOSED_ROW                                         # 반증 줄
    return None


# --------------------------------------------------------------------------
# (a) 반증 줄 행렬
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode", [None, "result", "off"])                  # None = 손잡이 없음(기본 result)
@pytest.mark.parametrize("gh", list(GH))
@pytest.mark.parametrize("in_rows", [True, False])
@pytest.mark.parametrize("bank", ["open", "done"])
def test_done_closed_rows_matrix(monkeypatch, tmp_path, bank, in_rows, gh, mode):
    path = make_store(_path(tmp_path), BANK_MEMS)
    repos, items = GH[gh]()
    add_github(path, repos=repos, items=items)
    key = OPEN_KEY if bank == "open" else DONE_KEY
    num = int(key.rpartition("#")[2])
    knobs = {"table_fmt": "b", **({} if mode is None else {"done_closed_rows": mode})}
    resp, log, _ = run(monkeypatch, tmp_path, knobs=knobs, rows=rows_with(*([num] if in_rows else [])))
    table = table_of(resp)
    want = expected_row(bank, in_rows, gh, mode)
    assert row_of(table["text"], key) == want
    assert key not in table["keys"] or (want is not None and want[0] == "열림 후보")
    info = s3_audit(log)[0]["item_open_table"]
    assert "knob_warnings" not in info
    assert info.get("done_closed_rows") == (mode or "result")
    assert info.get("counts") == table.get("counts")


def test_rebuttal_uses_v2_result_keys(monkeypatch, tmp_path):
    """v2 저장소: 업스트림 문맥 번호는 참조 고정이라 결과 행 키가 아니다(반증 줄 없음)."""
    path = make_store(_path(tmp_path), BANK_MEMS)
    repos, items = GH["completed"]()
    add_github(path, repos=repos, items=items, meta={"key_rules": "v2"})
    rows = rows_with(20) + [mrow("u21", text="[인계] upstream #21 확인", tags=("kind:handoff", PROJECT))]
    resp, _, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": "b"}, rows=rows)
    assert row_of(table_of(resp)["text"], DONE_KEY) is None
    resp, _, _ = run(monkeypatch, tmp_path / "v2b", knobs={"table_fmt": "b"}, rows=rows_with(20, 21))
    assert row_of(table_of(resp)["text"], DONE_KEY) == CLOSED_ROW


def test_rebuttal_row_truncated_after_open_candidates(monkeypatch, tmp_path):
    """반증 줄은 확인된 종결이라 잘림 우선순위가 열림 후보 뒤다."""
    path = make_store(_path(tmp_path), BANK_MEMS)
    add_github(path, repos=ok_repo(), items={(REPO, 20): item("issue", "open"),
                                             (REPO, 21): item("issue", "closed", "completed", closed=BEFORE)})
    resp, _, _ = run(monkeypatch, tmp_path, knobs={"max_rows": 1}, rows=rows_with(20, 21))
    table = table_of(resp)
    assert table["keys"] == [OPEN_KEY] and table["counts"] == {"열림 후보": 1}
    assert table["text"].splitlines()[-2] == "(상한 1줄: 생략 확인된 종결 1개)"


def test_result_keys_public():
    assert s3.result_keys(rows_with(20, 21), {}, "v1") == {OPEN_KEY, DONE_KEY}
    assert s3.result_keys(rows_with(21, upstream=True), {}, "v2") == set()
    assert s3.result_keys(rows_with(21, upstream=True), {}, "v1") == {DONE_KEY}
    assert s3.result_keys(None, {}, "v2") == set()


def _states():
    cut = s3.item_status._instant("2026-10-20T14:59:59Z")
    return cut, {OPEN_KEY: s3.KeyState(s3.STATE_OPEN, s3.item_status._instant("2026-09-30T16:00:00Z")),
                 DONE_KEY: s3.KeyState(s3.STATE_DONE, s3.item_status._instant("2026-10-04T16:00:00Z"))}


def test_build_table_without_result_keys_has_no_rebuttal():
    cut, states = _states()
    _, items = GH["completed"]()
    gh = {"repos": ok_repo(), "items": items}
    knobs = s3._defaults()
    plain = s3.build_table(states, cut, None, knobs, github=gh)
    assert all(r[0] != DONE_KEY for r in plain["rows"])
    got = s3.build_table(states, cut, None, knobs, github=gh, result_keys={DONE_KEY})
    assert [DONE_KEY, "확인된 종결", C_DAY, "github"] in got["rows"]
    off = s3.build_table(states, cut, None, {**knobs, "done_closed_rows": "off"}, github=gh, result_keys={DONE_KEY})
    assert off == plain
    four = s3.build_table(states, cut, None, knobs)
    assert s3.build_table(states, cut, None, knobs, result_keys={DONE_KEY}) == four   # 근거 없으면 무시


# --------------------------------------------------------------------------
# (b) 기본 형식 a
# --------------------------------------------------------------------------

FMT_MEMS = [("f1", "2026-10-01T01:00:00Z", "valid", {f"{REPO}#{n}": "open" for n in (1, 2, 3, 4)})]
FMT_ITEMS = {(REPO, 1): item("issue", "open"), (REPO, 2): item("issue", "closed", "completed", closed=BEFORE),
             (REPO, 3): item("issue", "closed", "not_planned", closed=BEFORE)}                    # 4 = 번호 없음


@pytest.mark.parametrize("fmt,lines", [
    ("a", [f"{REPO}#1 | 열림 후보 | {O_DAY}", f"{REPO}#2 | 확인된 종결 | {C_DAY}", f"{REPO}#3 | 종결(미계획)",
           f"{REPO}#4 | 미확인·충돌 | no-number"]),
    ("b", [f"{REPO}#1 | 열림 후보 | {O_DAY}", f"{REPO}#2 | 확인된 종결 | github | {C_DAY}",
           f"{REPO}#3 | 종결(미계획) | github | {C_DAY}", f"{REPO}#4 | 미확인·충돌 | no-number"]),      # b 는 그대로
])
def test_three_state_render(monkeypatch, tmp_path, fmt, lines):
    path = make_store(_path(tmp_path), FMT_MEMS)
    add_github(path, repos=ok_repo(), items=FMT_ITEMS)
    resp, _, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": fmt, "over_max_rows": "keep"})     # #410: #402 렌더 고정
    text = table_of(resp)["text"].splitlines()
    assert text[4:8] == lines
    assert text[3] == s3.LEGEND[fmt]


def test_cols_and_legend_name_reason_codes():
    assert "사유" in s3.COLS_3["a"] and "날짜" in s3.COLS_3["a"]
    for fmt in ("a", "b"):
        for code in s3.UNKNOWN_REASONS:
            assert code in s3.LEGEND[fmt], (fmt, code)
        assert "\n" not in s3.LEGEND[fmt]
    assert "stale" in s3.UNKNOWN_REASONS


# 수정 전 코드(origin/main b3b6612)에서 같은 입력으로 잰 sha256: 테이블 없음·ignore 응답 바이트, 4인자 표 전체.
def _sha(b):
    return hashlib.sha256(b).hexdigest()


@pytest.mark.parametrize("fmt", ["a", "b"])
def test_no_table_and_ignore_bytes_unchanged(monkeypatch, tmp_path, fmt):
    no_table = make_store(tmp_path / "nt" / "item-roles.sqlite", BANK_MEMS)
    monkeypatch.setenv(s3.ENV_DB, str(no_table))
    plain, _, _ = run(monkeypatch, tmp_path / "plain", knobs={"table_fmt": fmt}, rows=rows_with(20, 21))
    path = make_store(_path(tmp_path), BANK_MEMS)
    repos, items = GH["completed"]()
    add_github(path, repos=repos, items=items)
    monkeypatch.setenv(s3.ENV_DB, str(path))
    ign, _, _ = run(monkeypatch, tmp_path / "ign", knobs={"table_fmt": fmt, "github_evidence": "ignore"},
                    rows=rows_with(20, 21))
    got = {"fmt": fmt, "plain": _sha(plain.content), "ignore": _sha(ign.content)}
    assert got == {"fmt": fmt, **PINNED_RESP[fmt]}


PINNED_RESP = {"a": {"plain": "6d7a01cf784f2ae52e101764b4d8cf857dd57905cf81241b9375859a903f43b4",
                     "ignore": "6d7a01cf784f2ae52e101764b4d8cf857dd57905cf81241b9375859a903f43b4"},
               "b": {"plain": "a4dbd3245c172e5d371569c784630e8d8be6cc820050ba50a0438fe59d1d8f12",
                     "ignore": "a4dbd3245c172e5d371569c784630e8d8be6cc820050ba50a0438fe59d1d8f12"}}


def test_four_args_text_unchanged():
    cut, states = _states()
    for fmt in ("a", "b"):
        table = s3.build_table(states, cut, None, {**s3._defaults(), "table_fmt": fmt, "recent_done_days": 30})
        assert _sha(json.dumps(table, ensure_ascii=False, sort_keys=True).encode("utf-8")) == PINNED_FOUR[fmt]


PINNED_FOUR = {"a": "acfe71949adbb9883d9d560d48f08a46c5dedc723a187f01839d6dd07cd94aae",
               "b": "5db926abd867a9a0eb2e57578d952df5611d21094a8bedc777ea48cfc0358426"}


# --------------------------------------------------------------------------
# (c) 근거 나이 상한
# --------------------------------------------------------------------------

OTHER = "other-repo"
AGE_MEMS = BANK_MEMS + [("o1", "2026-10-03T01:00:00Z", "valid", {f"{OTHER}#3": "open"})]


def _age_store(tmp_path, repo_fetched, other_fetched):
    """other_fetched = 다른 리포 근거 시각(읽는 시각과 같게 둬 늘 신선, rework #402-1 미래 오차 규칙과 무관하게)."""
    path = make_store(_path(tmp_path), AGE_MEMS)
    repos = {REPO: {"owner": "o", "status": "ok", "fetched_at": repo_fetched, "error_code": None},
             OTHER: {"owner": "o", "status": "ok", "fetched_at": other_fetched, "error_code": None}}
    items = {(REPO, 20): item("issue", "open"), (REPO, 21): item("issue", "open"),
             (OTHER, 3): item("issue", "open", fetched=other_fetched)}
    add_github(path, repos=repos, items=items)
    return path


@pytest.mark.parametrize("knob", [None, 0, 4])
@pytest.mark.parametrize("repo_fetched", ["2026-11-01T00:00:00Z", ""])     # "" = 리포 행 fetched_at 못 읽음(항목 행 최댓값)
@pytest.mark.parametrize("age_h", [1, 3])
def test_freshness_matrix(monkeypatch, tmp_path, clock, age_h, repo_fetched, knob):
    _age_store(tmp_path, repo_fetched, f"2026-11-01T{age_h:02d}:00:00Z")
    clock["t"] = FETCHED_EPOCH + age_h * HOUR
    knobs = {"table_fmt": "b", **({} if knob is None else {"github_max_age_hours": knob})}
    resp, log, _ = run(monkeypatch, tmp_path, knobs=knobs, rows=rows_with(20, 21),
                       args={"tags": [PROJECT, f"project:{OTHER}"]})
    table = table_of(resp)
    limit = 2 if knob is None else knob
    stale = limit > 0 and age_h > limit
    if stale:
        assert row_of(table["text"], OPEN_KEY) == ("미확인·충돌", "stale")
        assert row_of(table["text"], DONE_KEY) == ("미확인·충돌", "stale")
        assert OPEN_KEY not in table["keys"]
    else:
        assert row_of(table["text"], OPEN_KEY) == ("열림 후보", O_DAY)
        assert row_of(table["text"], DONE_KEY) == ("미확인·충돌", "done-but-open")
    assert row_of(table["text"], f"{OTHER}#3") == ("열림 후보", "2026-10-03")      # 다른 리포 줄은 그대로
    info = s3_audit(log)[0]["item_open_table"]
    assert "knob_warnings" not in info
    assert info.get("github_max_age_hours") == limit
    assert info.get("unknown_reasons") == ({"stale": 2} if stale else {"done-but-open": 1})


def test_stale_when_no_fetched_at_anywhere(monkeypatch, tmp_path):
    """리포 행 fetched_at 을 못 읽고 그 리포 항목 행도 없으면 stale(코디네이터 결정 3)."""
    path = make_store(_path(tmp_path), BANK_MEMS)
    add_github(path, repos={REPO: {"owner": "o", "status": "ok", "fetched_at": "", "error_code": None}}, items={})
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": "b"}, rows=rows_with(20))
    assert row_of(table_of(resp)["text"], OPEN_KEY) == ("미확인·충돌", "stale")
    assert s3_audit(log)[0]["item_open_table"]["unknown_reasons"] == {"stale": 1}


def test_missing_repo_row_stays_not_fetched(monkeypatch, tmp_path, clock):
    """리포 행이 없으면 stale 이 아니라 repo-not-fetched 그대로(코디네이터 결정, 원인 구분)."""
    path = make_store(_path(tmp_path), BANK_MEMS)
    add_github(path, repos={}, items={})
    clock["t"] = FETCHED_EPOCH + 100 * HOUR
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": "b"}, rows=rows_with(20))
    assert row_of(table_of(resp)["text"], OPEN_KEY) == ("미확인·충돌", "repo-not-fetched")


def test_stale_repos_public():
    now = datetime.fromtimestamp(FETCHED_EPOCH + 3 * HOUR, timezone.utc)
    gh = {"repos": {"a": {"status": "ok", "fetched_at": "2026-11-01T00:00:00Z"},
                    "b": {"status": "ok", "fetched_at": None},
                    "c": {"status": "ok", "fetched_at": "x"},
                    "d": {"status": "ok", "fetched_at": "2026-11-01T02:30:00Z"}},
          "items": {("b", 1): {"fetched_at": "2026-11-01T00:00:00Z"}, ("b", 2): {"fetched_at": "2026-11-01T01:30:00Z"}}}
    assert s3.stale_repos(gh, now, 2) == {"a", "c"}            # b = 항목 최댓값 01:30 -> 1.5시간, d = 30분
    assert s3.stale_repos(gh, now, 0) == set()
    assert s3.stale_repos(gh, now, 1) == {"a", "b", "c"}


def test_build_table_now_keyword_only():
    """build_table 은 now 를 줄 때만 나이를 본다(github= 호출은 그대로)."""
    cut, states = _states()
    gh = {"repos": ok_repo(), "items": {(REPO, 20): item("issue", "open")}}
    knobs = s3._defaults()
    plain = s3.build_table(states, cut, None, knobs, github=gh)
    late = datetime.fromtimestamp(FETCHED_EPOCH + 10 * HOUR, timezone.utc)
    assert s3.build_table(states, cut, None, knobs, github=gh, now=None) == plain
    got = s3.build_table(states, cut, None, knobs, github=gh, now=late)
    assert [OPEN_KEY, "미확인·충돌", O_DAY, "stale"] in got["rows"]


# --------------------------------------------------------------------------
# (d) 손잡이
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key,good,bad", [
    ("done_closed_rows", ["result", "off"], ["on", 1, None]),
    ("github_max_age_hours", [0, 2, 168], [-1, 2.5, True, "2"]),
])
def test_new_knobs_are_optional(key, good, bad):
    assert key not in s3.DEFAULTS and key not in s3._defaults()
    for v in good:
        values, warnings = s3.parse_knobs(json.dumps({key: v}))
        assert warnings == [] and values[key] == v
    for v in bad:
        values, warnings = s3.parse_knobs(json.dumps({key: v}))
        assert warnings == [key] and key not in values
    assert s3.Knobs(s3._defaults(), "default").sha256 == DEFAULTS_SHA256_R7_2
    assert s3.Knobs({**s3._defaults(), "header": s3.HEADER_R6_1}, "default").sha256 == DEFAULTS_SHA256_R6


def test_audit_carries_knob_defaults(monkeypatch, tmp_path):
    path = make_store(_path(tmp_path), BANK_MEMS)
    add_github(path, repos=ok_repo(), items={})
    _, log, _ = run(monkeypatch, tmp_path, rows=rows_with(20))
    info = s3_audit(log)[0]["item_open_table"]
    assert info.get("done_closed_rows") == "result" and info.get("github_max_age_hours") == 2


# --------------------------------------------------------------------------
# (e) rework #402-1: 미래 시각 fetched_at(시계 어긋남) - 허용 오차 10분을 넘으면 stale
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ahead_min,knob,stale", [
    (1, None, False),           # 오차 안의 미래 = 나이 0(신선)
    (11, None, True),           # 오차 밖의 미래 = stale
    (11, 0, False),             # 손잡이 0 = 검사 안 함
    (-1, None, False),          # 과거 1분(대조)
])
def test_future_fetched_at(monkeypatch, tmp_path, clock, ahead_min, knob, stale):
    path = make_store(_path(tmp_path), BANK_MEMS)
    add_github(path, repos=ok_repo(), items={(REPO, 20): item("issue", "open")})
    clock["t"] = FETCHED_EPOCH - ahead_min * 60            # 읽는 시각이 근거 fetched_at 보다 ahead_min 분 이르다
    knobs = {"table_fmt": "b", **({} if knob is None else {"github_max_age_hours": knob})}
    resp, log, _ = run(monkeypatch, tmp_path, knobs=knobs, rows=rows_with(20))
    want = ("미확인·충돌", "stale") if stale else ("열림 후보", O_DAY)
    assert row_of(table_of(resp)["text"], OPEN_KEY) == want
    now = datetime.fromtimestamp(clock["t"], timezone.utc)
    limit = 2 if knob is None else knob
    assert s3.stale_repos({"repos": ok_repo(), "items": {}}, now, limit) == ({REPO} if stale else set())
