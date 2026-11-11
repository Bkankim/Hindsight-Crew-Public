"""S3 3상태 표 over-max 제외 + 키 규칙 v2.1 게이트웨이 경로 (issue #410, R7-5). 네트워크 0, 임시 sqlite 픽스처.

고정하는 계약:
  (a) over-max: 3상태 모드에서 사유 `no-number` 이고 번호가 그 리포 수집 데이터 최대 번호(저장소 github_items 의 그 리포
      번호 최댓값, 뱅크가 언급하지 않은 번호 포함)보다 크면 표에서 뺀다. 감사 `unknown_reasons` 에 `over-max` 로 센다.
      최대 번호 이하 no-number 는 `미확인·충돌` 그대로. 리포 행이 ok 가 아니거나(사유 repo-error 등) stale 이면 판정하지
      않는다. 그 리포 항목 행이 하나도 없으면 최대 번호가 없어 판정하지 않는다.
  (b) 손잡이 `over_max_rows`(drop|keep, 기본 drop)는 DEFAULTS 밖 선택 키(정규 sha256 불변). keep 이면 지금 응답과
      바이트가 같다(아래 고정 sha256 은 수정 전 코드로 잰 값). 감사 줄에 손잡이 값.
  (c) v2.1 저장소(meta key_rules=v2.1): 동점 끝남, 결과 행 범위·반증 키를 v2.1 규칙으로 다시 계산한다. v2 그대로.
red: 수정 전 코드에서는 (a) 의 drop 단언이 AssertionError 다(모르는 손잡이는 무시되고 줄이 남는다). (c) 는
`HC_R410_RED=1` 이면 v2.1 대신 v2 로 돌아 단언이 실패한다.
"""
import hashlib
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway import item_open_table as s3
from gateway import item_roles as IR
from test_item_open_table import (_s3_env, make_store, run, s3_audit, table_of,  # noqa: F401
                                  PROJECT)
from test_item_open_table_github import (DEFAULTS_SHA256_R7_2, REPO, _path, add_github, item, ok_repo, row_of)
from test_item_status import mrow

RED = os.environ.get("HC_R410_RED") == "1"
NEW = "v2" if RED else "v2.1"
FETCHED_EPOCH = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0
O_DAY = "2026-10-01"
LOW, HIGH, OPEN_N, MAX_N = 5, 30, 20, 25
MEMS = [("g1", "2026-10-01T01:00:00Z", "valid", {f"{REPO}#{n}": "open" for n in (LOW, OPEN_N, HIGH)})]


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(s3, "_wall_clock", lambda: FETCHED_EPOCH + HOUR)


def base_items(extra=()):
    items = {(REPO, OPEN_N): item("issue", "open"),
             (REPO, MAX_N): item("issue", "closed", "completed", closed="2026-10-10T00:00:00Z")}
    for n in extra:
        items[(REPO, n)] = item("issue", "open")
    return items


def setup(tmp_path, *, repos=None, items=None, meta=None):
    path = make_store(_path(tmp_path), MEMS)
    add_github(path, repos=ok_repo() if repos is None else repos, items=base_items() if items is None else items,
               meta=meta)
    return path


def k(n):
    return f"{REPO}#{n}"


# --------------------------------------------------------------------------
# (a) over-max 행렬: 근거 상황 x 손잡이 -> HIGH·LOW 줄과 over-max 수
# --------------------------------------------------------------------------

ERR_REPO = {REPO: {"owner": "o", "status": "error", "fetched_at": "2026-11-01T00:00:00Z", "error_code": "http"}}
MISSING_REPO = {REPO: {"owner": "o", "status": "missing", "fetched_at": "2026-11-01T00:00:00Z", "error_code": "404"}}
STALE_REPO = ok_repo("2026-10-30T00:00:00Z")
NOT_JUDGED = ("미확인·충돌", "no-number")
CASES = {
    # 상황: (repos, items, HIGH 줄(drop), LOW 줄, drop 일 때 over-max 수)
    "over-max": (None, None, None, NOT_JUDGED, 1),
    "unmentioned-higher-number-raises-max": (None, base_items(extra=(99,)), NOT_JUDGED, NOT_JUDGED, 0),
    "repo-error-not-judged": (ERR_REPO, None, ("미확인·충돌", "repo-error"), ("미확인·충돌", "repo-error"), 0),
    "repo-missing-not-judged": (MISSING_REPO, None, ("미확인·충돌", "repo-missing"), ("미확인·충돌", "repo-missing"), 0),
    "stale-not-judged": (STALE_REPO, None, ("미확인·충돌", "stale"), ("미확인·충돌", "stale"), 0),
    "no-items-no-max": (None, {}, NOT_JUDGED, NOT_JUDGED, 0),
}


