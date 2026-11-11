"""런 5 시간여행 라벨셋(R5-A1, issue #361, 상위 #360 "결정됨"의 "라벨셋(R5-A1)" 절).

GitHub 이슈 상태를 정답으로, D10 사본 뱅크 기억에 실제로 나오는 작업 키 `(repo, n)`마다 기준일 t 의 라벨을 만든다.

  r5_labels.py github --aliases <json> --out <json> [--policy <json>] [--meta <jsonl>]
  r5_labels.py meta   --bank <id> --aliases <json> --out <jsonl> [--policy <json>]   (hc-next 사본만, 읽기 전용)
  r5_labels.py build  --meta <jsonl> --github <json> --out-dir <dir> [--setc <json>] [--setd <json>]

입력과 출력(원문은 전부 리포 밖 0600 파일, 리포 산출물에는 수치·sha256·코드만):
  * 정답 리포 목록: 운영 뱅크 정책 `item_repo_aliases`(--policy, 정책 파일에서 repo 값만 읽는다)와 런 2~4 실험 별칭 표
    (--aliases)의 정규 repo 합집합 중 `gh api repos/<owner>/<repo>` 가 200 인 리포. 소유자는 `Bkankim` 먼저, 404 면
    기억의 `<owner>/<repo>#<n>` 표기(--meta 의 owner_refs)에서 찾고, 그래도 없으면 제외하고 센다.
  * github: 리포마다 이슈 목록(`issues?state=all`, 쪽 단위) + 키로 쓰이는 닫힌 이슈의 `issues/<n>/events`(재오픈 확인).
    `--out` 파일에 캐시하고, 다시 부르면 이미 받은 것은 건너뛴다. 제목·본문은 저장하지 않는다. GET 만 보낸다.
  * meta: 사본 뱅크 world·experience 기억 전수(`memories/list`, scripts/item-backfill.py 와 같은 방식, 컨테이너 안,
    키는 stdin). 한 줄 = id, fact_type, state, mentioned_at, tags, keys, completion, owner_refs, document_id. 본문은 키를
    뽑는 데만 쓰고 저장하지 않는다. R5-A2 가 이 파일을 다시 쓴다.
  * build: 라벨셋 JSONL(한 줄 = 키, t, 분류, 분할, fold) + 요약 JSON(코드화한 수치). 세트 C·D 는 일치율에만 쓴다.

라벨 규칙(#360 결정됨):
  * 키: gateway/item_keys.py 의 `extract_keys` 만 import 한다(키 정의). 완료 계열·"기억 있음" 규칙은 이 모듈에 따로
    둔다. S1 원장의 완료 판정(게이트웨이 쪽)과 서로 import 하지 않는다 - 측정이 처치 규칙 쪽으로
    기울지 않게 한다.
  * 이슈 제외: `pull_request` 필드가 있는 레코드(PR), 재오픈 이력(events 의 `reopened` 또는 지금 state_reason
    `reopened`), `not_planned` 로 닫힘, 그 밖의 사유(`duplicate`·빈 값)로 닫힘. 제외된 이슈는 모든 t 에서 라벨이 없다.
  * 끝남(t): state_reason=completed 이고 closed_at <= t 끝 시각. 열림(t): created_at <= t 끝 시각이고 아직 열렸거나
    closed_at > t 끝 시각. t 끝 시각 = t 의 23:59:59.999999 KST.
  * 양성 = (끝남, t) + t 끝 시각 이전에 그 키의 완료 계열 기억이 있음. 없으면 데이터층(#360 "없으면 데이터층(기억 없음)").
    데이터층 행은 하위 분류 `data_sub` 를 갖는다: `no_completion`(그 키를 말하는 기억은 있지만 완료 계열이 없음) /
    `no_mention`(그 키를 말하는 기억도 없음). 데이터층은 G 지표 분모에 들어가지 않는다(보고용).
    대조 = (열림, t) + t 끝 시각 이전에 그 키를 말하는 기억이 있음. 없으면 라벨이 아니다(대조 기억 없음으로 센다).
  * 동명 충돌: 같은 정규 repo 가 기억의 owner 표기에서 서로 다른 소유자로 나오거나, 기억의 owner 와 해석된 정답 소유자가
    다르면 그 repo 의 키는 모두 뺀다.
  * t: 2026-09-01 부터 3일 간격, 상한 = 기억의 마지막 mentioned_at 의 KST 날짜(그 날짜 포함). 고유 양성 키가 60 미만이면
    같은 3일 격자를 가장 이른 기억 날짜까지 뒤로 늘린다.
  * 분할(#360 "봉인은 키로도 나눈다" 20:13 개정): 봉인 구간 = 상한 직전 8일([상한-7일, 상한]). 봉인 구간에 쌍이 있는 키를
    층으로 나눈다 - 봉인 구간에 양성 쌍이 하나라도 있으면 양성 층, 아니면 대조 쌍이 있으면 대조 층(두 층에 다 걸리면 양성
    층, 데이터층 쌍만 있는 키는 층 밖이라 봉인 후보가 아니다). 층마다 키를 정렬해 씨앗 SEAL_SEED 로 섞고 앞에서
    floor(n x 0.3 + 0.5)개(층이 비지 않으면 최소 1키)를 봉인 키로 둔다. 봉인 테스트 = 봉인 키의 봉인 구간 쌍(데이터층 포함).
    봉인 키의 다른 t 는 쓰지 않는다. dev = 봉인 키가 아닌 키의 쌍 중 t <= 봉인 시작 - 5일. 간격(봉인 시작 5일 전 ~ 봉인
    시작 전)과 봉인 구간의 dev 키 쌍은 쓰지 않는다. fold = 키의 정규 repo(프로젝트) 단위.
    개정 전 규칙(처음 라벨된 t 가 봉인 구간에 드는 키)은 D10 에서 봉인 7키·대조 0이라 봉인 G2 를 잴 수 없어 바꿨다.

이 모듈은 봉인 쌍의 recall·d-llm 결과를 만들지 않는다(라벨과 분할만).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import urllib.parse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:                      # `python bench/r5_labels.py` 로 실행해도 gateway·bench 를 찾게
    sys.path.insert(0, REPO)

from gateway.item_keys import extract_keys  # noqa: E402  (키 정의만. 완료 판정은 이 모듈 규칙)

KST = timezone(timedelta(hours=9))
T_START = date(2026, 9, 1)
T_STEP_DAYS = 3
SEAL_DAYS = 8
GAP_DAYS = 5
SEAL_SEED = 20261101
SEAL_FRACTION = 0.3
MIN_POSITIVE_KEYS = 60
DEFAULT_OWNER = "Bkankim"
FACT_TYPES = ("world", "experience")

# 완료 계열 태그(라벨 쪽 정의). 이름 자체가 "끝남"을 말하는 kind 만 센다. 뱅크 tag_aliases 가 완료로 접는 넓은 kind
# (작업 기록류)는 넣지 않는다 - 라벨이 처치(S1)의 별칭 규칙을 따라가지 않게 한다.
COMPLETION_TAGS = frozenset({"kind:completion", "kind:done", "kind:task-done", "kind:task-completion"})

LABEL_POSITIVE = "positive"
LABEL_CONTROL = "control"
LABEL_DATA_LAYER = "data_layer"
LABEL_CONTROL_NO_MEMORY = "control_no_memory"      # 라벨 아님(대조 조건 미달), 센다
UNLABELED = (LABEL_CONTROL_NO_MEMORY,)
DATA_NO_COMPLETION = "no_completion"               # 데이터층 하위: 언급 기억은 있고 완료 계열 기억이 없음
DATA_NO_MENTION = "no_mention"                     # 데이터층 하위: 그 키를 말하는 기억도 없음
LABELED = (LABEL_POSITIVE, LABEL_CONTROL, LABEL_DATA_LAYER)

_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_OWNER_REF_RE = re.compile(
    r"(?<![0-9A-Za-z_./-])([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*)#([1-9][0-9]{0,6})(?![0-9A-Za-z_])")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s,;]+")

Key = Tuple[str, int]


class Fail(Exception):
    pass


# --- 시각 -----------------------------------------------------------------------------------

def parse_instant(value: Any) -> Optional[datetime]:
    """ISO 시각 -> aware datetime(오프셋 없으면 UTC). 읽을 수 없으면 None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def t_end(day: date) -> datetime:
    """기준일 t 의 끝 시각(t 23:59:59.999999 KST). 마지막 1초 안의 소수 초 mentioned_at 도 그날로 친다(KST 날짜 기준과 같게)."""
    return datetime.combine(day, time(23, 59, 59, 999999), tzinfo=KST)


