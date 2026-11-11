"""retain 모순 게이트 (issue #12, 3차) - warn, never reject.

A misjudgement made mid-session has no choke point; it becomes dangerous only when it is
retained and turns into the next session's premise. Every retain crosses this gateway, so
this is where the polarity check lives: a NEW retain whose bracket label says "blocked /
impossible / stuck" while a highly similar EXISTING memory says "done / succeeded" gets
stored anyway and answered with a warning that quotes the precedent.

Why warn and not reject: the world really does change (measured: sandbox file access
narrowed, invalidating an earlier success path). A rejection would erase
legitimate updates; a warning makes the model ask the one question that matters - "did you
actually try the path the precedent describes before calling it blocked?"

Polarity is a STRUCTURAL signal, LLM-free: the bank's "[label] body" content convention
plus kind tags. The last explicit outcome in a label wins: "실패 복구" is positive,
"복구 실패" is negative. Known negations such as "미완료" and "not resolved" stay
negative as a whole. A negative label overrides kind:completion/fix; those tags remain
the positive fallback when the label has no recognized outcome. A "[정정+완료]" gotcha
is positive even though its kind tag says gotcha (the measured precedent).
Similarity = the recall reranker score (sidecar cross-encoder),
threshold HC_CONTRADICTION_MIN_RERANK (default 0.90; a same-topic-different-trap pair
measured 0.876, the real pair 0.957).
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence

from .context_triggers import rerank_score, scrub_secrets

DECISION = "contradiction-warn"        # audit vocabulary (audit.py)
DEFAULT_MIN_RERANK = 0.90
MAX_PRECEDENTS = 2

_LABEL_RE = re.compile(r"^\s*\[([^\]]{1,24})\]")
# Match negated compounds before their positive substrings. English words are bounded
# so e.g. "incomplete"/"unresolved" never become "complete"/"resolved". This is a small
# label vocabulary, not a natural-language parser; the body is deliberately never read.
_POSITIVE_EN = r"(?:done|fixed|resolved|completed?|success(?:ful)?|succeeded|unblocked|recovered)"
# Negative outcome words. Review P3 (2026-09-23) added the tool-diagnosis vocabulary that
# CLAUDE.md singles out as "symptoms, not causes" (권한 없음, 접근 거부, 만료, 타임아웃, denied
# ...) - exactly the labels a premature "blocked" retain uses - plus 불완료/안됨/못함/포기/보류/에러.
# (?![율률]) keeps rates out ("실패율 감소", "오류율 개선"); 만료(?!일) keeps "만료일 확인" out;
# (?<![가-힣])안 keeps "보안됨" out (review 2nd pass).
_NEGATIVE_KO = (r"(?:미|불)(?:완료|해결|복구|성공)|차단|불가|막힘|실패(?![율률])|권한\s*없음|접근\s*거부|"
                r"거부|만료(?!일)|타임아웃|시간\s*초과|(?<![가-힣])안\s*됨|못\s*함|포기|보류|"
                r"에러(?![율률])|오류(?![율률])")
_OUTCOME_RE = re.compile(
    # "<negative word> 아님" negates the negative ("차단 아님", "실패 아님"): tried first so the
    # bare negative word at the same position does not win.
    # "<negative> 아님/없음" negates the negative ("차단 아님", "만료 아님", "에러 없음").
    r"(?P<positive_negated>(?:" + _NEGATIVE_KO + r"|timeout|denied|blocked)\s*(?:아님|없음))"
    r"|(?P<negative>" + _NEGATIVE_KO + r"|"
    r"\b(?:(?:not|never|cannot|can['’]t|failed\s+to|unable\s+to)\s+"
    r"(?:yet\s+)?(?:be\s+)?" + _POSITIVE_EN + r"|"
    r"incomplete|unresolved|unsuccessful|blocked|impossible|stuck|cannot|failed|failure|"
    r"denied|forbidden|expired|timeout|timed\s+out)\b)"
    # 성공률 is a rate, not an outcome ("성공률 낮음" is not a success).
    r"|(?P<positive>완료|성공(?!률)|해결|해제|해소|복구|돌파|우회|"
    r"\b(?:(?:not|no\s+longer)\s+(?:blocked|stuck)|" + _POSITIVE_EN + r")\b)"
)
_POSITIVE_KINDS = frozenset({"kind:completion", "kind:fix"})

WARNING = ("[gateway 경고] 반대 결론의 전례 있음 - 차단 판정 전 이 경로를 실제로 시도했는가? "
           "저장 요청은 허용됐다. 비동기 작업이면 완료 상태도 확인하라. "
           "전례가 맞으면 이 기억을 정정 retain으로 갱신하고, 세상이 바뀐 것이면 "
           "그 근거(무엇이 언제 바뀌었나)를 덧붙여라.")


def min_rerank() -> float:
    try:
        return float(os.environ.get("HC_CONTRADICTION_MIN_RERANK", DEFAULT_MIN_RERANK))
    except ValueError:
        return DEFAULT_MIN_RERANK


def label_of(content: Any) -> str:
    if not isinstance(content, str):
        return ""
    m = _LABEL_RE.match(content)
    return m.group(1).strip().lower() if m else ""


def _label_outcome(content: Any) -> Optional[str]:
    outcome = None
    for match in _OUTCOME_RE.finditer(label_of(content)):
        outcome = "negative" if match.lastgroup == "negative" else "positive"
    return outcome


def is_negative(content: Any) -> bool:
    return _label_outcome(content) == "negative"


def is_positive_precedent(memory: Dict[str, Any]) -> bool:
    outcome = _label_outcome(memory.get("text"))
    if outcome is not None:
        return outcome == "positive"
    tags = memory.get("tags")
    return isinstance(tags, list) and any(t in _POSITIVE_KINDS for t in tags)


def find_precedents(content: str, results: Sequence[Dict[str, Any]], *,
                    threshold: Optional[float] = None) -> List[Dict[str, Any]]:
    """Positive-polarity memories similar enough to the negative retain, best first."""
    thr = min_rerank() if threshold is None else threshold
    norm = " ".join(content.split())
    out = []
    for mem in results:
        text = mem.get("text")
        if not isinstance(text, str) or " ".join(text.split()) == norm:
            continue
        if rerank_score(mem) < thr or not is_positive_precedent(mem):
            continue
        out.append(mem)
    out.sort(key=rerank_score, reverse=True)
    return out[:MAX_PRECEDENTS]


def _date(mem: Dict[str, Any]) -> str:
    for key in ("occurred_start", "mentioned_at", "created_at"):
        v = mem.get(key)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    return "날짜 미상"


def warning_text(precedents: Sequence[Dict[str, Any]], *, max_chars: int = 320) -> str:
    lines = [WARNING]
    for mem in precedents:
        # Scrub before clipping: truncating first can leave a credential prefix that
        # no longer matches the full secret shape and still leaks through the warning.
        body = scrub_secrets(" ".join(str(mem.get("text", "")).split()))
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "…"
        lines.append(f"- 전례 (id:{str(mem.get('id', ''))[:8]}, {_date(mem)} 관측, "
                     f"rerank {rerank_score(mem):.2f}): {body}")
    return "\n".join(lines)
