"""recall 재생 하네스 - 시험 문제(코퍼스) 추출기 (issue #13).

실사용 recall 호출을 세 출처에서 모아 정규화·정제·중복 제거하고, 개발용(dev)과 숨김 평가용
(holdout)으로 결정적으로 나눈다. 합성 쿼리는 만들지 않는다 - 실제로 에이전트가 던진 인자만 쓴다.

출처
  cc      ~/.claude/projects/**/*.jsonl      tool_use name=mcp__memory-vault__recall 의 input
  codex   ~/.codex/sessions/**/*.jsonl       McpToolCall server=memory-vault tool=recall 의 arguments
  ledger  audit/recall-queries.log           게이트웨이 recall 원장의 identity=demo 행 (인자는 query·tags·tags_match 뿐)
                                             transcript 가 없는 런타임(YEW·mentor·Codex code-mode) 몫으로만 쓴다

정규화 규칙
  - bank_id 는 버린다: 게이트웨이가 무시하고 ACL 뱅크(X-Bank-Id)로 고정한다.
  - 인자는 그대로 재생한다(쿼리 공백도 보존). 중복 판정 키만 정규화한다.
  - 원장 행은 인자(max_tokens·budget)가 빠진 그림자라 키가 따로 생긴다. 같은 텍스트가 transcript 에 한 번이라도
    있으면 원장 행은 버린다(그림자). 같은 세션이 같은 텍스트를 반복한 원장 행은 1회로 센다(리허설 트래픽 가중 억제).
  - CC tool_use 는 id 로 1회만 센다(같은 호출이 여러 transcript 줄에 복제된다).

제외 규칙 (전부 manifest 에 사유별 건수로 남는다)
  - secret-shape      scrub_secrets 가 쿼리를 바꾼다(시크릿 형태 포함) - 파일에 남기지 않는다
  - probe             검증용 probe·smoke 쿼리(실사용이 아님)
  - policy-rejected   현 게이트웨이 규칙이 upstream 도달 전에 거부하는 인자(max_tokens 하한 미달 등) -
                      두 팔 모두 같은 RULE 응답이라 측정할 것이 없다
  - malformed         query 가 문자열이 아니거나 비었다

분할
  holdout 여부 = HMAC-SHA256(salt, split_key) 첫 2바이트 < ratio*65536. split_key 는 **정규화한 쿼리 텍스트**의
  해시다(인자 전체가 아니다) - 같은 텍스트가 인자만 달리해 dev 와 holdout 에 갈라지면 dev 로 개발한 쪽이
  holdout 문제를 이미 본 셈이 된다(2026-09-23 재리뷰 P2). salt 는 holdout 디렉터리에만 있어서 dev 파일만 보는
  쪽은 분할을 재계산할 수 없다. 같은 salt 면 코퍼스가 늘어나도 기존 텍스트의 소속이 안 바뀐다.
  분할 규칙 버전(SPLIT_VERSION)은 salt 옆에 기록하고, 코드의 버전과 다르면 재생성을 거부한다 - 규칙이 바뀌면
  항목이 dev/holdout 사이를 옮겨 다니므로 --reset-split 으로 명시해야 한다.
"""
from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from gateway.context_triggers import scrub_secrets  # noqa: E402  (단일 여과 SSoT 재사용)

CC_ROOT = os.path.expanduser("~/.claude/projects")
CODEX_ROOT = os.path.expanduser("~/.codex/sessions")
LEDGER = os.path.join(REPO, "audit", "recall-queries.log")
DEV_DIR = os.path.join(REPO, "bench", ".data")
HOLDOUT_DIR = os.path.expanduser("~/.local/share/hindsight-bench/holdout")

# recall 도구가 받는 인자 중 재생에 쓰는 것. bank_id 는 게이트웨이가 무시하므로 뺀다.
# limit 은 upstream recall 시그니처에 없는 인자라(mcp_tools.py:850) 무시된다 - 키만 가르므로 재생하지 않는다.
REPLAY_ARGS = ("query", "max_tokens", "budget", "tags", "tags_match", "types",
               "query_timestamp", "min_scores", "prefer_observations")
RECALL_MAX_TOKENS_FLOOR = 4096       # policies/bank-policies.json defaults (personal-demo)
# upstream MCP recall 도구의 기본값(mcp_tools.py:850). 명시해도 생략해도 같은 요청이므로 중복 판정에서 지운다.
MCP_DEFAULTS = {"max_tokens": 4096, "budget": "high"}
SPLIT_VERSION = "text-v1"          # split_key 규칙이 바뀌면 올린다
PROBE_RE = re.compile(r"probe|프로브|smoke|스모크|무효화 예정|harness-selftest", re.I)