def kst_date(instant: datetime) -> date:
    return instant.astimezone(KST).date()


def t_grid(upper: date, start: date = T_START, step: int = T_STEP_DAYS) -> List[date]:
    """start 부터 step 일 간격, upper 이하(포함). start 가 upper 보다 늦으면 빈 목록."""
    out, day = [], start
    while day <= upper:
        out.append(day)
        day += timedelta(days=step)
    return out


def extended_start(earliest: date, anchor: date = T_START, step: int = T_STEP_DAYS) -> date:
    """같은 격자(anchor 기준 step 일)를 earliest 이후 첫 격자점까지 뒤로 늘린 시작일."""
    if earliest >= anchor:
        return anchor
    back = (anchor - earliest).days // step
    return anchor - timedelta(days=back * step)


# --- 기억 쪽 규칙("기억 있음") -------------------------------------------------------------------

def _tags(tags: Any) -> FrozenSet[str]:
    if isinstance(tags, (str, bytes)) or not isinstance(tags, Iterable):
        return frozenset()
    return frozenset(t.strip().lower() for t in tags if isinstance(t, str) and t.strip())


def is_done_family(tags: Any) -> bool:
    """완료 계열 기억인가: 태그에 COMPLETION_TAGS 중 하나가 있다(대소문자 무시). 본문·라벨은 보지 않는다."""
    return bool(_tags(tags) & COMPLETION_TAGS)


