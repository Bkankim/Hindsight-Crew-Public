"""이슈 #237: 재발 탐지 초안에 aside 등록 가능성(`aside_scope`)을 표시한다.

길목 훅은 `tool: "aside"`만 보내므로 aside 밖 함정 초안은 등록해도 발화하지 않는다(#174).
초안이 만들어지는 근거 기억 텍스트(새 gotcha + 유사 기억)의 aside 단서로 `likely|unlikely`를 붙이고,
`unlikely`일 때만 요약 줄에 "등록 불가 추정"을 적는다. 표시만 한다 - 상태·감사 줄·원장은 그대로다.

확인하는 경계:
  1. 분류 함수와 build_draft: 단서 있음/없음/단어 경계/양쪽 텍스트.
  2. discord_message: unlikely에만 표시 문구.
  3. 상태(state)는 단서와 무관하게 기존 규칙 그대로.
  4. 끝에서 끝까지(임시 경로 원장): 감사 줄 decision/reason 형식 불변, 기존 원장 행은 다시 쓰이지 않는다.
"""
import json

import pytest

from gateway import recurrence
from test_retain_gates import (BANK, GOTCHA_NEW, GOTCHA_OLD, RESCUE_QUERY, Notifier, Upstream,
                               audit_lines, make, mcp, mem, retain_args)

MARK = "등록 불가 추정(비-aside 함정, #174)"
OLD = "fb9fc4e3-dddd"

GIT_NEW = "[발견] git commit이 pre-commit 훅에서 거부된다 - 스테이징 후 --no-verify 없이 다시 커밋해야 한다"
GIT_OLD = "[발견] git 커밋이 훅 검사로 거부되어 스테이징을 다시 해야 통과한다"

# (새 gotcha 본문, 유사 기억 본문, 기대 aside_scope)
SCOPE_CASES = [
    pytest.param(GOTCHA_NEW, GOTCHA_OLD, "likely", id="aside-word-both"),
    pytest.param(GIT_NEW, GIT_OLD, "unlikely", id="git-commit-no-clue"),
    pytest.param(GIT_NEW, "[발견] aside 세션에서도 같은 커밋 거부가 난다", "likely", id="clue-only-in-similar"),
    pytest.param("[발견] git 훅 거부 - aside exec로 돌려도 같다", GIT_OLD, "likely", id="clue-only-in-new"),
    pytest.param("[발견] mcp__aside__exec 호출이 타임아웃된다", GIT_OLD, "likely", id="mcp-tool-name"),
    pytest.param("[발견] aside repl의 page.goto가 멈춘다", GIT_OLD, "likely", id="repl"),
    pytest.param("[발견] 브라우저 세션에서 로그인 쿠키가 만료된다", GIT_OLD, "likely", id="korean-browser-login-cookie"),
    pytest.param("[발견] Browser login redirect loops", "[발견] Cookie jar is empty", "likely", id="english-case-insensitive"),
    # 복수형은 같은 단서다 (final-review C1)
    pytest.param("[발견] session cookies expire after the browsers restart", GIT_OLD, "likely", id="plural-forms"),
    # 브라우저 조작 어휘 (final-review C2)
    pytest.param("[발견] 크롬 프로필 캡차에서 멈춘다", GIT_OLD, "likely", id="korean-chrome-captcha"),
    pytest.param("[발견] Playwright page가 Chrome에서 멈춘다", GIT_OLD, "likely", id="english-playwright-chrome"),
    # 단어 경계: exec/repl이 execute/replace의 일부일 때는 단서가 아니다
    pytest.param("[발견] execute 권한이 없어 스크립트가 실패한다", "[발견] replace 옵션이 거부된다", "unlikely", id="exec-repl-inside-words"),
    pytest.param("[발견] gh pr merge는 로컬 브랜치 삭제에서 실패한다", "[발견] pnpm install은 TTY가 필요하다", "unlikely", id="other-tools"),
]


def _draft(new, old_text, rescue=None):
    return recurrence.build_draft(bank=BANK, signal=recurrence.SIGNAL_GOTCHA, new_content=new,
                                  similar=mem(OLD, old_text, 0.958), rescue=rescue)


def _rescue():
    return recurrence.RecallEvent(ts=0.0, identity="alice", bank=BANK, transport="mcp",
                                  query=RESCUE_QUERY, tags=("project:x",), result_ids=(OLD,))


@pytest.mark.parametrize("new, old_text, expected", SCOPE_CASES)
def test_draft_carries_aside_scope(new, old_text, expected):
    assert _draft(new, old_text, _rescue())["aside_scope"] == expected


@pytest.mark.parametrize("new, old_text, expected", SCOPE_CASES)
@pytest.mark.parametrize("with_rescue", [True, False], ids=["ready-for-review", "needs-evidence"])
def test_summary_marks_only_unlikely(new, old_text, expected, with_rescue):
    msg = recurrence.discord_message(_draft(new, old_text, _rescue() if with_rescue else None))
    assert (MARK in msg) is (expected == "unlikely")