@pytest.mark.parametrize("knob", [None, "drop", "keep"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_over_max_matrix(monkeypatch, tmp_path, case, knob):
    repos, items, high, low, n_over = CASES[case]
    setup(tmp_path, repos=repos, items=items)
    knobs = {"table_fmt": "b", **({} if knob is None else {"over_max_rows": knob})}
    resp, log, _ = run(monkeypatch, tmp_path, knobs=knobs)
    table = table_of(resp)
    info = s3_audit(log)[0]["item_open_table"]
    drop = knob != "keep"
    want_high = high if (drop or n_over == 0) else NOT_JUDGED
    assert row_of(table["text"], k(HIGH)) == want_high
    assert row_of(table["text"], k(LOW)) == low
    assert info.get("over_max_rows") == ("drop" if knob is None else knob)
    over = info["unknown_reasons"].get("over-max", 0)
    assert over == (n_over if drop else 0)
    if drop and n_over:
        assert table["counts"].get("미확인·충돌") == 1          # 뺀 줄은 상태별 줄 수에도 없다
        assert info["counts"] == table["counts"]


def test_over_max_does_not_touch_open_candidates_or_done_keys(monkeypatch, tmp_path):
    mems = [("g1", "2026-10-01T01:00:00Z", "valid", {k(OPEN_N): "open", k(40): "done"})]
    path = make_store(_path(tmp_path), mems)
    add_github(path, repos=ok_repo(), items=base_items())
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": "b"})
    table = table_of(resp)
    assert table["keys"] == [k(OPEN_N)]
    assert row_of(table["text"], k(40)) is None
    assert "over-max" not in s3_audit(log)[0]["item_open_table"]["unknown_reasons"]


# keep 렌더 = 수정 전 코드의 응답 바이트(수정 전 HEAD 에서 같은 픽스처로 잰 sha256).
KEEP_SHA256 = {
    ("a", "none"): "bfef13ceff978b0db21dc3e70ab2773b21ffa3b6916a158ab8512713e8f47a6d",
    ("b", "none"): "de012ea7447cc741cccc09f1846355e7ab970a6697c60b038b633c88425902b8",
    ("a", "v2"): "bfef13ceff978b0db21dc3e70ab2773b21ffa3b6916a158ab8512713e8f47a6d",
    ("b", "v2"): "de012ea7447cc741cccc09f1846355e7ab970a6697c60b038b633c88425902b8",
}


@pytest.mark.parametrize("fmt, rules", sorted(KEEP_SHA256))
def test_keep_is_byte_identical_to_before(monkeypatch, tmp_path, fmt, rules):
    setup(tmp_path, meta=None if rules == "none" else {"key_rules": rules})
    resp, _, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": fmt, "over_max_rows": "keep"})
    assert hashlib.sha256(resp.content).hexdigest() == KEEP_SHA256[(fmt, rules)]


@pytest.mark.parametrize("fmt", ["a", "b"])
def test_ignore_mode_is_unaffected_by_the_knob(monkeypatch, tmp_path, fmt):
    setup(tmp_path)
    plain, _, _ = run(monkeypatch, tmp_path / "p", knobs={"table_fmt": fmt, "github_evidence": "ignore"})
    setup(tmp_path / "x")
    drop, _, _ = run(monkeypatch, tmp_path / "x", knobs={"table_fmt": fmt, "github_evidence": "ignore",
                                                          "over_max_rows": "drop"})
    assert table_of(plain)["text"] == table_of(drop)["text"]
    assert k(HIGH) in table_of(drop)["keys"]                  # 근거 없는 지금 모양: 열림 그대로


# --------------------------------------------------------------------------
# (b) 손잡이·순수 함수
# --------------------------------------------------------------------------

def test_knob_is_optional_and_defaults_sha_unchanged():
    assert "over_max_rows" not in s3.DEFAULTS
    assert s3.Knobs(s3._defaults(), "default").sha256 == DEFAULTS_SHA256_R7_2
    assert s3.over_max_mode({}) == "drop"


@pytest.mark.parametrize("value, ok", [("drop", True), ("keep", True), ("DROP", False), ("", False), (1, False),
                                       (None, False)])
def test_knob_validation(value, ok):
    values, warnings = s3.parse_knobs(json.dumps({"over_max_rows": value}))
    assert (values.get("over_max_rows", "<absent>") == value) is ok
    assert (warnings == []) is ok


GH = {"repos": {REPO: {"status": "ok", "fetched_at": "2026-11-01T00:00:00Z"}, "other": {"status": "error"}},
      "items": {(REPO, 3): {"kind": "issue"}, (REPO, 9): {"kind": "pr"}, ("other", 50): {"kind": "issue"}}}


def test_repo_max_numbers_from_items():
    assert s3.repo_max_numbers(GH) == {REPO: 9, "other": 50}
    assert s3.repo_max_numbers(None) == {}


@pytest.mark.parametrize("key, maxes, expect", [
    (k(10), {REPO: 9}, True),
    (k(9), {REPO: 9}, False),
    (k(4), {REPO: 9}, False),                       # 최대 이하 번호 없음은 판정하지 않는다
    (k(10), {}, False),                             # 최대 번호를 모르면 판정하지 않는다
    ("other#60", {"other": 50}, False),             # 리포 행이 ok 가 아니다
    ("nobody#60", {"nobody": 1}, False),            # 리포 행이 없다(repo-not-fetched)
    ("not-a-key", {REPO: 9}, False),
])
def test_over_max_function(key, maxes, expect):
    assert s3.over_max(key, GH, maxes) is expect


# rework #410-1: 그 키의 언급(뱅크 상태 시각)이 리포 행 fetched_at 보다 늦으면 수집 뒤 생긴 번호일 수 있어 판정하지 않는다.
@pytest.mark.parametrize("mentioned, expect", [
    (datetime(2026, 11, 1, 1, tzinfo=timezone.utc), False),            # 수집(11-01 00:00) 뒤 언급
    (datetime(2026, 10, 31, tzinfo=timezone.utc), True),               # 수집 전 언급
    (None, True),                                                      # 언급 시각 모름 = 지금 동작
])
def test_over_max_skips_mentions_after_fetch(mentioned, expect):
    assert s3.over_max(k(10), GH, {REPO: 9}, mentioned_at=mentioned) is expect


def test_over_max_unreadable_fetched_at_keeps_judging():
    gh = {"repos": {REPO: {"status": "ok", "fetched_at": "garbage"}}, "items": {}}
    assert s3.over_max(k(10), gh, {REPO: 9}, mentioned_at=datetime(2030, 1, 1, tzinfo=timezone.utc)) is True


@pytest.mark.parametrize("repo_fetched, kept", [("2026-09-30T00:00:00Z", True), ("2026-10-02T00:00:00Z", False)])
def test_over_max_freshness_end_to_end(monkeypatch, tmp_path, repo_fetched, kept):
    """언급 10-01 01:00. 리포 수집이 그보다 이르면 HIGH 는 no-number 로 남고, 늦으면 over-max 로 빠진다(나이 검사는 끈다)."""
    setup(tmp_path, repos=ok_repo(repo_fetched))
    resp, log, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": "b", "github_max_age_hours": 0})
    assert row_of(table_of(resp)["text"], k(HIGH)) == (NOT_JUDGED if kept else None)
    over = s3_audit(log)[0]["item_open_table"]["unknown_reasons"].get("over-max", 0)
    assert over == (0 if kept else 1)



def test_build_table_judges_only_with_max_numbers():
    states = {k(HIGH): s3.KeyState("열림", datetime(2026, 10, 1, tzinfo=timezone.utc)),
              k(LOW): s3.KeyState("열림", datetime(2026, 10, 1, tzinfo=timezone.utc))}
    gh = {"repos": ok_repo(), "items": base_items()}
    cut = datetime(2026, 10, 20, tzinfo=timezone.utc)
    knobs = s3._defaults()
    plain = s3.build_table(states, cut, None, knobs, github=gh)
    assert [r[0] for r in plain["rows"]] == [k(LOW), k(HIGH)] and "over_max" not in plain
    dropped = s3.build_table(states, cut, None, knobs, github=gh, max_numbers=s3.repo_max_numbers(gh))
    assert [r[0] for r in dropped["rows"]] == [k(LOW)] and dropped["over_max"] == 1
    kept = s3.build_table(states, cut, None, {**knobs, "over_max_rows": "keep"}, github=gh,
                          max_numbers=s3.repo_max_numbers(gh))
    assert kept == plain


def test_read_store_reads_the_repo_max_over_unmentioned_numbers(tmp_path):
    path = setup(tmp_path, items=base_items(extra=(99,)))
    store = s3.read_store(str(path), 1.5, {REPO})
    assert store.github_max == {REPO: 99}
    assert (REPO, 99) not in store.github["items"]             # 항목 행은 여전히 뱅크가 언급한 번호만 읽는다
    assert s3.read_store(str(path), 1.5, None).github_max is None


# --------------------------------------------------------------------------
# (c) 키 규칙 v2.1 저장소
# --------------------------------------------------------------------------

ALIASES = {"ar": "alpha-repo", "alpha-repo": "alpha-repo", "hc": REPO}


def _at(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_role_states_v21_tie_is_done():
    at = _at("2026-10-01T00:00:00Z")
    ms = [s3.Mention(k(1), "open", at, "a"), s3.Mention(k(1), "done", at, "b")]
    assert s3.role_states(ms, _at("2026-10-20T00:00:00Z"), NEW)[k(1)].state == "끝남"
    assert s3.role_states(ms, _at("2026-10-20T00:00:00Z"), "v1")[k(1)].state == "열림"


PREFLIGHT_ROWS = [mrow("r1", text="[인계] ar #11·#12(정리), #13 남음", tags=("kind:handoff", PROJECT))]


def test_result_keys_v21_follow_the_enumeration_prefix():
    assert s3.result_keys(PREFLIGHT_ROWS, ALIASES, NEW) == {"alpha-repo#11", "alpha-repo#12", "alpha-repo#13"}
    assert s3.result_keys(PREFLIGHT_ROWS, ALIASES, "v2") == {"alpha-repo#11", f"{REPO}#12", f"{REPO}#13"}


def test_result_repos_v21():
    assert s3.result_repos_v2(PREFLIGHT_ROWS, ALIASES, NEW) == {"alpha-repo"}
    assert s3.result_repos_v2(PREFLIGHT_ROWS, ALIASES) == {"alpha-repo", REPO}


def test_rescope_v21_recomputes_result_scope():
    rows = [mrow("r1", text="[메모] upstream #5 참고", tags=("kind:handoff", PROJECT))]
    assert s3.rescope_for_key_rules(({REPO}, "result"), rows, ALIASES, NEW) == (None, "no-result-keys-v2")
    assert s3.rescope_for_key_rules(({REPO}, "result"), PREFLIGHT_ROWS, ALIASES, NEW) == ({"alpha-repo"}, "result")
    assert s3.rescope_for_key_rules(({REPO}, "request"), rows, ALIASES, NEW) == ({REPO}, "request")


def test_v21_store_end_to_end_audit_key_rules(monkeypatch, tmp_path):
    setup(tmp_path, meta={"key_rules": NEW})
    _, log, _ = run(monkeypatch, tmp_path, knobs={"table_fmt": "b"})
    assert s3_audit(log)[0]["item_open_table"]["key_rules"] == NEW


# --------------------------------------------------------------------------
# 사이드카: rekey --rules v2.1
# --------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("item_roles_sidecar_410", _REPO_ROOT / "scripts" / "item_roles_sidecar.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)


def test_sidecar_rekey_v21(tmp_path, monkeypatch):
    from test_item_roles_rekey import ALIASES as RK_ALIASES, Caller, mem, source
    text = "[인계] hc #11·#12(정리), #13 남음"
    m = mem("pf", text, tags=["project:other-repo"])
    db = source(tmp_path, {"pf": (m, {"hindsight-crew#11": "open"})})
    S.save_listing({"pf": m}, str(tmp_path / "b.jsonl"), str(tmp_path / "m.jsonl"))
    af = tmp_path / "aliases.json"
    af.write_text(json.dumps(RK_ALIASES))
    caller = Caller()
    monkeypatch.setattr(S, "claude_caller", lambda **kw: caller)
    out = tmp_path / "new.sqlite"
    rc = S.main(["rekey", "--db", str(db), "--out", str(out), "--rules", NEW, "--bank", "bank-x",
                 "--offline-bodies", str(tmp_path / "b.jsonl"), "--offline-meta", str(tmp_path / "m.jsonl"),
                 "--aliases-file", str(af), "--max-usd", "1"])
    meta, _, roles = IR.load_store(str(out))
    assert rc == 0 and meta["key_rules"] == NEW
    assert sorted(roles["pf"]) == ["hindsight-crew#11", "hindsight-crew#12", "hindsight-crew#13"]