def has_memory(mentions: Sequence[Tuple[datetime, bool]], cutoff: datetime, *, completion: bool) -> bool:
    """"기억 있음" 판정. `mentions` = 그 키를 말하는 유효 기억의 (mentioned_at, 완료 계열 여부) 목록.

    completion=True(양성 조건): mentioned_at <= cutoff 인 완료 계열 기억이 하나 이상.
    completion=False(대조 조건): mentioned_at <= cutoff 인 기억(종류 무관)이 하나 이상.
    mentioned_at 이 없거나 읽을 수 없는 기억은 meta 단계에서 빠진다(시각을 알 수 없으면 "t 이전"을 말할 수 없다).
    """
    return any(at <= cutoff and (done or not completion) for at, done in mentions)


def canonical_repo(name: str, aliases: Mapping[str, str]) -> str:
    low = name.strip().lower()
    if low in aliases:
        return aliases[low]
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", low)).strip("-")
    return aliases.get(slug, slug)


def owner_refs(content: Any, aliases: Mapping[str, str]) -> List[Tuple[str, str]]:
    """본문의 `<owner>/<repo>#<n>` 표기에서 (owner 소문자, 정규 repo). URL 안 표기는 보지 않는다."""
    if not isinstance(content, str) or "#" not in content:
        return []
    text = _URL_RE.sub(" ", content)
    out = []
    for m in _OWNER_REF_RE.finditer(text):
        repo = canonical_repo(m.group(2), aliases)
        if repo:
            out.append((m.group(1).lower(), repo))
    return sorted(set(out))


def memory_meta(mem: Mapping[str, Any], aliases: Mapping[str, str]) -> Dict[str, Any]:
    """memories/list 행 하나 -> 메타 한 줄(본문 없음)."""
    text = mem.get("text") if isinstance(mem.get("text"), str) else mem.get("content")
    tags = sorted(_tags(mem.get("tags")))
    keys = sorted(extract_keys(text, tags, aliases))
    return {
        "id": mem.get("id"),
        "fact_type": mem.get("fact_type", mem.get("type")),
        "state": mem.get("state"),
        "mentioned_at": mem.get("mentioned_at"),
        "document_id": mem.get("document_id"),
        "tags": tags,
        "keys": ["%s#%d" % k for k in keys],
        "completion": is_done_family(tags),
        "owner_refs": [list(r) for r in owner_refs(text, aliases)],
    }


def parse_key(text: str) -> Key:
    repo, _, n = text.rpartition("#")
    return repo, int(n)


def key_text(key: Key) -> str:
    return "%s#%d" % key


def key_mentions(meta: Iterable[Mapping[str, Any]]) -> Tuple[Dict[Key, List[Tuple[datetime, bool]]], Counter]:
    """유효(state=valid) 기억 메타 -> {키: [(mentioned_at, 완료 계열)]}. 뺀 행 사유를 센다."""
    out: Dict[Key, List[Tuple[datetime, bool]]] = defaultdict(list)
    skipped: Counter = Counter()
    for row in meta:
        if row.get("state") != "valid":
            skipped["not_valid"] += 1
            continue
        at = parse_instant(row.get("mentioned_at"))
        if at is None:
            skipped["no_mentioned_at"] += 1
            continue
        for k in row.get("keys") or []:
            out[parse_key(k)].append((at, bool(row.get("completion"))))
    return dict(out), skipped


# --- 이슈 쪽 규칙 ------------------------------------------------------------------------------

def issue_exclusion(issue: Mapping[str, Any], reopened: bool) -> Optional[str]:
    """라벨에서 뺄 이슈면 사유, 아니면 None. 순서: PR > 재오픈 > not_planned > 그 밖의 닫힘 사유."""
    if issue.get("pull_request"):
        return "pull_request"
    reason = issue.get("state_reason")
    if reopened or reason == "reopened":
        return "reopened"
    if issue.get("state") == "closed" or issue.get("closed_at"):
        if reason == "not_planned":
            return "not_planned"
        if reason != "completed":
            return "closed_other"
    return None


def issue_state_at(issue: Mapping[str, Any], cutoff: datetime) -> Optional[str]:
    """제외되지 않은 이슈의 cutoff 시점 상태: "done" | "open" | None(아직 없음 또는 시각을 읽을 수 없음)."""
    created = parse_instant(issue.get("created_at"))
    if created is None or created > cutoff:
        return None
    closed = parse_instant(issue.get("closed_at"))
    if closed is not None and closed <= cutoff:
        return "done" if issue.get("state_reason") == "completed" else None
    return "open"


def collision_repos(meta: Iterable[Mapping[str, Any]], resolved: Mapping[str, Optional[str]]) -> Dict[str, str]:
    """동명 충돌 repo -> 사유. 기억 owner 표기가 둘 이상(memory_owners)이거나, 기억 owner 가 해석된 정답 소유자와 다르다
    (resolved_owner). resolved = {정규 repo: 정답 소유자(소문자 비교) 또는 None}."""
    owners: Dict[str, Set[str]] = defaultdict(set)
    for row in meta:
        for owner, repo in row.get("owner_refs") or []:
            owners[repo].add(owner.lower())
    out = {}
    for repo, seen in owners.items():
        if len(seen) > 1:
            out[repo] = "memory_owners"
        else:
            got = resolved.get(repo)
            if got and got.lower() not in seen:
                out[repo] = "resolved_owner"
    return out


# --- 라벨 ------------------------------------------------------------------------------------

