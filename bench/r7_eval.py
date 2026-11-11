"""런 7 표 키 전체 GitHub 대조 평가 하네스(R7-0, issue #394, 상위 #393 "결정됨"의 "R7-0 표 정밀도 정의").

S3 열린 키 표에 오른 **모든** 키를 기준일 t 시점 GitHub 상태와 대조한다. 지출 0, 사본 0, LLM 0. GitHub 는 GET 만.

  r7_eval.py ops-listing --bank <id> --out <jsonl>      운영 뱅크 memories/list GET 1회 -> 0600 JSONL(이미 있으면 재사용)
  r7_eval.py run --ops-store ... --dev-store ... --work <dir> [--offline]   표 재구성 -> GitHub 대조 -> 요약
  r7_eval.py offline --arm <라벨> <저장소> use|ignore ... --github-cache <R7-0 캐시> --work <dir> [--offline]
      R7-3(#397) 수집 켬·끔 판본 측정: v2 meta key_rules, GitHub 근거 테이블, 3상태 표(분모 = `열림 후보` 줄),
      줄 상태 x 범주, 미확인·충돌 사유, 하위군(GitHub 연결 키 / 없는 키), 알려진 한계 수, 저장소 감사.

무엇을:
  (a) GitHub 대조 캐시: 표에 오른 리포 + 별칭 표 정규 리포마다 `gh api --paginate --slurp
      repos/<owner>/<repo>/issues?state=all&per_page=100`(PR 포함, 404 면 `missing_repo`) 한 번과 리포 단위
      `issues/events`(closed·reopened 만 남긴다) 한 번. 저장 필드는 번호·종류(issue/pr)·상태·사유·생성·닫힘·머지 시각과
      이벤트(종류·시각·사유)뿐이다(제목·본문 없음). 이슈 레코드에 `pull_request` 가 있으면 PR 이다(머지 시각은 그 객체의
      `merged_at`, 없으면 `pulls?state=closed` 로 보충). 닫힌 적 있는 표 키 이슈의 이벤트가 리포 목록에 없으면
      `issues/<n>/events` 로 보충하고, 그래도 없으면 범주 `reopened_unknown`. 소유자는 런 5 캐시(`r5-a1-github.json`)가
      해석해 둔 값, 없으면 `DEFAULT_OWNER`. 호출 수(쪽 수)를 센다.
  (b) 표 재구성: 역할 저장소를 게이트웨이 공개 함수(`read_store`·`role_states`·`build_table`)로 읽어 기준일 t 의 뱅크 전체
      표(손잡이 scope all, recent_done_days 0, max_rows 1000, 나머지 기본값)를 만든다. 표 키 = `rows` 중 열림 키 전부.
      hindsight-crew 꼴 = 같은 표에서 repo 가 hindsight-crew 인 키. `role_states` 호출은 `store_view` 한 곳에 모으고
      저장소 meta 를 함께 돌려준다(R7-2 가 meta `key_rules` 를 넘기도록 확장한다). 저장소는 `mode=ro` 로만 연다.
  (c) 대조: 키마다 t 시점 범주(`CATEGORIES`). 이슈는 created_at <= t 이고 t 시점에 닫혀 있지 않으면 열림(재오픈·재닫힘은
      이벤트로), t 이후 생성은 `not_yet`. PR 은 언제나 `open_issue` 가 아니다. 표 정밀도 = open_issue / 표 키 전체.
      회수율 = t 시점 열린 이슈(PR 제외, 존재하는 리포) 중 표에 오른 것. 보조 회수율 분모 = 그중 저장소에 키가 있는 이슈.
  (d) 원인(표 오류 키 = open_issue 가 아닌 키): 1 범위 표기(결정 언급이 범위 양 끝에서만 나옴 `endpoint`, 또는 결정 뒤
      끝냄 범위 안 번호 `covered`) / 2 같은 시각 open·done 동점(기억 사이) / 3 이전 끝냄 뒤 open 언급이 상태를 정함 /
      4 번호만 규칙 키이고 외부 문맥 낱말이 있거나 그 리포 명시 표기 최대 번호를 넘음 / 5 PR 표기에서만 나온 키 /
      데이터층(GitHub 에서 끝났는데 끝난 시각 이후 뱅크 끝냄 언급이 없음) / 기타. 여럿이면 모두 세고 주 원인은
      `PRIMARY_ORDER` 첫 번째. 1·4·5 는 기억 본문으로 가른다(dev = 런 5 본문 덤프, 운영 = `ops-listing` 저장본).
      t 뒤 mentioned_at 기억은 쓰지 않는다.

봉인(상위 #393 제약): 표 정밀도는 표의 모든 키를 센다(봉인 키 목록을 읽지 않는다). 금지는 봉인 구간 t 와 봉인 저장물이다:
입력 경로 이름에 `sealed` 가 있거나, dev t 가 상한 `DEV_T_MAX` 뒤이거나, 운영 t 가 런 5 봉인 쪽 구간
(`DEV_T_MAX` 뒤 ~ `SEAL_LAST`)이면 exit 2. 별칭 표 정규 sha256 이 저장소 meta `aliases_sha256` 과 다르면 exit 3.

출력(리포 밖 `--work`, 0700/0600): `r7-0-github.json`(캐시), `r7-repo-codes.json`(리포 -> 코드, 런 5 코드 rNN 유지,
없는 리포는 이름순 x01..), `r7-0-details.json`(키별 범주·원인, 원문 키 목록), `r7-0-summary.json`(코드·수치만).
표준 출력은 수치·sha256 만.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from bench.r5_labels import DEFAULT_OWNER, parse_instant  # noqa: E402
from gateway import item_keys as IK  # noqa: E402
from gateway import item_open_table as OT  # noqa: E402
from gateway import item_roles as IR  # noqa: E402

KST = timezone(timedelta(hours=9))
DEV_TS = ("2026-09-28", "2026-10-01", "2026-10-04", "2026-10-07", "2026-10-10", "2026-10-13")   # R5-A2 dev t
DEV_T_MAX = date(2026, 10, 13)
SEAL_LAST = date(2026, 10, 28)
HC_REPO = "hindsight-crew"
KNOBS = {"scope": "all", "recent_done_days": 0, "max_rows": 1000}

CATEGORIES = ("open_issue", "closed_completed", "closed_not_planned", "pr_open", "pr_merged", "pr_closed",
              "missing_number", "missing_repo", "not_yet", "reopened_unknown")
FINISHED = frozenset({"closed_completed", "closed_not_planned", "pr_merged", "pr_closed"})
NOT_PLANNED_REASONS = frozenset({"not_planned", "duplicate"})
PRIMARY_ORDER = ("5", "1", "4", "2", "3", "data", "other")
CAUSES = PRIMARY_ORDER

# #395(R7-1) 원인 4·5 의 고정 표기와 같은 모양
EXTERNAL_WORDS = ("upstream", "업스트림", "외부 리포", "외부 저장소", "다른 리포", "타 리포")
_NUM = r"([1-9][0-9]{0,6})"
_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
RANGE_RE = re.compile(r"(?<![0-9A-Za-z_./&#-])(?:(" + _NAME + r")[ ]?)?#" + _NUM + r"\s*(?:~|-|\.\.)\s*(?:(" + _NAME +
                      r")[ ]?)?#" + _NUM + r"(?![0-9A-Za-z_])")
PR_RE = re.compile(r"(?<![0-9A-Za-z_])(?:pr|pull request|pull)[ ]?#" + _NUM + r"(?![0-9A-Za-z_])", re.I)
RANGE_CAP = 20
_DUMMY_PROJECTS = ["project:r7-dummy-a", "project:r7-dummy-b"]    # 번호만 규칙을 끄고 명시 표기만 남긴다


class SealGuard(Exception):
    pass


class Fail(Exception):
    pass


# --- 공통 ---------------------------------------------------------------------------------------------

def write_private(path: str, text: str) -> str:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def repo_of(key: str) -> str:
    return key.rpartition("#")[0]


def num_of(key: str) -> int:
    return int(key.rpartition("#")[2])


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


# --- 봉인 가드 ------------------------------------------------------------------------------------------

def guard_paths(paths: Iterable[Optional[str]]) -> None:
    for p in paths:
        if p and "sealed" in os.path.basename(os.path.normpath(p)).lower():
            raise SealGuard("a sealed input was given")


def guard_dev_t(day: date) -> None:
    if day > DEV_T_MAX:
        raise SealGuard("dev t %s is after the dev upper bound %s" % (day, DEV_T_MAX))


def guard_ops_t(day: date) -> None:
    if DEV_T_MAX < day <= SEAL_LAST:
        raise SealGuard("ops t %s falls in the run 5 sealed side window" % day)


# --- 표 재구성 ------------------------------------------------------------------------------------------

@dataclass
class StoreView:
    meta: Dict[str, str]
    states: Dict[str, OT.KeyState]
    by_key: Dict[str, List[Tuple[datetime, str, str]]]      # 비참조 언급(<= t, valid): (시각, 역할, 기억 id)
    mem_roles: Dict[str, Dict[str, str]]                     # <= t valid 기억의 {키: 역할}(ref 포함)
    mem_at: Dict[str, datetime]
    all_keys: Set[str]                                       # <= t valid 기억에 나온 키 전부(ref 포함)
    github: Optional[Dict[str, Dict[Any, Dict[str, Any]]]] = None   # #395 근거 테이블(손잡이 use 이고 테이블이 있을 때)
    key_rules: str = IR.KEY_RULES_V1                         # 저장소 meta key_rules(없으면 v1)
    max_numbers: Optional[Dict[str, int]] = None             # #412: 리포별 수집 최대 번호(github 가 있을 때, 근거 전체)


def store_view(path: str, cutoff: datetime, timeout_s: float = 60.0, github_mode: str = OT.GITHUB_USE) -> StoreView:
    """역할 저장소(읽기 전용) -> 기준 시각 cutoff 의 키 상태와 meta. `role_states` 호출은 여기 한 곳이다.

    R7-3(#397): meta `key_rules` 를 `role_states` 에 넘기고(v2 동점 = 끝남), github_mode 가 use 면 게이트웨이와 같은
    `read_store(..., GITHUB_ALL)` 로 근거 테이블을 읽는다(테이블이 없으면 None). ignore 면 읽지 않는다(게이트웨이 ignore 와 같다)."""
    if github_mode not in (OT.GITHUB_USE, OT.GITHUB_IGNORE):
        raise Fail("github_evidence must be use or ignore")
    store = OT.read_store(path, timeout_s, OT.GITHUB_ALL if github_mode == OT.GITHUB_USE else None)
    states = OT.role_states(store.mentions, cutoff, store.key_rules)
    by_key: Dict[str, List[Tuple[datetime, str, str]]] = defaultdict(list)
    for m in store.mentions:
        if m.at <= cutoff:
            by_key[m.key].append((m.at, m.role, m.memory_id))
    conn = OT.connect_ro(path)
    try:
        rows = conn.execute("SELECT r.memory_id, r.key, r.role, m.mentioned_at FROM mention_roles r "
                            "JOIN memories m ON m.memory_id = r.memory_id WHERE m.state = 'valid'").fetchall()
    finally:
        conn.close()
    mem_roles: Dict[str, Dict[str, str]] = defaultdict(dict)
    mem_at: Dict[str, datetime] = {}
    for mid, key, role, at_raw in rows:
        at = parse_instant(at_raw)
        if at is None or at > cutoff:
            continue
        mem_roles[mid][key] = role
        mem_at[mid] = at
    all_keys = {k for roles in mem_roles.values() for k in roles}
    max_numbers = OT.repo_max_numbers(IR.load_github(path)) if store.github is not None else None   # #410 공개 이름
    return StoreView(dict(store.meta), states, dict(by_key), dict(mem_roles), mem_at, all_keys, store.github,
                     store.key_rules, max_numbers)


def table_knobs() -> Dict[str, Any]:
    knobs = OT._defaults()
    knobs.update(KNOBS)
    return knobs


def table_keys(view: StoreView, cutoff: datetime) -> Tuple[List[str], int]:
    table = OT.build_table(view.states, cutoff, None, table_knobs())
    return [r[0] for r in table["rows"] if r[1] == OT.STATE_OPEN], table["truncated"]


def check_aliases(meta: Mapping[str, str], aliases: Mapping[str, str]) -> bool:
    return meta.get("aliases_sha256") == OT.aliases_sha256(aliases)


def load_aliases(path: str) -> Dict[str, str]:
    with open(path, encoding="utf-8") as fh:
        value = json.load(fh)
    if isinstance(value, dict) and isinstance(value.get("item_repo_aliases"), dict):
        value = value["item_repo_aliases"]
    if not isinstance(value, dict):
        raise Fail("aliases file must be a JSON object")
    return IR.normalize_aliases(value)


# --- R7-3 3상태 표(#397) ------------------------------------------------------------------------------------

FINISHED_ISSUE = frozenset({"closed_completed", "closed_not_planned"})
SLUG_SEPARATORS = ("_", ".")


def table3(view: StoreView, cutoff: datetime) -> Dict[str, Any]:
    """게이트웨이 `build_table(..., github=view.github, max_numbers=view.max_numbers)` 로 표를 만든다. github 가 있으면
    3상태 표다(#412: over-max 줄은 게이트웨이 기본 손잡이 drop 처럼 빠진다).
    keys = payload `keys`(3상태면 `열림 후보` 줄, 아니면 열린 키 전부) = R7-0 기준 표 정밀도 분모."""
    table = OT.build_table(view.states, cutoff, None, table_knobs(), github=view.github,
                           max_numbers=view.max_numbers)      # #412: 게이트웨이와 같은 over-max 판정(github 없으면 None)
    keys = OT.payload_table(table)["keys"]
    sha = hashlib.sha256(json.dumps({"rows": table["rows"], "text": table["text"]}, ensure_ascii=False,
                                    sort_keys=True).encode("utf-8")).hexdigest()
    return {"table": table, "keys": keys, "three_state": "counts" in table, "sha256": sha}


def row_categories(rows: Sequence[Sequence[str]], gh: Mapping[str, Any], t: datetime) -> Dict[str, Any]:
    """표 전체 줄의 줄 상태 x t 시점 GitHub 범주(보조 분포)와 미확인·충돌 사유별 수."""
    by: Dict[str, Counter] = {}
    reasons: Counter = Counter()
    for r in rows:
        key, state = r[0], r[1]
        cat, _ = item_at(gh.get(repo_of(key)), num_of(key), t)
        by.setdefault(state, Counter())[cat] += 1
        if state == OT.ROW_UNKNOWN:
            reasons[r[3] if len(r) > 3 else ""] += 1
    order = list(OT.ROW_STATES) + [OT.STATE_OPEN]
    states = sorted(by, key=lambda s: (order.index(s) if s in order else len(order), s))
    return {"rows": len(rows),
            "by_state": {s: {"rows": sum(by[s].values()),
                             "categories": {c: by[s][c] for c in CATEGORIES if by[s].get(c)}} for s in states},
            "unknown_reasons": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])))}


def linked(key: str, gh: Mapping[str, Any]) -> bool:
    """GitHub 연결 키 = 평가 캐시에 그 리포가 있다(200). 아니면 GitHub 없는 키(리포 없음 404·미확인)."""
    return (gh.get(repo_of(key)) or {}).get("status") == 200


def subgroup_metrics(keys: Sequence[str], gh: Mapping[str, Any], t: datetime) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for name, want in (("linked", True), ("no_github", False)):
        cats = Counter(item_at(gh.get(repo_of(k)), num_of(k), t)[0] for k in keys if linked(k, gh) is want)
        n = sum(cats.values())
        oi = cats.get("open_issue", 0)
        out[name] = {"keys": n, "open_issue": oi, "precision": _ratio(oi, n),
                     "categories": {c: cats[c] for c in CATEGORIES if cats.get(c)}}
    return out


def reopen_limit(rows: Sequence[Sequence[str]], gh: Mapping[str, Any], t: datetime) -> Dict[str, int]:
    """알려진 한계 (a)(#396 final-review #3): 근거는 현재 스냅샷이라 t 뒤 재오픈 이슈를 t 시점 열림으로 본다.
    candidate_closed_at_t = `열림 후보` 줄인데 평가(리포 단위 events)는 t 시점 닫힘, 그중 reopened_after_t = t 뒤 재오픈
    이벤트가 있음. finished_row_open_at_t = 종결 계열 줄인데 평가는 t 시점 열린 이슈(반대 방향)."""
    cand = reopened = finished_open = 0
    for r in rows:
        key, state = r[0], r[1]
        rec = gh.get(repo_of(key))
        cat, _ = item_at(rec, num_of(key), t)
        if state == OT.ROW_CANDIDATE and cat in FINISHED_ISSUE:
            cand += 1
            evs = ((rec or {}).get("events") or {}).get(str(num_of(key))) or []
            if any(e[0] == "reopened" and (parse_instant(e[1]) or t) > t for e in evs):
                reopened += 1
        elif state in (OT.ROW_CLOSED, OT.ROW_NOT_PLANNED) and cat == "open_issue":
            finished_open += 1
    return {"candidate_closed_at_t": cand, "reopened_after_t": reopened, "finished_row_open_at_t": finished_open}


def slug_limit(rows: Sequence[Sequence[str]]) -> Dict[str, int]:
    """알려진 한계 (b)(#395 final-review #8): 실명에 `_`·`.` 이 든 리포 slug 는 404(repo-missing)로 잡힐 수 있다.
    repo-missing 미확인 줄 중 리포 이름에 그 글자가 든 줄·리포 수(그 몫의 상한, 실제 리포 여부는 가리지 않는다)."""
    miss = [r for r in rows if r[1] == OT.ROW_UNKNOWN and len(r) > 3 and r[3] == "repo-missing"]
    sep = [r for r in miss if any(c in repo_of(r[0]) for c in SLUG_SEPARATORS)]
    return {"repo_missing_rows": len(miss), "with_sep_rows": len(sep),
            "with_sep_repos": len({repo_of(r[0]) for r in sep})}


def store_audit(path: str, view: StoreView, sha_before: str, work: str, mode: str) -> Dict[str, Any]:
    conn = OT.connect_ro(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    return {"file": os.path.basename(path), "mode": mode, "sha256_before": sha_before,
            "sha256_after": file_sha256(path), "key_rules": view.key_rules,
            "github_tables": {"github_items", "github_repos"} <= tables,
            "github_fetched_at": OT.github_fetched_at(view.github),
            "in_work_dir": os.path.realpath(os.path.dirname(os.path.abspath(path))) == os.path.realpath(work)}


# --- GitHub ---------------------------------------------------------------------------------------------

def slim_item(raw: Mapping[str, Any]) -> Dict[str, Any]:
    pr = raw.get("pull_request")
    out = {"kind": "pr" if pr is not None else "issue", "state": raw.get("state"), "state_reason": raw.get("state_reason"),
           "created_at": raw.get("created_at"), "closed_at": raw.get("closed_at"), "merged_at": None}
    if pr is not None:
        out["merged_at"] = pr.get("merged_at") if isinstance(pr, Mapping) else None
        out["merged_known"] = isinstance(pr, Mapping) and "merged_at" in pr
    return out


def slim_events(raw: Iterable[Mapping[str, Any]]) -> Dict[str, List[List[Any]]]:
    """리포 단위 issue events -> {이슈 번호: [[closed|reopened, 시각, 사유], ...] 시각순}. PR 이벤트는 뺀다."""
    out: Dict[str, List[List[Any]]] = defaultdict(list)
    for e in raw:
        if not isinstance(e, Mapping) or e.get("event") not in ("closed", "reopened"):
            continue
        issue = e.get("issue") or {}
        if issue.get("pull_request") is not None or not isinstance(issue.get("number"), int):
            continue
        out[str(issue["number"])].append([e["event"], e.get("created_at"), e.get("state_reason")])
    return {k: sorted(v, key=lambda x: x[1] or "") for k, v in out.items()}


GH_TIMEOUT_S = 60.0     # gh 호출 하나(쪽 넘김 포함)의 시한(issue #414). 넘기면 실행기 실패(`Fail`)


def gh_paged(path: str, timeout_s: float = GH_TIMEOUT_S) -> Tuple[int, List[Any], int]:
    """`gh api --paginate --slurp -X GET <path>` -> (상태, 항목 전부, 호출(쪽) 수). 404 는 (404, [], 1).
    `timeout_s` 초를 넘기면 `Fail`(issue #414: 운영 연막이 시한 없이 멈추지 않게)."""
    try:
        proc = subprocess.run(["gh", "api", "--paginate", "--slurp", "-X", "GET", path], capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise Fail("gh api timed out after %gs (%s)" % (timeout_s, path.split("?")[0]))
    if proc.returncode == 0:
        pages = json.loads(proc.stdout or "[]")
        items = [x for pg in pages for x in (pg if isinstance(pg, list) else [pg])]
        return 200, items, max(1, len(pages))
    if "HTTP 404" in proc.stderr or "Not Found" in proc.stderr:
        return 404, [], 1
    raise Fail("gh api failed (%s): %s" % (path.split("?")[0], proc.stderr.strip()[:200]))


def fetch_repo(owner: str, repo: str, get=gh_paged) -> Dict[str, Any]:
    status, items, calls = get("repos/%s/%s/issues?state=all&per_page=100" % (owner, repo))
    rec: Dict[str, Any] = {"owner": owner, "status": status, "calls": calls,
                           "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if status != 200:
        return rec
    rec["items"] = {str(x["number"]): slim_item(x) for x in items if isinstance(x, Mapping)}
    status, evs, c = get("repos/%s/%s/issues/events?per_page=100" % (owner, repo))
    rec["calls"] += c
    if status != 200:
        raise Fail("repo events answered %s" % status)
    rec["events"] = slim_events(evs)
    return rec


def fill_gaps(rec: Dict[str, Any], repo: str, wanted: Set[int], get=gh_paged) -> int:
    """표 키 중 닫힌 적 있는데 이벤트가 없는 이슈(-> issues/<n>/events)와 머지 시각이 없는 PR(-> pulls) 보충. 호출 수."""
    if rec.get("status") != 200:
        return 0
    calls = 0
    items, events = rec["items"], rec.setdefault("events", {})
    fetched = rec.setdefault("events_fetched", [])
    for n in sorted(wanted):
        it = items.get(str(n))
        if not it or it["kind"] != "issue" or str(n) in events or n in fetched:
            continue
        if it.get("closed_at") is None and it.get("state_reason") != "reopened":
            continue
        status, evs, c = get("repos/%s/%s/issues/%d/events?per_page=100" % (rec["owner"], repo, n))
        calls += c
        fetched.append(n)
        if status == 200:
            got = slim_events({**e, "issue": {"number": n}} for e in evs if isinstance(e, Mapping))
            if got.get(str(n)):
                events[str(n)] = got[str(n)]
    need_pulls = any(items.get(str(n), {}).get("kind") == "pr" and items[str(n)].get("closed_at")
                     and not items[str(n)].get("merged_known") for n in wanted)
    if need_pulls and not rec.get("pulls_fetched"):
        status, pulls, c = get("repos/%s/%s/pulls?state=closed&per_page=100" % (rec["owner"], repo))
        calls += c
        rec["pulls_fetched"] = True
        for p in pulls:
            it = items.get(str(p.get("number")))
            if it and it["kind"] == "pr":
                it["merged_at"], it["merged_known"] = p.get("merged_at"), True
    rec["calls"] = rec.get("calls", 0) + calls
    return calls


def item_at(rec: Optional[Mapping[str, Any]], n: int, t: datetime) -> Tuple[str, Optional[datetime]]:
    """t 시점 범주와 (끝난 범주면) GitHub 에서 끝난 시각."""
    if not rec or rec.get("status") != 200:
        return "missing_repo", None
    it = (rec.get("items") or {}).get(str(n))
    if it is None:
        return "missing_number", None
    created = parse_instant(it.get("created_at"))
    if created is None or created > t:
        return "not_yet", None
    closed = parse_instant(it.get("closed_at"))
    if it["kind"] == "pr":
        merged = parse_instant(it.get("merged_at"))
        if merged is not None and merged <= t:
            return "pr_merged", merged
        if closed is not None and closed <= t and merged is None:
            return "pr_closed", closed
        return "pr_open", None
    evs = (rec.get("events") or {}).get(str(n))
    ever_closed = closed is not None or it.get("state_reason") == "reopened" or bool(evs)
    if not ever_closed:
        return "open_issue", None
    if not evs:
        return "reopened_unknown", None
    upto = [e for e in evs if (parse_instant(e[1]) or t + timedelta(days=1)) <= t]
    if not upto or upto[-1][0] == "reopened":
        return "open_issue", None
    last = upto[-1]
    reason = last[2]
    if reason is None and last is evs[-1] and it.get("state") == "closed":
        reason = it.get("state_reason")
    cat = "closed_not_planned" if reason in NOT_PLANNED_REASONS else "closed_completed"
    return cat, parse_instant(last[1])


def open_issues_at(rec: Optional[Mapping[str, Any]], t: datetime) -> Tuple[Set[int], Set[int]]:
    """t 시점 열린 이슈 번호(PR 제외)와 이력이 없어 정하지 못한 번호."""
    opened, unknown = set(), set()
    if not rec or rec.get("status") != 200:
        return opened, unknown
    for k, it in rec["items"].items():
        if it["kind"] != "issue":
            continue
        cat, _ = item_at(rec, int(k), t)
        if cat == "open_issue":
            opened.add(int(k))
        elif cat == "reopened_unknown":
            unknown.add(int(k))
    return opened, unknown


# --- 원인 -----------------------------------------------------------------------------------------------

def _without(pattern: "re.Pattern[str]", text: str) -> str:
    return pattern.sub(" ", text)


def key_sources(sentence: str, tags: Sequence[str], aliases: Mapping[str, str], key: str) -> Set[str]:
    """문장 하나에서 key 가 어떻게 나왔나: explicit|bare(+ range: 범위 양 끝에서만, pr: PR 표기에서만, external)."""
    want = (repo_of(key), num_of(key))
    if want not in IK.extract_keys(sentence, tags, aliases):
        return set()
    out = {"explicit" if want in IK.extract_keys(sentence, list(tags) + _DUMMY_PROJECTS, aliases) else "bare"}
    if want not in IK.extract_keys(_without(RANGE_RE, sentence), tags, aliases):
        out.add("range")
    if want not in IK.extract_keys(_without(PR_RE, sentence), tags, aliases):
        out.add("pr")
    low = sentence.lower()
    if any(w in low for w in EXTERNAL_WORDS):
        out.add("external")
    return out


def _tags(mem: Mapping[str, Any]) -> List[str]:
    tags = mem.get("tags")
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, (list, tuple)) else []


def _text(mem: Mapping[str, Any]) -> str:
    v = mem.get("text") if isinstance(mem.get("text"), str) else mem.get("content")
    return v if isinstance(v, str) else ""


def _usable(mem: Mapping[str, Any], t: datetime) -> bool:
    at = parse_instant(mem.get("mentioned_at"))
    return mem.get("state") == "valid" and at is not None and at <= t


def repo_max_numbers(listing: Mapping[str, Mapping[str, Any]], aliases: Mapping[str, str], t: datetime
                     ) -> Dict[str, int]:
    """리포별 명시 표기(`<repo>#n`·`owner/repo#n`·`item:` 태그) 최대 번호(t 이하 valid 기억)."""
    out: Dict[str, int] = {}
    for mem in listing.values():
        if not _usable(mem, t):
            continue
        tags = _tags(mem)
        other = [x for x in tags if not x.strip().lower().startswith("item:")] + _DUMMY_PROJECTS
        item_tags = [x for x in tags if x.strip().lower().startswith("item:")]
        for repo, n in IK.extract_keys(_text(mem), other, aliases) | IK.extract_keys("", item_tags, aliases):
            out[repo] = max(out.get(repo, 0), n)
    return out


def _ranges(text: str, tags: Sequence[str], aliases: Mapping[str, str]) -> List[Tuple[str, int, int]]:
    table = IK._table(aliases)
    projects = IK._project_repos(IK._tag_set(tags), table)
    names = set(table) | set(table.values()) | set(projects)
    out = []
    for m in RANGE_RE.finditer(IK._mask_quotes(text)):
        a, b = int(m.group(2)), int(m.group(4))
        prefix = m.group(1) or m.group(3)
        repo = None
        if prefix and prefix.lower() in names:
            repo = IK._canonical(prefix, table)
        elif len(projects) == 1:
            (repo,) = projects
        if repo and a < b and b - a + 1 <= RANGE_CAP:
            out.append((repo, a, b))
    return out


def causes_for(key: str, cat: str, end_at: Optional[datetime], view: StoreView,
               listing: Mapping[str, Mapping[str, Any]], aliases: Mapping[str, str], repo_max: Mapping[str, int],
               t: datetime) -> Dict[str, Any]:
    ms = view.by_key.get(key) or []
    causes: Set[str] = set()
    info: Dict[str, Any] = {"range": set(), "over_max": False, "no_body": False}
    if ms:
        dec_at = max(m[0] for m in ms)
        deciding = sorted({mid for at, role, mid in ms if at == dec_at and role == IR.OPEN})
        if any(at == dec_at and role == IR.DONE for at, role, _ in ms):
            causes.add("2")
        if any(at < dec_at and role == IR.DONE for at, role, _ in ms):
            causes.add("3")
        srcs: List[Set[str]] = []
        for mid in deciding:
            mem = listing.get(mid)
            if mem is None:
                info["no_body"] = True
                continue
            tags = [x for x in _tags(mem) if not x.strip().lower().startswith("item:")]
            for s in IR.split_sentences(_text(mem)):
                got = key_sources(s, tags, aliases, key)
                if got:
                    srcs.append(got)
        if srcs:
            if all("range" in s for s in srcs):
                info["range"].add("endpoint")
            if all("pr" in s for s in srcs):
                causes.add("5")
            if all("explicit" not in s for s in srcs):
                over = repo_of(key) in repo_max and num_of(key) > repo_max[repo_of(key)]
                info["over_max"] = over
                if over or any("external" in s for s in srcs):
                    causes.add("4")
        # 1 covered: 결정 뒤(같은 시각 포함) 끝냄 범위가 이 번호를 안에 품는다(범위 미전개로 끝냄이 닿지 않음)
        n, repo = num_of(key), repo_of(key)
        for mid, roles in view.mem_roles.items():
            mem = listing.get(mid)
            if mem is None or view.mem_at.get(mid, t) < dec_at or not _usable(mem, t):
                continue
            tags = [x for x in _tags(mem) if not x.strip().lower().startswith("item:")]
            for r, a, b in _ranges(_text(mem), tags, aliases):
                if r == repo and a < n < b and IR.DONE in (roles.get("%s#%d" % (r, a)), roles.get("%s#%d" % (r, b))):
                    info["range"].add("covered")
        if info["range"]:
            causes.add("1")
        if cat in FINISHED and end_at is not None and not any(
                role == IR.DONE and at >= end_at for at, role, _ in ms):
            causes.add("data")
    if not causes:
        causes.add("other")
    primary = next(c for c in PRIMARY_ORDER if c in causes)
    return {"causes": causes, "primary": primary, **info}


# --- 지표 -----------------------------------------------------------------------------------------------

def _ratio(num: int, den: int) -> Optional[float]:
    return num / den if den else None


def table_metrics(keys: Sequence[str], gh: Mapping[str, Any], t: datetime, recall_repos: Iterable[str],
                  store_keys: Set[str], codes: Mapping[str, str]) -> Dict[str, Any]:
    groups: Dict[str, Dict[str, Any]] = {}

    def group(name: str) -> Dict[str, Any]:
        return groups.setdefault(name, {"keys": 0, "cats": Counter(), "den": 0, "num": 0, "sden": 0, "snum": 0,
                                        "unknown": 0})

    tset = set(keys)
    for k in keys:
        cat, _ = item_at(gh.get(repo_of(k)), num_of(k), t)
        for g in (group("__total__"), group(repo_of(k))):
            g["keys"] += 1
            g["cats"][cat] += 1
    for repo in sorted(set(recall_repos)):
        rec = gh.get(repo)
        if not rec or rec.get("status") != 200:
            continue
        opened, unknown = open_issues_at(rec, t)
        hit = {n for n in opened if "%s#%d" % (repo, n) in tset}
        sopen = {n for n in opened if "%s#%d" % (repo, n) in store_keys}
        shit = sopen & hit
        for g in (group("__total__"), group(repo)):
            g["den"] += len(opened)
            g["num"] += len(hit)
            g["sden"] += len(sopen)
            g["snum"] += len(shit)
            g["unknown"] += len(unknown)

    def shape(g: Mapping[str, Any]) -> Dict[str, Any]:
        oi = g["cats"].get("open_issue", 0)
        return {"keys": g["keys"], "open_issue": oi, "precision": _ratio(oi, g["keys"]),
                "categories": {c: g["cats"][c] for c in CATEGORIES if g["cats"].get(c)},
                "recall": {"den": g["den"], "num": g["num"], "value": _ratio(g["num"], g["den"])},
                "recall_store": {"den": g["sden"], "num": g["snum"], "value": _ratio(g["snum"], g["sden"])},
                "recall_unknown": g["unknown"]}

    total = shape(groups.get("__total__") or group("__total__"))
    repos = {codes[r]: shape(g) for r, g in groups.items() if r != "__total__" and g["keys"] + g["den"] > 0}
    return {"total": total, "repos": dict(sorted(repos.items()))}


def cause_counts(keys: Sequence[str], details: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    multi, primary, rng = Counter(), Counter(), Counter()
    errors = 0
    over = no_body = 0
    for k in keys:
        d = details[k]
        if d["category"] == "open_issue":
            continue
        errors += 1
        multi.update(d["causes"])
        primary[d["primary"]] += 1
        rng.update(d["range"])
        over += bool(d["over_max"])
        no_body += bool(d["no_body"])
    return {"errors": errors, "any": {c: multi.get(c, 0) for c in CAUSES},
            "primary": {c: primary.get(c, 0) for c in CAUSES},
            "range_kind": {k: rng.get(k, 0) for k in ("endpoint", "covered")}, "over_max": over, "no_body": no_body}


def repo_codes(r5: Mapping[str, str], existing: Mapping[str, str], repos: Iterable[str]) -> Dict[str, str]:
    out = dict(existing)
    out.update(r5)
    used = [int(c[1:]) for c in out.values() if re.fullmatch(r"x\d+", c)]
    nxt = max(used, default=0) + 1
    for repo in sorted(set(repos) - set(out)):
        out[repo] = "x%02d" % nxt
        nxt += 1
    return out


# --- 입력 ------------------------------------------------------------------------------------------------

def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_listing(bodies: str, meta: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """본문 JSONL(id, text) + 메타 JSONL(id, tags, state, mentioned_at, document_id, fact_type) -> {id: 기억}.
    `scripts/item_roles_sidecar.offline_listing` 과 같은 꼴(메타를 안 주면 본문 줄에 메타가 같이 있다)."""
    rows = _read_jsonl(bodies)
    metas = {str(r["id"]): r for r in _read_jsonl(meta)} if meta else {str(r["id"]): r for r in rows}
    out = {}
    for b in rows:
        mid = str(b["id"])
        m = metas.get(mid)
        if m is None:
            raise Fail("a body has no meta row")
        out[mid] = {"id": mid, "text": b.get("text") if isinstance(b.get("text"), str) else "",
                    "tags": m.get("tags") or [], "state": m.get("state"), "document_id": m.get("document_id"),
                    "mentioned_at": m.get("mentioned_at"), "type": m.get("fact_type")}
    return out


def plan_cutoffs(plan_path: str, days: Sequence[str]) -> Dict[str, datetime]:
    with open(plan_path, encoding="utf-8") as fh:
        per_t = json.load(fh)["per_t"]
    out = {}
    for d in days:
        if d not in per_t:
            raise Fail("dev t %s is not in the plan" % d)
        out[d] = parse_instant(per_t[d]["query_timestamp"])
    return out


# --- 명령 -----------------------------------------------------------------------------------------------

def _sidecar():
    spec = importlib.util.spec_from_file_location("item_roles_sidecar",
                                                  os.path.join(REPO, "scripts", "item_roles_sidecar.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_ops_listing(args) -> int:
    if os.path.exists(args.out):
        print("ops-listing: reuse sha256=%s" % file_sha256(args.out))
        return 0
    sc = _sidecar()
    get = sc.upstream_getter(allow_prod=True)          # GET 만 하는 호출기
    listing = sc.list_all(get, args.bank)
    lines = []
    for mid in sorted(listing):
        mem = listing[mid]
        lines.append(json.dumps({"id": mid, "text": sc.memory_text(mem), "tags": sc.memory_tags(mem),
                                 "state": mem.get("state"), "mentioned_at": mem.get("mentioned_at"),
                                 "document_id": mem.get("document_id"),
                                 "fact_type": mem.get("fact_type") or mem.get("type")}, ensure_ascii=False))
    sha = write_private(args.out, "\n".join(lines) + "\n")
    print("ops-listing: memories=%d sha256=%s" % (len(lines), sha))
    return 0


def _evaluate(label: str, t: datetime, view: StoreView, listing: Mapping[str, Mapping[str, Any]],
              aliases: Mapping[str, str], gh: Mapping[str, Any], recall_repos: Sequence[str],
              codes: Mapping[str, str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    keys, truncated = table_keys(view, t)
    hc = [k for k in keys if repo_of(k) == HC_REPO]
    rmax = repo_max_numbers(listing, aliases, t)
    details: Dict[str, Dict[str, Any]] = {}
    for k in keys:
        cat, end = item_at(gh.get(repo_of(k)), num_of(k), t)
        c = causes_for(k, cat, end, view, listing, aliases, rmax, t) if cat != "open_issue" else {
            "causes": set(), "primary": None, "range": set(), "over_max": False, "no_body": False}
        details[k] = {"category": cat, "end": _iso(end), **c}
    summary = {
        "t": _iso(t), "t_kst_date": t.astimezone(KST).date().isoformat(), "truncated": truncated,
        "bank": {"metrics": table_metrics(keys, gh, t, recall_repos, view.all_keys, codes),
                 "causes": cause_counts(keys, details)},
        "hindsight_crew": {"metrics": table_metrics(hc, gh, t, [HC_REPO], view.all_keys, codes)["total"],
                           "causes": cause_counts(hc, details)},
        "store_keys": len(view.all_keys), "decided_keys": len(view.states),
    }
    priv = {k: {**d, "causes": sorted(d["causes"]), "range": sorted(d["range"])} for k, d in details.items()}
    return summary, {"label": label, "keys": priv}


def cmd_run(args) -> int:
    dev_days = list(args.dev_t or DEV_TS)
    for d in dev_days:
        guard_dev_t(datetime.strptime(d, "%Y-%m-%d").date())
    with open(args.ops_copy, encoding="utf-8") as fh:
        copy = json.load(fh)
    t_ops = parse_instant(copy["copy_end_kst"])
    guard_ops_t(t_ops.astimezone(KST).date())

    ops_sha = file_sha256(args.ops_store)
    if copy.get("sha256") and copy["sha256"] != ops_sha:
        raise Fail("ops store copy sha256 differs from the copy record")
    aliases = load_aliases(args.aliases)
    ops_view = store_view(args.ops_store, t_ops)
    dev_cut = plan_cutoffs(args.plan, dev_days)
    dev_views = {d: store_view(args.dev_store, c) for d, c in dev_cut.items()}
    for meta in [ops_view.meta] + [v.meta for v in dev_views.values()]:
        if not check_aliases(meta, aliases):
            print("aliases: canonical sha256 differs from store meta aliases_sha256", file=sys.stderr)
            return 3
    ops_listing = load_listing(args.ops_listing)
    dev_listing = load_listing(args.bodies, args.meta)

    tables = {"ops": table_keys(ops_view, t_ops)[0]}
    tables.update({"dev-" + d: table_keys(v, dev_cut[d])[0] for d, v in dev_views.items()})
    table_repos = {repo_of(k) for ks in tables.values() for k in ks}
    recall_repos = sorted(table_repos | set(aliases.values()))

    cache_path = os.path.join(args.work, "r7-0-github.json")
    gh: Dict[str, Any] = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as fh:
            gh = json.load(fh)
    repos_doc = gh.setdefault("repos", {})
    calls_before = sum(r.get("calls", 0) for r in repos_doc.values())
    owners: Dict[str, str] = {}
    if args.owners:
        with open(args.owners, encoding="utf-8") as fh:
            for repo, cur in (json.load(fh).get("repos") or {}).items():
                if isinstance(cur, dict) and cur.get("owner"):
                    owners[repo] = cur["owner"]
    wanted: Dict[str, Set[int]] = defaultdict(set)
    for ks in tables.values():
        for k in ks:
            wanted[repo_of(k)].add(num_of(k))
    try:
        for repo in recall_repos:
            if repo not in repos_doc:
                if args.offline:
                    raise Fail("offline run but a repo is missing from the cache")
                repos_doc[repo] = fetch_repo(owners.get(repo, DEFAULT_OWNER), repo)
            if not args.offline:
                fill_gaps(repos_doc[repo], repo, wanted.get(repo, set()))
    finally:
        calls_after = sum(r.get("calls", 0) for r in repos_doc.values())
        gh["calls_total"] = calls_after
        gh_sha = write_private(cache_path, json.dumps(gh, sort_keys=True, ensure_ascii=False))

    codes_path = os.path.join(args.work, "r7-repo-codes.json")
    existing = {}
    if os.path.exists(codes_path):
        with open(codes_path, encoding="utf-8") as fh:
            existing = json.load(fh)
    with open(args.codes, encoding="utf-8") as fh:
        r5_codes = json.load(fh)
    codes = repo_codes(r5_codes, existing, set(recall_repos) | table_repos)
    write_private(codes_path, json.dumps(codes, sort_keys=True, ensure_ascii=False))

    results, private = {}, {}
    s, p = _evaluate("ops", t_ops, ops_view, ops_listing, aliases, repos_doc, recall_repos, codes)
    results["ops"], private["ops"] = s, p
    for d, v in dev_views.items():
        s, p = _evaluate("dev-" + d, dev_cut[d], v, dev_listing, aliases, repos_doc, recall_repos, codes)
        results["dev-" + d], private["dev-" + d] = s, p

    exist = sorted(codes[r] for r in recall_repos if (repos_doc.get(r) or {}).get("status") == 200)
    missing = sorted(codes[r] for r in recall_repos if (repos_doc.get(r) or {}).get("status") != 200)
    summary = {
        "inputs": {"ops_store_sha256": ops_sha, "ops_t": _iso(t_ops), "dev_store_sha256": file_sha256(args.dev_store),
                   "aliases_file_sha256": file_sha256(args.aliases), "aliases_canonical_sha256": OT.aliases_sha256(aliases),
                   "plan_sha256": file_sha256(args.plan), "ops_listing_sha256": file_sha256(args.ops_listing),
                   "bodies_sha256": file_sha256(args.bodies), "meta_sha256": file_sha256(args.meta),
                   "github_cache_sha256": gh_sha},
        "github": {"calls_this_run": calls_after - calls_before, "calls_total": calls_after,
                   "repos_checked": len(recall_repos), "repos_exist": exist, "repos_missing": missing},
        "knobs": {k: table_knobs()[k] for k in ("scope", "recent_done_days", "max_rows", "table_fmt")},
        "results": results,
    }
    details_sha = write_private(os.path.join(args.work, "r7-0-details.json"),
                                json.dumps(private, sort_keys=True, ensure_ascii=False))
    sum_sha = write_private(os.path.join(args.work, "r7-0-summary.json"),
                            json.dumps(summary, sort_keys=True, ensure_ascii=False, indent=1))
    for label, r in results.items():
        b, h = r["bank"]["metrics"]["total"], r["hindsight_crew"]["metrics"]
        print("%s: bank %d/%d precision=%s recall=%s | hc %d/%d precision=%s" % (
            label, b["open_issue"], b["keys"], _fmt(b["precision"]), _fmt(b["recall"]["value"]),
            h["open_issue"], h["keys"], _fmt(h["precision"])))
    print("github: calls_this_run=%d calls_total=%d cache_sha256=%s" % (calls_after - calls_before, calls_after, gh_sha))
    print("summary_sha256=%s details_sha256=%s" % (sum_sha, details_sha))
    return 0


def _fmt(v: Optional[float]) -> str:
    return "-" if v is None else "%.1f%%" % (100 * v)


# --- offline (R7-3, #397) ----------------------------------------------------------------------------------

def _evaluate_arm(t: datetime, view: StoreView, listing: Mapping[str, Mapping[str, Any]], aliases: Mapping[str, str],
                  gh: Mapping[str, Any], recall_repos: Sequence[str], codes: Mapping[str, str]
                  ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    tab = table3(view, t)
    keys, rows = tab["keys"], tab["table"]["rows"]
    rmax = repo_max_numbers(listing, aliases, t)
    details: Dict[str, Dict[str, Any]] = {}
    for k in keys:
        cat, end = item_at(gh.get(repo_of(k)), num_of(k), t)
        c = causes_for(k, cat, end, view, listing, aliases, rmax, t) if cat != "open_issue" else {
            "causes": set(), "primary": None, "range": set(), "over_max": False, "no_body": False}
        details[k] = {"category": cat, "end": _iso(end), **c}

    def part(ks: Sequence[str], rs: Sequence[Sequence[str]], repos: Sequence[str]) -> Dict[str, Any]:
        m = table_metrics(ks, gh, t, repos, view.all_keys, codes)
        return {"metrics": m, "causes": cause_counts(ks, details), "rows": row_categories(rs, gh, t),
                "subgroups": subgroup_metrics(ks, gh, t),
                "linked_causes": cause_counts([k for k in ks if linked(k, gh)], details)}

    hc_keys = [k for k in keys if repo_of(k) == HC_REPO]
    hc_rows = [r for r in rows if repo_of(r[0]) == HC_REPO]
    hc = part(hc_keys, hc_rows, [HC_REPO])
    hc["metrics"] = hc["metrics"]["total"]
    summary = {"three_state": tab["three_state"], "table_sha256": tab["sha256"],
               "truncated": tab["table"]["truncated"], "bank": part(keys, rows, recall_repos), "hindsight_crew": hc,
               "limits": {"reopen": reopen_limit(rows, gh, t), "slug": slug_limit(rows),
                          "reopen_hc": reopen_limit(hc_rows, gh, t)},
               "store_keys": len(view.all_keys), "decided_keys": len(view.states)}
    priv = {"keys": {k: {**d, "causes": sorted(d["causes"]), "range": sorted(d["range"])} for k, d in details.items()},
            "rows": [list(r) for r in rows]}
    return summary, priv


def cmd_offline(args) -> int:
    with open(args.ops_copy, encoding="utf-8") as fh:
        copy = json.load(fh)
    t = parse_instant(copy["copy_end_kst"])
    guard_ops_t(t.astimezone(KST).date())
    labels = [a[0] for a in args.arm]
    if len(set(labels)) != len(labels):
        raise Fail("arm labels must be unique")
    aliases = load_aliases(args.aliases)
    work = os.path.abspath(args.work)
    views: Dict[str, StoreView] = {}
    audit: Dict[str, Dict[str, Any]] = {}
    for label, path, mode in args.arm:
        before = file_sha256(path)
        view = store_view(path, t, github_mode=mode)
        if not check_aliases(view.meta, aliases):
            print("aliases: canonical sha256 differs from store meta aliases_sha256", file=sys.stderr)
            return 3
        views[label] = view
        audit[label] = store_audit(path, view, before, work, mode)
    listing = load_listing(args.ops_listing)

    cache_out = os.path.join(work, "r7-3-github.json")
    src = cache_out if os.path.exists(cache_out) else args.github_cache     # R7-0 캐시 원본은 읽기만
    with open(src, encoding="utf-8") as fh:
        gh = json.load(fh)
    repos_doc = gh.setdefault("repos", {})
    calls_before = sum(r.get("calls", 0) for r in repos_doc.values())
    with open(args.github_cache, encoding="utf-8") as fh:
        r70_repos = set((json.load(fh).get("repos") or {}))
    tabs = {label: table3(v, t) for label, v in views.items()}
    wanted: Dict[str, Set[int]] = defaultdict(set)
    for tab in tabs.values():
        for r in tab["table"]["rows"]:
            wanted[repo_of(r[0])].add(num_of(r[0]))
    recall_repos = sorted(r70_repos | set(wanted) | set(aliases.values()))
    owners = {r: v.get("owner") for r, v in repos_doc.items() if isinstance(v, dict) and v.get("owner")}
    try:
        for repo in recall_repos:
            if repo not in repos_doc:
                if args.offline:
                    raise Fail("offline run but a repo is missing from the cache")
                repos_doc[repo] = fetch_repo(owners.get(repo, DEFAULT_OWNER), repo)
            if not args.offline:
                fill_gaps(repos_doc[repo], repo, wanted.get(repo, set()))
    finally:
        calls_after = sum(r.get("calls", 0) for r in repos_doc.values())
        gh["calls_total"] = calls_after
        gh_sha = write_private(cache_out, json.dumps(gh, sort_keys=True, ensure_ascii=False))

    with open(args.codes, encoding="utf-8") as fh:
        codes = repo_codes({}, json.load(fh), recall_repos)
    write_private(os.path.join(work, "r7-3-repo-codes.json"), json.dumps(codes, sort_keys=True, ensure_ascii=False))

    arms, private = {}, {}
    for label, _, mode in args.arm:
        s, p = _evaluate_arm(t, views[label], listing, aliases, repos_doc, recall_repos, codes)
        arms[label] = {"mode": mode, "store": audit[label], **s}
        private[label] = p
    equal = [[a, b] for i, a in enumerate(labels) for b in labels[i + 1:]
             if arms[a]["table_sha256"] == arms[b]["table_sha256"]]
    baseline = None
    if args.baseline:
        with open(args.baseline, encoding="utf-8") as fh:
            ops = json.load(fh)["results"]["ops"]
        baseline = {"bank": ops["bank"]["metrics"]["total"], "bank_causes": ops["bank"].get("causes"),
                    "hindsight_crew": ops["hindsight_crew"]["metrics"], "hc_causes": ops["hindsight_crew"].get("causes")}
    exist = sorted(codes[r] for r in recall_repos if (repos_doc.get(r) or {}).get("status") == 200)
    missing = sorted(codes[r] for r in recall_repos if (repos_doc.get(r) or {}).get("status") != 200)
    summary = {
        "inputs": {"ops_t": _iso(t), "ops_listing_sha256": file_sha256(args.ops_listing),
                   "aliases_file_sha256": file_sha256(args.aliases), "aliases_canonical_sha256": OT.aliases_sha256(aliases),
                   "github_cache_r70_sha256": file_sha256(args.github_cache), "github_cache_sha256": gh_sha,
                   "baseline_sha256": file_sha256(args.baseline) if args.baseline else None},
        "github": {"calls_this_run": calls_after - calls_before, "calls_total": calls_after,
                   "repos_checked": len(recall_repos), "repos_exist": exist, "repos_missing": missing},
        "knobs": {k: table_knobs()[k] for k in ("scope", "recent_done_days", "max_rows", "table_fmt")},
        "arms": arms, "equal_tables": equal, "baseline_v1": baseline,
    }
    details_sha = write_private(os.path.join(work, "r7-3-details.json"),
                                json.dumps(private, sort_keys=True, ensure_ascii=False))
    sum_sha = write_private(os.path.join(work, "r7-3-summary.json"),
                            json.dumps(summary, sort_keys=True, ensure_ascii=False, indent=1))
    for label, a in arms.items():
        b, sg = a["bank"]["metrics"]["total"], a["bank"]["subgroups"]["linked"]
        h = a["hindsight_crew"]["metrics"]
        print("%s: mode=%s three_state=%s key_rules=%s bank %d/%d precision=%s recall=%s linked %d/%d precision=%s"
              " | hc %d/%d precision=%s | unknown=%d" % (
                  label, a["mode"], a["three_state"], a["store"]["key_rules"], b["open_issue"], b["keys"],
                  _fmt(b["precision"]), _fmt(b["recall"]["value"]), sg["open_issue"], sg["keys"], _fmt(sg["precision"]),
                  h["open_issue"], h["keys"], _fmt(h["precision"]),
                  a["bank"]["rows"]["by_state"].get(OT.ROW_UNKNOWN, {}).get("rows", 0)))
    print("equal_tables=%s" % json.dumps(equal))
    print("github: calls_this_run=%d calls_total=%d cache_sha256=%s" % (calls_after - calls_before, calls_after, gh_sha))
    print("summary_sha256=%s details_sha256=%s" % (sum_sha, details_sha))
    return 0


def main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="r7_eval.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ol = sub.add_parser("ops-listing")
    ol.add_argument("--bank", required=True)
    ol.add_argument("--out", required=True)
    rn = sub.add_parser("run")
    for flag in ("--ops-store", "--ops-copy", "--ops-listing", "--dev-store", "--plan", "--bodies", "--meta",
                 "--aliases", "--codes", "--work"):
        rn.add_argument(flag, required=True)
    rn.add_argument("--owners")
    rn.add_argument("--dev-t", action="append")
    rn.add_argument("--offline", action="store_true")
    of = sub.add_parser("offline")
    of.add_argument("--arm", nargs=3, action="append", required=True, metavar=("LABEL", "STORE", "MODE"),
                    help="label, role store copy, github_evidence use|ignore")
    for flag in ("--ops-copy", "--ops-listing", "--aliases", "--codes", "--github-cache", "--work"):
        of.add_argument(flag, required=True)
    of.add_argument("--baseline")
    of.add_argument("--offline", action="store_true")
    args = ap.parse_args(list(argv))
    try:
        if args.cmd == "ops-listing":
            guard_paths([args.out])
            return cmd_ops_listing(args)
        if args.cmd == "offline":
            guard_paths([a[1] for a in args.arm] + [args.ops_copy, args.ops_listing, args.aliases, args.codes,
                                                     args.github_cache, args.work, args.baseline])
            return cmd_offline(args)
        guard_paths([args.ops_store, args.ops_copy, args.ops_listing, args.dev_store, args.plan, args.bodies,
                     args.meta, args.aliases, args.owners, args.codes, args.work])
        return cmd_run(args)
    except SealGuard as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2
    except Fail as e:
        print("failed: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