@pytest.mark.parametrize("new, old_text, expected", SCOPE_CASES)
@pytest.mark.parametrize("with_rescue, state", [(True, "ready-for-review"), (False, "needs-evidence")])
def test_state_ignores_aside_scope(new, old_text, expected, with_rescue, state):
    d = _draft(new, old_text, _rescue() if with_rescue else None)
    assert d["state"] == state
    assert (d["trigger"] is not None) is with_rescue                 # needs-evidence keeps NO trigger


# (tool_hint, 본문, 기대 aside_scope) - tool_hint가 있으면 본문 단서보다 우선한다 (final-review C7)
TOOL_HINT_CASES = [
    pytest.param("aside", GIT_NEW, "likely", id="aside-hint-clue-free-text"),
    pytest.param("Aside", GIT_NEW, "likely", id="aside-hint-case-insensitive"),
    pytest.param("mcp__aside__exec", GIT_NEW, "likely", id="aside-mcp-tool-hint"),
    pytest.param("bash", GOTCHA_NEW, "unlikely", id="bash-hint-aside-text"),
    pytest.param("bash", GIT_NEW, "unlikely", id="bash-hint-clue-free-text"),
    pytest.param(None, GOTCHA_NEW, "likely", id="no-hint-falls-back-to-text-likely"),
    pytest.param(None, GIT_NEW, "unlikely", id="no-hint-falls-back-to-text-unlikely"),
    pytest.param("", GOTCHA_NEW, "likely", id="empty-hint-falls-back-to-text"),
]


@pytest.mark.parametrize("tool_hint, new, expected", TOOL_HINT_CASES)
@pytest.mark.parametrize("with_rescue", [True, False], ids=["ready-for-review", "needs-evidence"])
def test_tool_hint_takes_precedence_over_text_clues(tool_hint, new, expected, with_rescue):
    d = recurrence.build_draft(bank=BANK, signal=recurrence.SIGNAL_GOTCHA, new_content=new,
                               similar=mem(OLD, GIT_OLD, 0.958), rescue=_rescue() if with_rescue else None,
                               tool_hint=tool_hint)
    assert d["aside_scope"] == expected
    assert (MARK in recurrence.discord_message(d)) is (expected == "unlikely")
    assert d["state"] == ("ready-for-review" if with_rescue else "needs-evidence")


def test_draft_without_similar_memory_still_classifies_new_content():
    d = recurrence.build_draft(bank=BANK, signal=recurrence.SIGNAL_CORRECTION, new_content=GIT_NEW,
                               similar=None, rescue=None)
    assert d["aside_scope"] == "unlikely"
    d = recurrence.build_draft(bank=BANK, signal=recurrence.SIGNAL_CORRECTION, new_content=GOTCHA_NEW,
                               similar=None, rescue=None)
    assert d["aside_scope"] == "likely"


def test_legacy_draft_without_the_field_renders_without_the_mark():
    """기존 원장 행(aside_scope 없음)을 다시 요약해도 표시 문구를 추정해 붙이지 않는다."""
    d = _draft(GIT_NEW, GIT_OLD, _rescue())
    d.pop("aside_scope")
    assert MARK not in recurrence.discord_message(d)


# --- 끝에서 끝까지: 임시 경로 원장 ---------------------------------------------------------

def _run(tmp_path, new, old_text):
    old = mem(OLD, old_text, 0.958)
    up = Upstream(gate_results=[old], mcp_recall_results=[old])
    notifier = Notifier()
    client, log = make(up, tmp_path, notifier)
    pending = tmp_path / "pending.jsonl"
    legacy = '{"draft_id": "legacy0000001", "state": "needs-evidence", "signal": "gotcha-recurrence"}\n'
    pending.write_text(legacy, encoding="utf-8")
    assert mcp(client, "recall", {"query": RESCUE_QUERY, "tags": ["project:x"]}, el_id=7).status_code == 200
    assert mcp(client, "retain", retain_args(new), el_id=8).status_code == 200
    return pending, legacy, notifier, log


@pytest.mark.parametrize("new, old_text, expected", [
    pytest.param(GOTCHA_NEW, GOTCHA_OLD, "likely", id="likely"),
    pytest.param(GIT_NEW, GIT_OLD, "unlikely", id="unlikely"),
])
def test_end_to_end_ledger_audit_and_notification(tmp_path, new, old_text, expected):
    pending, legacy, notifier, log = _run(tmp_path, new, old_text)
    lines = pending.read_text(encoding="utf-8").splitlines(keepends=True)
    assert lines[0] == legacy                                        # 기존 행은 바이트 그대로
    assert len(lines) == 2
    d = json.loads(lines[1])
    assert d["aside_scope"] == expected and d["state"] == "ready-for-review"
    assert d["trigger"] is not None                                  # unlikely도 자동 기각·삭제 없음
    assert (MARK in recurrence.discord_message(notifier.drafts[0])) is (expected == "unlikely")
    ev = audit_lines(log, "recurrence")
    assert ev[0]["decision"] == "recurrence" and ev[0]["reason"] == "draft:gotcha-recurrence"
    assert ev[0]["recurrence"]["state"] == "ready-for-review"