def label_pairs(mentions: Mapping[Key, Sequence[Tuple[datetime, bool]]], issues: Mapping[Key, Mapping[str, Any]],
                grid: Sequence[date]) -> List[Dict[str, Any]]:
    """(키, t) 분류 행. issues 는 제외 규칙을 통과한 이슈만 담는다(호출자가 거른다). 라벨도 대조 미달도 아닌 t 는 행이 없다.

    행: {"key", "t", "label"}(+ 데이터층이면 "data_sub"); label 은 positive / data_layer / control / control_no_memory.
    """
    rows = []
    for key in sorted(issues):
        issue = issues[key]
        seen = mentions.get(key, ())
        for day in grid:
            cutoff = t_end(day)
            state = issue_state_at(issue, cutoff)
            row = {"key": key_text(key), "t": day.isoformat()}
            if state == "done":
                if has_memory(seen, cutoff, completion=True):
                    row["label"] = LABEL_POSITIVE
                else:
                    row["label"] = LABEL_DATA_LAYER
                    row["data_sub"] = (DATA_NO_COMPLETION if has_memory(seen, cutoff, completion=False)
                                       else DATA_NO_MENTION)
            elif state == "open":
                row["label"] = LABEL_CONTROL if has_memory(seen, cutoff, completion=False) else LABEL_CONTROL_NO_MEMORY
            else:
                continue
            rows.append(row)
    return rows


def holdout(keys: Iterable[str], seed: int, fraction: float, rng: random.Random) -> Set[str]:
    """정렬한 키를 rng 로 섞어 앞 floor(n x fraction + 0.5)개(n > 0 이면 최소 1). seed 는 호출자가 rng 를 만들 때 쓴다."""
    pool = sorted(keys)
    if not pool:
        return set()
    rng.shuffle(pool)
    return set(pool[:max(1, int(len(pool) * fraction + 0.5))])


def split(rows: Sequence[Mapping[str, Any]], upper: date, seed: int = SEAL_SEED,
          fraction: float = SEAL_FRACTION) -> Dict[str, Any]:
    """봉인·dev 분할(모듈 머리 주석). rows 는 label_pairs 출력. 각 행에 "split"(dev / sealed / unused)과 "fold"(정규 repo)를
    붙인 새 행. strata = 층별 후보 키 수."""
    seal_start = upper - timedelta(days=SEAL_DAYS - 1)
    dev_last = seal_start - timedelta(days=GAP_DAYS)
    in_window: Dict[str, Set[str]] = defaultdict(set)
    for r in rows:
        if seal_start <= date.fromisoformat(r["t"]) <= upper:
            in_window[r["key"]].add(r["label"])
    pos = {k for k, labs in in_window.items() if LABEL_POSITIVE in labs}
    ctl = {k for k, labs in in_window.items() if LABEL_CONTROL in labs} - pos
    rng = random.Random(seed)
    sealed_keys = holdout(pos, seed, fraction, rng) | holdout(ctl, seed, fraction, rng)
    out = []
    for r in rows:
        day = date.fromisoformat(r["t"])
        if r["key"] in sealed_keys:
            part = "sealed" if seal_start <= day <= upper else "unused"
        else:
            part = "dev" if day <= dev_last else "unused"
        out.append(dict(r, split=part, fold=parse_key(r["key"])[0]))
    dev_keys = {r["key"] for r in out if r["split"] == "dev" and r["label"] in LABELED}
    return {"seal_start": seal_start, "dev_last": dev_last, "rows": out, "sealed_keys": sealed_keys,
            "dev_keys": dev_keys, "strata": {"positive": len(pos), "control": len(ctl)}}


def counts(rows: Sequence[Mapping[str, Any]], part: str) -> Dict[str, Any]:
    sel = [r for r in rows if r["split"] == part]
    out: Dict[str, Any] = {}
    for label in LABELED + UNLABELED:
        hit = [r for r in sel if r["label"] == label]
        out[label] = {"pairs": len(hit), "keys": len({r["key"] for r in hit})}
    for sub in (DATA_NO_COMPLETION, DATA_NO_MENTION):
        hit = [r for r in sel if r["label"] == LABEL_DATA_LAYER and r.get("data_sub") == sub]
        out["data_layer_" + sub] = {"pairs": len(hit), "keys": len({r["key"] for r in hit})}
    out["folds"] = len({r["fold"] for r in sel if r["label"] in LABELED})
    return out


# --- 세트 C·D 일치율 ---------------------------------------------------------------------------

def labeled_keys(rows: Sequence[Mapping[str, Any]], part: Optional[str] = None) -> Set[str]:
    """라벨(양성·대조·데이터층)을 받은 키. part 를 주면 그 분할(dev / sealed)의 행만 본다. 일치율은 dev 키만 쓴다
    (봉인 키를 라벨 규칙 수정 판단에 쓰지 않는다, final-review #361-1)."""
    return {r["key"] for r in rows if r["label"] in LABELED and (part is None or r.get("split") == part)}


SET_STATE = {"closed": "done", "controls": "open"}