@dataclass
class Call:
    source: str
    ts: Optional[str]
    args: Dict[str, Any]
    origin: str                       # 출처 파일(경로) - 디버깅용, 산출물에는 basename 만


@dataclass
class Entry:
    key: str
    args: Dict[str, Any]
    sources: collections.Counter = field(default_factory=collections.Counter)
    count: int = 0
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None


# ---------------------------------------------------------------- 수집

def _iter_jsonl(pattern: str, needle: str) -> Iterator[Tuple[str, Dict[str, Any]]]:
    for path in sorted(glob.glob(pattern, recursive=True)):
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    if needle not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        yield path, obj
        except OSError:
            continue


def _walk(obj: Any) -> Iterator[Dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def collect_cc(root: str = CC_ROOT) -> List[Call]:
    out, seen = [], set()
    for path, obj in _iter_jsonl(os.path.join(root, "**", "*.jsonl"), "mcp__memory-vault__recall"):
        ts = obj.get("timestamp")
        for node in _walk(obj):
            if node.get("type") == "tool_use" and node.get("name") == "mcp__memory-vault__recall" \
                    and isinstance(node.get("input"), dict):
                # 같은 tool_use 가 여러 줄(부모·서브에이전트 transcript, 재개)에 복제된다 - id 로 1회만
                ident = node.get("id") or (path, ts, json.dumps(node["input"], sort_keys=True))
                if ident in seen:
                    continue
                seen.add(ident)
                out.append(Call("cc", ts, dict(node["input"]), path))
    return out


def collect_codex(root: str = CODEX_ROOT) -> List[Call]:
    out, seen = [], set()
    for path, obj in _iter_jsonl(os.path.join(root, "**", "*.jsonl"), '"tool":"recall"'):
        ts = obj.get("timestamp")
        for node in _walk(obj):
            if node.get("type") == "McpToolCall" and node.get("server") == "memory-vault" \
                    and node.get("tool") == "recall" and isinstance(node.get("arguments"), dict):
                # 같은 호출이 item_started / item_completed 로 두 번 기록된다 - id 로 1회만
                ident = (path, node.get("id"))
                if ident in seen:
                    continue
                seen.add(ident)
                out.append(Call("codex", ts, dict(node["arguments"]), path))
    return out


def collect_ledger(path: str = LEDGER) -> List[Call]:
    out, seen = [], set()
    try:
        fh = open(path, errors="ignore")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("identity") != "demo" or row.get("bank") != "personal-demo":
                continue
            # 같은 세션이 같은 텍스트를 되풀이한 행(리허설·재시도)은 1회로 센다. 세션 없는 행은 하루 단위.
            scope = row.get("session_id") or ("day:" + str(row.get("ts", ""))[:10])
            ident = (scope, norm_text(row.get("query")))
            if ident in seen:
                continue
            seen.add(ident)
            args: Dict[str, Any] = {"query": row.get("query")}
            if row.get("tags"):
                args["tags"] = row["tags"]
            if row.get("tags_match"):
                args["tags_match"] = row["tags_match"]
            out.append(Call("ledger", row.get("ts"), args, path))
    return out


# ---------------------------------------------------------------- 정규화·정제

def norm_text(q: Any) -> str:
    return " ".join(str(q or "").split())


def split_key(args: Dict[str, Any]) -> str:
    """분할 단위 = 정규화한 쿼리 텍스트. 인자만 다른 같은 텍스트는 반드시 같은 쪽에 간다."""
    return hashlib.sha256(norm_text(args.get("query")).encode()).hexdigest()[:16]


def replay_args(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {k: raw[k] for k in REPLAY_ARGS if k in raw and raw[k] is not None}


def canonical_key(args: Dict[str, Any]) -> str:
    norm = dict(args)
    norm["query"] = norm_text(norm.get("query"))
    if isinstance(norm.get("tags"), list):
        norm["tags"] = sorted(norm["tags"])
    for k, v in MCP_DEFAULTS.items():
        if norm.get(k) == v:
            del norm[k]
    if norm.get("tags"):
        norm.setdefault("tags_match", "any")            # upstream 기본값과 같은 의미
    else:
        norm.pop("tags", None)
        norm.pop("tags_match", None)                    # 태그 없으면 매칭 방식은 무의미
    if isinstance(norm.get("types"), list):
        norm["types"] = sorted(norm["types"])
    blob = json.dumps(norm, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def exclusion_reason(args: Dict[str, Any]) -> Optional[str]:
    q = args.get("query")
    if not isinstance(q, str) or not q.strip():
        return "malformed"
    if scrub_secrets(q) != q:
        return "secret-shape"
    if PROBE_RE.search(q):
        return "probe"
    mt = args.get("max_tokens")
    if isinstance(mt, (int, float)) and not isinstance(mt, bool) and mt < RECALL_MAX_TOKENS_FLOOR:
        return "policy-rejected"
    return None


def build(calls: Iterable[Call]) -> Tuple[Dict[str, Entry], Dict[str, Any]]:
    entries: Dict[str, Entry] = {}
    excluded: collections.Counter = collections.Counter()
    by_source: collections.Counter = collections.Counter()
    calls = list(calls)
    transcript_texts = {norm_text(c.args.get("query")) for c in calls if c.source != "ledger"}
    for c in calls:
        by_source[c.source] += 1
        args = replay_args(c.args)
        reason = exclusion_reason(args)
        if not reason and c.source == "ledger" and norm_text(args.get("query")) in transcript_texts:
            reason = "ledger-shadow"                    # transcript 에 온전한 인자로 이미 있다
        if reason:
            excluded[reason] += 1
            continue
        key = canonical_key(args)
        e = entries.get(key)
        if e is None:
            e = entries[key] = Entry(key=key, args=args)
        e.sources[c.source] += 1
        e.count += 1
        if c.ts:
            e.first_seen = min(filter(None, [e.first_seen, c.ts]))
            e.last_seen = max(filter(None, [e.last_seen, c.ts]))
    stats = {"calls_by_source": dict(by_source), "excluded": dict(excluded),
             "unique": len(entries)}
    return entries, stats


# ---------------------------------------------------------------- 분할·저장

def load_or_create_salt(holdout_dir: str) -> bytes:
    os.makedirs(holdout_dir, mode=0o700, exist_ok=True)
    path = os.path.join(holdout_dir, "split.salt")
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(secrets.token_bytes(32))
        os.chmod(path, 0o600)
    with open(path, "rb") as fh:
        return fh.read()


def is_holdout(salt: bytes, key: str, ratio: float) -> bool:
    digest = hmac.new(salt, key.encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:2], "big") < int(ratio * 65536)


def entry_row(e: Entry) -> Dict[str, Any]:
    return {"key": e.key, "split_key": split_key(e.args), "args": e.args, "count": e.count,
            "sources": dict(e.sources),
            "first_seen": e.first_seen, "last_seen": e.last_seen}


def check_split_version(holdout_dir: str, reset: bool) -> None:
    path = os.path.join(holdout_dir, "split.version")
    if os.path.exists(path):
        cur: Optional[str] = open(path).read().strip()
    elif os.path.exists(os.path.join(holdout_dir, "holdout.jsonl")):
        cur = "key-v0"              # 버전 파일 이전(eecef81)의 분할 - 인자 전체 키 기준. 조용히 넘기지 않는다
    else:
        cur = None                  # 처음 만드는 분할
    if cur not in (None, SPLIT_VERSION) and not reset:
        raise SystemExit(f"split rule changed ({cur} -> {SPLIT_VERSION}): items would move between dev and "
                         f"holdout. Re-run with --reset-split only if no holdout verdict depends on the old split.")
    with open(path, "w") as fh:
        fh.write(SPLIT_VERSION + "\n")
    os.chmod(path, 0o600)


def write_jsonl(path: str, rows: List[Dict[str, Any]]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    blob = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    with open(path, "w") as fh:
        fh.write(blob)
    return hashlib.sha256(blob.encode()).hexdigest()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--holdout-ratio", type=float, default=0.3)
    ap.add_argument("--dev-dir", default=DEV_DIR)
    ap.add_argument("--holdout-dir", default=HOLDOUT_DIR)
    ap.add_argument("--reset-split", action="store_true", help="분할 규칙 버전이 바뀐 것을 승인한다")
    a = ap.parse_args(argv)

    calls = collect_cc() + collect_codex() + collect_ledger()
    entries, stats = build(calls)
    salt = load_or_create_salt(a.holdout_dir)
    check_split_version(a.holdout_dir, a.reset_split)
    rows = sorted((entry_row(e) for e in entries.values()), key=lambda r: r["key"])
    dev = [r for r in rows if not is_holdout(salt, r["split_key"], a.holdout_ratio)]
    hold = [r for r in rows if is_holdout(salt, r["split_key"], a.holdout_ratio)]
    stats.update(dev=len(dev), holdout=len(hold), holdout_ratio=a.holdout_ratio, split_version=SPLIT_VERSION,
                 dev_sha256=write_jsonl(os.path.join(a.dev_dir, "dev.jsonl"), dev),
                 holdout_sha256=write_jsonl(os.path.join(a.holdout_dir, "holdout.jsonl"), hold))
    os.chmod(os.path.join(a.holdout_dir, "holdout.jsonl"), 0o600)      # 새로 만들 때 umask 로 0644 가 되지 않게
    with open(os.path.join(a.dev_dir, "manifest.json"), "w") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