def state_at(key: Key, issue: Optional[Mapping[str, Any]], excluded: Mapping[Key, str], cutoff: datetime) -> str:
    """일치율용 라벨 쪽 상태: done / open / 제외 사유(excluded:<사유>) / no_issue / not_created."""
    if key in excluded:
        return "excluded:" + excluded[key]
    if issue is None:
        return "no_issue"
    return issue_state_at(issue, cutoff) or "not_created"


def agreement(set_doc: Mapping[str, Any], key_state: Callable[[str], Optional[str]], labeled: Set[str],
              task_keys: Callable[[Mapping[str, Any]], Set[str]]) -> Dict[str, Any]:
    """세트 C·D(closed=끝남, controls=열림)와 라벨 규칙의 기준 시각 상태 비교.

    key_state(norm_key) -> done / open / 그 밖의 문자열(겹침 밖 사유) / None(라벨셋 키 아님). labeled 는 라벨셋에 한 번이라도
    라벨된 키(norm_key 형)다. 겹침 = 키가 라벨셋에 있고 상태가 done·open 인 항목. 일치 = 항목 상태와 같음.
    한 항목의 겹친 키 상태가 섞이면 불일치(multi_key_mixed).
    """
    n_overlap = n_match = 0
    reasons: Counter = Counter()
    outside: Counter = Counter()
    disagree: List[Dict[str, str]] = []
    for part, want in SET_STATE.items():
        for item in set_doc.get(part) or []:
            keys = sorted(task_keys(item))
            if not keys:
                outside["no_key"] += 1
                continue
            states = {k: key_state(k) for k in keys}
            hits = {k: s for k, s in states.items() if k in labeled and s in ("done", "open")}
            if not hits:
                why = sorted({s or "not_in_labelset" for s in states.values()})
                outside["|".join(why)] += 1
                continue
            n_overlap += 1
            got = set(hits.values())
            if got == {want}:
                n_match += 1
                continue
            reason = "multi_key_mixed" if len(got) > 1 else "%s_vs_%s" % (want, got.pop())
            reasons[reason] += 1
            disagree.append({"id": str(item.get("id")), "reason": reason})
    rate = (n_match / n_overlap) if n_overlap else None
    return {"overlap": n_overlap, "match": n_match, "rate": rate, "reasons": dict(reasons),
            "outside": dict(outside), "disagree": disagree}


# --- 정답 리포 목록 ----------------------------------------------------------------------------

def load_alias_table(path: str) -> Tuple[Dict[str, str], str]:
    """별칭 표 JSON({alias: repo} 또는 `item_repo_aliases` 를 가진 객체) -> (소문자 표, 파일 sha256)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    value = json.loads(raw)
    if isinstance(value, dict) and isinstance(value.get("item_repo_aliases"), dict):
        value = value["item_repo_aliases"]
    if not isinstance(value, dict):
        raise Fail("%s: alias table must be a JSON object" % path)
    table = {}
    for k, v in value.items():
        if not (isinstance(k, str) and isinstance(v, str) and _SLUG_RE.match(k.lower()) and _SLUG_RE.match(v.lower())):
            raise Fail("%s: an alias pair is not a slug pair" % path)
        table[k.lower()] = v.lower()
    return table, hashlib.sha256(raw).hexdigest()


def policy_repos(path: Optional[str]) -> Tuple[Dict[str, str], int]:
    """운영 정책 파일에서 `item_repo_aliases` 만 읽는다(뱅크마다, 다른 키는 보지 않는다). -> (합친 표, 표를 가진 뱅크 수)."""
    if not path:
        return {}, 0
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    banks = doc.get("banks") if isinstance(doc, dict) and isinstance(doc.get("banks"), dict) else {"": doc}
    table, n = {}, 0
    for pol in banks.values():
        got = pol.get("item_repo_aliases") if isinstance(pol, dict) else None
        if isinstance(got, dict):
            n += 1
            for k, v in got.items():
                if isinstance(k, str) and isinstance(v, str) and _SLUG_RE.match(v.lower()):
                    table[k.lower()] = v.lower()
    return table, n


def merged_aliases(policy: Mapping[str, str], experiment: Mapping[str, str]) -> Tuple[Dict[str, str], int]:
    """정책 표가 이긴다(같은 별칭이 다른 repo 를 가리키면 정책 값). -> (표, 충돌 수)."""
    out = dict(experiment)
    conflicts = 0
    for k, v in policy.items():
        if k in out and out[k] != v:
            conflicts += 1
        out[k] = v
    return out, conflicts


# --- GitHub(읽기 전용) -------------------------------------------------------------------------

def gh_get(path: str) -> Tuple[int, Any]:
    """`gh api <path>`(GET). -> (상태, JSON). 404 는 (404, None). 그 밖의 실패는 Fail."""
    proc = subprocess.run(["gh", "api", "-X", "GET", path], capture_output=True, text=True)
    if proc.returncode == 0:
        return 200, json.loads(proc.stdout or "null")
    if "HTTP 404" in proc.stderr or "Not Found" in proc.stderr:
        return 404, None
    raise Fail("gh api %s failed: %s" % (path, proc.stderr.strip()[:200]))


ISSUE_FIELDS = ("number", "state", "state_reason", "created_at", "closed_at")


def slim_issue(raw: Mapping[str, Any]) -> Dict[str, Any]:
    out = {k: raw.get(k) for k in ISSUE_FIELDS}
    out["pull_request"] = bool(raw.get("pull_request"))
    return out


def fetch_issues(owner: str, repo: str, get: Callable[[str], Tuple[int, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """리포의 이슈 목록 전부(PR 포함, state=all, 쪽당 100). -> (가벼운 행, 호출 수)."""
    out, page, calls = [], 1, 0
    while True:
        status, body = get("repos/%s/%s/issues?state=all&per_page=100&page=%d" % (owner, repo, page))
        calls += 1
        if status != 200 or not isinstance(body, list):
            raise Fail("issue listing of a repo answered %s" % status)
        out.extend(slim_issue(x) for x in body)
        if len(body) < 100:
            return out, calls
        page += 1


def fetch_reopened(owner: str, repo: str, number: int, get: Callable[[str], Tuple[int, Any]]) -> Tuple[bool, int]:
    """이슈 하나의 events 에 `reopened` 가 있나. -> (재오픈, 호출 수)."""
    page, calls = 1, 0
    while True:
        status, body = get("repos/%s/%s/issues/%d/events?per_page=100&page=%d" % (owner, repo, number, page))
        calls += 1
        if status != 200 or not isinstance(body, list):
            raise Fail("issue events answered %s" % status)
        if any(isinstance(e, dict) and e.get("event") == "reopened" for e in body):
            return True, calls
        if len(body) < 100:
            return False, calls
        page += 1


def read_meta(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def write_private(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cmd_github(args, get=gh_get) -> int:
    exp, exp_sha = load_alias_table(args.aliases)
    pol, pol_banks = policy_repos(args.policy)
    aliases, conflicts = merged_aliases(pol, exp)
    repos = sorted(set(aliases.values()))
    meta = read_meta(args.meta) if args.meta else None
    state: Dict[str, Any] = {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            state = json.load(fh)
    state.update({"aliases_sha256": exp_sha, "policy_alias_banks": pol_banks, "policy_repos": len(set(pol.values())),
                  "alias_conflicts": conflicts, "candidate_repos": len(repos)})
    state.setdefault("calls", 0)
    info = state.setdefault("repos", {})
    mem_owners: Dict[str, Set[str]] = defaultdict(set)
    for row in meta or []:
        for owner, repo in row.get("owner_refs") or []:
            mem_owners[repo].add(owner)
    try:                                    # 도중 Fail 이어도 받은 것·호출 수는 캐시에 남긴다(다시 부르면 건너뛴다)
        for repo in repos:
            cur = info.setdefault(repo, {})
            if cur.get("owner") is None and cur.get("tried_default") is None:
                status, _ = get("repos/%s/%s" % (DEFAULT_OWNER, repo))
                state["calls"] += 1
                cur["tried_default"] = status
                if status == 200:
                    cur["owner"] = DEFAULT_OWNER
            if cur.get("owner") is None and meta is not None:
                tried = set(cur.get("tried_memory") or ())    # 새 meta 에 새 owner 후보가 생기면 그것만 더 본다
                for owner in sorted(mem_owners.get(repo, set()) - tried):
                    status, _ = get("repos/%s/%s" % (owner, repo))
                    state["calls"] += 1
                    tried.add(owner)
                    if status == 200:
                        cur["owner"] = owner
                        break
                cur["tried_memory"] = sorted(tried)
            if cur.get("owner") and "issues" not in cur:
                cur["issues"], n = fetch_issues(cur["owner"], repo, get)
                state["calls"] += n
        if meta is not None:
            wanted: Dict[str, Set[int]] = defaultdict(set)
            for row in meta:
                for k in row.get("keys") or []:
                    repo, n = parse_key(k)
                    wanted[repo].add(n)
            for repo, cur in info.items():
                reo = cur.setdefault("reopened", {})
                for issue in cur.get("issues") or []:
                    n = issue["number"]
                    if n not in wanted.get(repo, ()) or issue["pull_request"] or str(n) in reo:
                        continue
                    if issue.get("closed_at") is None and issue.get("state_reason") != "reopened":
                        continue                    # 한 번도 닫힌 적 없는 열린 이슈는 재오픈 이력이 없다
                    reo[str(n)], c = fetch_reopened(cur["owner"], repo, n, get)
                    state["calls"] += c
    finally:
        sha = write_private(args.out, json.dumps(state, sort_keys=True, ensure_ascii=False))
    resolved = sum(1 for c in info.values() if c.get("owner"))
    print("github: candidate_repos=%d resolved=%d calls_total=%d sha256=%s" % (len(repos), resolved, state["calls"], sha))
    return 0


# --- meta(사본) -------------------------------------------------------------------------------

def _bank_config():
    spec = importlib.util.spec_from_file_location("bank_config", os.path.join(REPO, "scripts", "bank-config.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def list_memories(bc, container, key, bank, fact_type, page_size=500):
    """scripts/item-backfill.py list_memories 와 같은 방식: 모든 쪽, id 수가 total 과 맞아야 한다."""
    seen, offset, total = {}, 0, None
    while True:
        q = urllib.parse.urlencode([("type", fact_type), ("limit", page_size), ("offset", offset)])
        page = bc.upstream(container, key, "GET",
                           "/v1/default/banks/%s/memories/list?%s" % (urllib.parse.quote(bank, safe=""), q))
        if not isinstance(page.get("total"), int):
            raise Fail("memories/list answered without an integer total")
        total = page["total"] if total is None else total
        if page["total"] != total:
            raise Fail("memories/list total moved while paging")
        items = page.get("items") or []
        for it in items:
            if it.get("fact_type", it.get("type")) != fact_type:
                raise Fail("memories/list returned a row of another type")
            seen[it["id"]] = it
        offset += len(items)
        if not items or offset >= total:
            break
    if len(seen) != total:
        raise Fail("memories/list ids do not add up to the total")
    return list(seen.values())


def cmd_meta(args) -> int:
    bc = _bank_config()
    aliases, sha = load_alias_table(args.aliases)
    pol, _ = policy_repos(args.policy)
    aliases, _ = merged_aliases(pol, aliases)
    container, is_copy, key = bc.resolve_target()
    if not is_copy:
        raise Fail("meta reads the hc-next copy only (HC_TARGET=next); nothing was read")
    rows = {}
    for ft in FACT_TYPES:
        for mem in list_memories(bc, container, key, args.bank, ft):
            rows.setdefault(mem["id"], memory_meta(mem, aliases))
    text = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows.values())
    digest = write_private(args.out, text)
    keyed = sum(1 for r in rows.values() if r["keys"])
    print("meta: memories=%d keyed=%d completion=%d aliases_sha256=%s sha256=%s" % (
        len(rows), keyed, sum(1 for r in rows.values() if r["completion"]), sha, digest))
    return 0


# --- build ------------------------------------------------------------------------------------

def bounds(meta: Sequence[Mapping[str, Any]]) -> Tuple[date, date]:
    days = [kst_date(at) for at in (parse_instant(r.get("mentioned_at")) for r in meta
                                     if r.get("state") == "valid") if at is not None]
    if not days:
        raise Fail("no valid memory with a readable mentioned_at")
    return min(days), max(days)


def build(meta: Sequence[Mapping[str, Any]], gh: Mapping[str, Any],
          start: date = T_START) -> Dict[str, Any]:
    """meta + github 캐시 -> 분류 행·분할·제외 수. start 는 t 격자 시작(늘리기는 호출자가 판단)."""
    earliest, upper = bounds(meta)
    mentions, skipped = key_mentions(meta)
    info = gh.get("repos") or {}
    resolved = {repo: c.get("owner") for repo, c in info.items()}
    collisions = collision_repos(meta, resolved)
    excluded: Dict[Key, str] = {}
    issues: Dict[Key, Mapping[str, Any]] = {}
    key_reason: Counter = Counter()
    for key in sorted(mentions):
        repo, n = key
        cur = info.get(repo)
        if cur is None:
            key_reason["repo_not_in_answer_list"] += 1
            continue
        if not cur.get("owner"):
            key_reason["repo_unresolved"] += 1
            continue
        if repo in collisions:
            excluded[key] = "collision"
            key_reason["collision"] += 1
            continue
        found = next((i for i in cur.get("issues") or [] if i["number"] == n), None)
        if found is None:
            key_reason["no_issue"] += 1
            continue
        reo = cur.get("reopened") or {}
        if not found.get("pull_request") and found.get("closed_at") is not None and str(n) not in reo:
            raise Fail("reopen events were not fetched for a keyed closed issue; run `github --meta` with this meta first")
        why = issue_exclusion(found, bool(reo.get(str(n))))
        if why:
            excluded[key] = why
            key_reason[why] += 1
            continue
        issues[key] = found
    grid = t_grid(upper, start)
    rows = label_pairs(mentions, issues, grid)
    parts = split(rows, upper)
    return {"earliest": earliest, "upper": upper, "grid": grid, "rows": parts["rows"], "parts": parts,
            "mentions": mentions, "skipped": skipped, "issues": issues, "excluded": excluded,
            "key_reason": key_reason, "collisions": collisions}


def positive_keys(rows: Sequence[Mapping[str, Any]]) -> int:
    return len({r["key"] for r in rows if r["label"] == LABEL_POSITIVE})


def repo_codes(rows: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """라벨된 키 수 내림차순(동률은 이름) -> r01.. 코드."""
    per = Counter()
    for k in {r["key"] for r in rows if r["label"] in LABELED}:
        per[parse_key(k)[0]] += 1
    order = sorted(per, key=lambda r: (-per[r], r))
    return {repo: "r%02d" % (i + 1) for i, repo in enumerate(order)}


def cmd_build(args) -> int:
    from bench.d_llm_match import norm_key, task_keys  # 동결 모듈, import 만
    meta = read_meta(args.meta)
    with open(args.github, encoding="utf-8") as fh:
        gh = json.load(fh)
    res = build(meta, gh)
    extended = False
    if positive_keys(res["rows"]) < MIN_POSITIVE_KEYS:
        start = extended_start(res["earliest"])
        if start < T_START:
            res = build(meta, gh, start)
            extended = True
    rows, parts = res["rows"], res["parts"]
    overlap = parts["dev_keys"] & parts["sealed_keys"]
    if overlap:
        raise Fail("dev and sealed keys overlap (%d)" % len(overlap))
    codes = repo_codes(rows)
    for r in rows:
        r["fold"] = codes.get(r["fold"], r["fold"])
    out_dir = args.out_dir
    labelset = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    lab_path = os.path.join(out_dir, "r5-a1-labelset.jsonl")
    lab_sha = write_private(lab_path, labelset)
    write_private(os.path.join(out_dir, "r5-a1-repo-codes.json"), json.dumps(codes, sort_keys=True))

    # 세트 C·D 일치율(기준 시각 = 세트의 질의 날짜 끝 KST). 판정용은 dev 키만, 봉인 키 포함 값은 참고
    dev_norm = {norm_key(k) for k in labeled_keys(rows, "dev")}
    all_norm = {norm_key(k) for k in labeled_keys(rows)}
    by_norm: Dict[str, Key] = {}
    for key in set(res["issues"]) | set(res["excluded"]):
        by_norm[norm_key(key_text(key))] = key
    agree, agree_all = {}, {}
    for name, path in (("C", args.setc), ("D", args.setd)):
        if not path:
            continue
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        when = t_end(date.fromisoformat(doc.get("query_date") or "2026-10-28"))

        def key_state(nk, when=when):
            key = by_norm.get(nk)
            if key is None:
                return None
            return state_at(key, res["issues"].get(key), res["excluded"], when)
        agree[name] = agreement(doc, key_state, dev_norm, task_keys)
        agree[name]["reference"] = when.isoformat()
        agree_all[name] = agreement(doc, key_state, all_norm, task_keys)

    per_repo = Counter()
    for k in {r["key"] for r in rows if r["label"] in LABELED}:
        per_repo[codes[parse_key(k)[0]]] += 1
    excl_issue = Counter()
    for cur in (gh.get("repos") or {}).values():
        reo = cur.get("reopened") or {}
        for i in cur.get("issues") or []:
            why = issue_exclusion(i, bool(reo.get(str(i["number"]))))
            excl_issue[why or "kept"] += 1
    summary = {
        "t_start": res["grid"][0].isoformat() if res["grid"] else None, "t_extended": extended,
        "t_upper": res["upper"].isoformat(), "t_count": len(res["grid"]), "earliest_memory": res["earliest"].isoformat(),
        "seal_start": parts["seal_start"].isoformat(), "dev_last": parts["dev_last"].isoformat(),
        "dev": counts(rows, "dev"), "sealed": counts(rows, "sealed"), "unused": counts(rows, "unused"),
        "dev_sealed_key_overlap": len(overlap), "dev_keys": len(parts["dev_keys"]),
        "sealed_keys": len(parts["sealed_keys"]),
        "seal_strata": parts["strata"], "seal_seed": SEAL_SEED, "seal_fraction": SEAL_FRACTION,
        "keys_in_memory": len(res["mentions"]), "key_reason": dict(res["key_reason"]),
        "meta_skipped": dict(res["skipped"]), "memories": len(meta),
        "collision_repos": Counter(res["collisions"].values()), "issue_records": dict(excl_issue),
        "keys_per_repo": dict(sorted(per_repo.items())),
        "github_calls": gh.get("calls"), "candidate_repos": gh.get("candidate_repos"),
        "resolved_repos": sum(1 for c in (gh.get("repos") or {}).values() if c.get("owner")),
        "policy_alias_banks": gh.get("policy_alias_banks"), "policy_repos": gh.get("policy_repos"),
        "agreement": {n: {k: v for k, v in a.items() if k != "disagree"} for n, a in agree.items()},
        "disagree_ids": {n: a["disagree"] for n, a in agree.items()},
        "agreement_with_sealed_reference": {n: {k: v for k, v in a.items() if k != "disagree"}
                                            for n, a in agree_all.items()},
        "labelset_sha256": lab_sha,
    }
    sum_text = json.dumps(summary, sort_keys=True, default=str, indent=1)
    sum_sha = write_private(os.path.join(out_dir, "r5-a1-summary.json"), sum_text)
    print(sum_text)
    print("labelset: %s sha256=%s" % (os.path.basename(lab_path), lab_sha))
    print("summary sha256=%s" % sum_sha)
    return 0


def main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("github", help="answer repo list, issue lists, reopen events (GET only, cached)")
    g.add_argument("--aliases", required=True)
    g.add_argument("--policy")
    g.add_argument("--meta")
    g.add_argument("--out", required=True)
    m = sub.add_parser("meta", help="memory metadata of the hc-next copy (read only)")
    m.add_argument("--bank", required=True)
    m.add_argument("--aliases", required=True)
    m.add_argument("--policy")
    m.add_argument("--out", required=True)
    b = sub.add_parser("build", help="labels, split, set C/D agreement")
    b.add_argument("--meta", required=True)
    b.add_argument("--github", required=True)
    b.add_argument("--out-dir", required=True)
    b.add_argument("--setc")
    b.add_argument("--setd")
    args = ap.parse_args(argv)
    try:
        return {"github": cmd_github, "meta": cmd_meta, "build": cmd_build}[args.cmd](args)
    except Fail as e:
        print("r5_labels: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
