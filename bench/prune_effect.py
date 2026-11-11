"""#15 운영 재확인 - chunk_id 형식 파생 제거의 절감 폭을 운영 데이터로 잰다 (issue #31).

#15 는 holdout 에서 비용 -3.82% 로 PASS 했지만 배포 뒤에는 스모크만 봤다. 감사 로그에는 응답 크기가 없어서
운영 upstream 에 직접 recall 해 raw 응답을 받고, 같은 응답에 #15 이전 프루너와 현재 프루너를 각각 적용해 비교한다.

  쿼리  audit/recall-queries.log 의 identity=demo·bank=personal-demo 행에서 최근 고유 쿼리 N개(<= 20).
        제외 규칙은 bench/corpus.exclusion_reason 재사용. 인자는 원장에 있는 것(query·tags·tags_match)만 -
        max_tokens·budget 은 생략해 MCP 기본값으로 둔다.
  호출  운영 upstream MCP 직결: `docker compose exec hindsight` 안에서 POST /mcp + X-Bank-Id.
        게이트웨이(8888 호스트)를 거치지 않는다 - 감사 원장·recall ledger 오염 방지. REST 는 MCP 와 필드 집합이 달라
        대조군으로 못 쓴다. 토큰은 컨테이너 env(HINDSIGHT_API_MCP_AUTH_TOKEN)에서 읽으므로 호스트 argv 에 안 나온다.
        직렬(한 프로세스, 한 세션, 순차), MAX_CALLS 이하.
  비교  이전 = `git show 92beb6c^:gateway/recall_prune.py` 를 임시 모듈로 로드(리포에 사본 없음), 현재 = gateway.recall_prune.
        공개판(내보내기로 sha 가 바뀐 이력)에서는 `Exported-From: <92beb6c 전체 sha>` trailer 가 달린 커밋의 부모를 쓴다(#447).
        토큰 = 클라이언트가 받는 content 텍스트의 o200k 토큰(bench/runner 와 같은 정의).
        결과 불변 = 건수·순서·chunk_id 외 모든 필드·payload 나머지·structuredContent 일치, 사라진 chunk_id 는 전부
        `{bank}_{document_id}_0`(복원 가능).
  출력  ~/.local/share/hindsight-bench/prune-effect/<UTC stamp>/{raw.jsonl, report.json}. 호출 창(KST)은 #22 카나리
        기준선의 제외 구간으로 쓴다.

실행 (메인 체크아웃의 compose·원장을 쓴다. 값은 출력하지 않는다):
  uv run --python 3.14 --no-project --with tiktoken --with-requirements gateway/requirements.txt \\
      python -B -m bench.prune_effect --compose-dir <운영 체크아웃 경로>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import types
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bench.corpus import exclusion_reason, norm_text  # noqa: E402
from bench.runner import McpClient, classify  # noqa: E402

BANK = "personal-demo"
OLD_REV = "92beb6c^"                 # #15 병합 직전 - chunk_id 형식 파생 제거가 없는 프루너
OLD_COMMIT = "92beb6c9918be5cb7e99e658caed61ecea862402"   # OLD_REV 의 자식(#15 커밋) 운영판 전체 sha
EXPORT_TRAILER = "Exported-From"     # 공개판 내보내기가 모든 커밋에 다는 원본 sha trailer(scripts/public-export)
MAX_CALLS = 20
OUT_ROOT = os.path.expanduser("~/.local/share/hindsight-bench/prune-effect")
KST = dt.timezone(dt.timedelta(hours=9))
LEDGER_ARGS = ("query", "tags", "tags_match")

# 컨테이너 안에서 돈다. stdin = {"bank", "queries"}. 호출마다 한 줄 JSON(원문 본문 포함)을 stdout 에 쓴다.
_MCP_RECALL = r'''
import json, os, sys, time, urllib.request as u, urllib.error as e
spec = json.load(sys.stdin)
url = "http://localhost:8888/mcp"
base = {"Authorization": "Bearer " + os.environ["HINDSIGHT_API_MCP_AUTH_TOKEN"], "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "X-Bank-Id": spec["bank"]}
def post(payload, session=None, timeout=120):
    h = dict(base)
    if session:
        h["Mcp-Session-Id"] = session
    req = u.Request(url, data=json.dumps(payload).encode(), headers=h, method="POST")
    try:
        with u.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("content-type", ""), r.headers.get("mcp-session-id"), r.read()
    except e.HTTPError as x:
        return x.code, x.headers.get("content-type", ""), None, x.read()
code, _, session, _ = post({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "hc-prune-effect", "version": "1"}}})
if code != 200 or not session:
    print(json.dumps({"fatal": "initialize", "http": code})); sys.exit(2)
post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
for i, args in enumerate(spec["queries"]):
    t0 = time.time()
    try:
        code, ctype, _, body = post({"jsonrpc": "2.0", "id": i + 1, "method": "tools/call",
                                     "params": {"name": "recall", "arguments": args}}, session)
        rec = {"i": i, "http": code, "ctype": ctype, "body": body.decode("utf-8", "replace")}
    except Exception as x:
        rec = {"i": i, "transport_error": type(x).__name__}
    rec.update(t0=t0, t1=time.time())
    print(json.dumps(rec, ensure_ascii=False), flush=True)
'''


# ---------------------------------------------------------------- 쿼리

def select_queries(ledger: str, n: int = MAX_CALLS) -> List[Dict[str, Any]]:
    """원장에서 demo·personal-demo 의 최근 고유 쿼리 n개(최신순). 고유 = 정규화 텍스트 기준, 가장 최근 행의 인자."""
    rows = []
    with open(ledger, errors="ignore") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("identity") == "demo" and row.get("bank") == BANK:
                rows.append(row)
    # ts 는 초 단위라 같은 초가 흔하다 - 원장 줄 순서(추가 순)를 동률 깨기로 써서 나중 행이 먼저 오게 한다
    order = sorted(range(len(rows)), key=lambda j: (str(rows[j].get("ts", "")), j), reverse=True)
    out, seen = [], set()
    for row in (rows[j] for j in order):
        if len(out) >= n:
            break
        if row.get("reusable") is False:
            continue                  # 스크럽·절단·원래 추가 필터가 있던 행 - query·tags 만으로는 운영 호출을 재현 못 한다
        args = {k: row[k] for k in LEDGER_ARGS if row.get(k)}
        if exclusion_reason(args):
            continue
        key = norm_text(args["query"])
        if key in seen:
            continue
        seen.add(key)
        out.append(args)
    return out


# ---------------------------------------------------------------- 프루너

def load_pruner(source: str, name: str = "gateway._recall_prune_pre15") -> types.ModuleType:
    """프루너 소스를 임시 모듈로 올린다. `from . import reframe` 가 풀리도록 gateway 패키지 소속으로 둔다."""
    mod = types.ModuleType(name)
    mod.__package__ = "gateway"
    exec(compile(source, f"<{OLD_REV}:gateway/recall_prune.py>", "exec"), mod.__dict__)
    return mod


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


NOT_A_REPO = "git 리포가 아니다(ZIP·tarball 같은 사본) - 기준 판본을 git 이력에서 읽을 수 없다"
NOT_IN_HISTORY = (f"기준 판본({OLD_REV}, #15 직전)이 이 리포 HEAD 이력에 없다 - 공개판이면 {EXPORT_TRAILER} trailer 커밋이 "
                  "HEAD 이력에 없고, shallow checkout 이면 전체 이력을 받아야 한다")
NO_HEAD = "HEAD 커밋이 없다(커밋 없는 리포) - 기준 판본을 찾을 이력이 없다"


def locate_old_rev(repo: str = REPO) -> Tuple[Optional[str], str]:
    """기준 판본(OLD_REV, #15 커밋의 부모)을 이 리포에서 가리키는 rev 와, 못 찾았을 때의 사유.

    운영판은 OLD_COMMIT sha 그대로다. 공개판은 내보내기가 sha 를 모두 바꾸지만 각 커밋 메시지 맨 끝에
    `Exported-From: <원본 sha>` 를 남기므로, HEAD 이력에서 메시지 마지막 줄이 그 trailer 이고 값이 OLD_COMMIT 인 커밋을 찾아
    그 부모를 쓴다. 빈 커밋이 빠져도 부모 자리의 트리는 원본 부모를 거른 트리와 같다.
    찾으면 (rev, ""), 못 찾으면 (None, 사유). 후보가 둘 이상이면 RuntimeError.
    """
    if _git(repo, "rev-parse", "--git-dir").returncode != 0:                   # git 리포가 아닌 사본(ZIP·tarball)
        return None, NOT_A_REPO
    if _git(repo, "cat-file", "-e", OLD_COMMIT + "^^{commit}").returncode == 0:   # 부모까지 있어야(shallow 경계 제외)
        return OLD_COMMIT + "^", ""
    if _git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}").returncode != 0:   # unborn HEAD
        return None, NO_HEAD
    p = _git(repo, "log", "HEAD", "--format=%H", "--basic-regexp",              # grep.patternType 설정 무시
             f"--grep=^{EXPORT_TRAILER}: {OLD_COMMIT}$")                        # HEAD 이력만 - 다른 ref 의 옛 내보내기 제외
    if p.returncode != 0:
        raise RuntimeError(f"git log failed (rc={p.returncode}): {p.stderr.strip()[-200:]}")
    hits = [h for h in p.stdout.split() if _last_exported_from(repo, h) == OLD_COMMIT]   # 본문 인용 줄은 제외
    if len(hits) > 1:
        raise RuntimeError(f"{EXPORT_TRAILER}: {OLD_COMMIT} 커밋이 {len(hits)}개 - 기준 판본이 모호하다")
    if not hits or _git(repo, "cat-file", "-e", hits[0] + "^^{commit}").returncode != 0:
        return None, NOT_IN_HISTORY
    return hits[0] + "^", ""


def _last_exported_from(repo: str, commit: str) -> str:
    """메시지 마지막 줄의 Exported-From 값(내보내기 cb_commit 이 맨 끝에 단다 - export.py 도 마지막 줄로 검증). 없으면 ""."""
    p = _git(repo, "log", "-1", "--format=%B", commit)
    if p.returncode != 0:
        raise RuntimeError(f"git log failed (rc={p.returncode}): {p.stderr.strip()[-200:]}")
    last = p.stdout.rstrip("\n").rsplit("\n", 1)[-1]
    m = re.match(rf"{EXPORT_TRAILER}: (\S*)\s*$", last)
    return m.group(1) if m else ""


def resolve_old_rev(repo: str = REPO) -> Optional[str]:
    """기준 판본 rev. 없으면 None(사유는 locate_old_rev)."""
    return locate_old_rev(repo)[0]


def old_pruner_source(repo: str = REPO) -> str:
    rev, why = locate_old_rev(repo)
    if rev is None:
        raise RuntimeError(why)
    return subprocess.run(["git", "-C", repo, "show", f"{rev}:gateway/recall_prune.py"],
                          capture_output=True, text=True, check=True).stdout


# ---------------------------------------------------------------- 비교

def _cls(result: Any) -> Dict[str, Any]:
    return classify({"msg": {"jsonrpc": "2.0", "result": result}})


def _canon_text(result: Any) -> str:
    """토큰은 두 쪽 모두 같은 직렬화(recall_prune._dumps)로 다시 쓴 뒤 센다. 이전 프루너가 손대지 않은 응답은
    upstream 원문(indent=2, \\u 이스케이프)이고 손댄 응답은 compact 라서, 원문 그대로 세면 직렬화 차이가 절감에 섞인다."""
    blocks = result.get("content") if isinstance(result, dict) else None
    texts = [b.get("text", "") for b in blocks or [] if isinstance(b, dict) and b.get("type") == "text"]
    out = []
    for t in texts:
        try:
            out.append(_dumps(json.loads(t)))
        except ValueError:
            out.append(t)
    return "\n".join(out)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _structured_payload(result: Any) -> Any:
    sc = result.get("structuredContent") if isinstance(result, dict) else None
    inner = sc.get("result", sc) if isinstance(sc, dict) else sc
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            return inner
    if isinstance(inner, dict) and isinstance(inner.get("results"), list):
        inner = {**inner, "results": [{k: v for k, v in r.items() if k != "chunk_id"} if isinstance(r, dict) else r
                                      for r in inner["results"]]}
    return inner


def compare(result: Any, bank: str, old: Callable[[Any], Any], new: Callable[[Any, str], Any],
            tok: Any) -> Dict[str, Any]:
    """같은 raw CallToolResult 에 두 프루너를 적용해 토큰과 결과 불변을 잰다. 본문은 기록하지 않는다(키 이름만)."""
    old_out, new_out = old(result), new(result, bank)
    a, b = _cls(old_out), _cls(new_out)
    rec: Dict[str, Any] = {"outcome": a["outcome"], "tokens_old": tok.count(_canon_text(old_out)),
                           "tokens_new": tok.count(_canon_text(new_out)), "n_results": 0,
                           "chunk_id_restorable": 0, "chunk_id_kept": 0, "diff_other": []}
    if a["outcome"] != "ok" or b["outcome"] != "ok":
        rec["invariant"] = None if a["outcome"] == b["outcome"] else False
        if rec["invariant"] is False:
            rec["diff_other"].append(f"outcome {a['outcome']} -> {b['outcome']}")
        return rec
    diffs = rec["diff_other"]
    sa, sb = a["stored"], b["stored"]
    for k in ("payload_rest", "blocks"):
        if sa[k] != sb[k]:
            diffs.append(k)
    if sb["structured"] != sa["structured"]:
        diffs.append(f"structured {sa['structured']} -> {sb['structured']}")
    elif _structured_payload(old_out) != _structured_payload(new_out):
        diffs.append("structuredContent value")          # 라벨이 같아도(둘 다 inconsistent 등) 값이 다르면 차이
    ra, rb = a["results"], b["results"]
    rec["n_results"] = len(ra)
    if len(ra) != len(rb):
        diffs.append(f"count {len(ra)} -> {len(rb)}")
    for i, (x, y) in enumerate(zip(ra, rb)):
        for k in sorted((set(x) | set(y)) - {"chunk_id"}):
            if x.get(k, KeyError) != y.get(k, KeyError):
                diffs.append(f"result[{i}].{k}")
        if "chunk_id" in x and "chunk_id" in y:
            if x["chunk_id"] == y["chunk_id"]:
                rec["chunk_id_kept"] += 1
            else:
                diffs.append(f"result[{i}].chunk_id changed")
        elif "chunk_id" in x:
            if "document_id" in y and x["chunk_id"] == f"{bank}_{y['document_id']}_0":
                rec["chunk_id_restorable"] += 1
            else:
                diffs.append(f"result[{i}].chunk_id dropped (not restorable)")
        elif "chunk_id" in y:
            diffs.append(f"result[{i}].chunk_id added")
    rec["invariant"] = not diffs
    return rec


def summarize(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok = [r for r in recs if r["outcome"] == "ok"]
    old = sum(r["tokens_old"] for r in ok)
    new = sum(r["tokens_new"] for r in ok)
    return {"queries": len(recs), "ok": len(ok), "invariant": sum(1 for r in ok if r["invariant"] is True),
            "tokens_old": old, "tokens_new": new,
            "saving_pct": (100.0 * (new - old) / old) if old else None,
            "results": sum(r["n_results"] for r in ok),
            "chunk_id_restorable": sum(r["chunk_id_restorable"] for r in ok),
            "chunk_id_kept": sum(r["chunk_id_kept"] for r in ok),
            "queries_saved": sum(1 for r in ok if r["tokens_new"] < r["tokens_old"]),
            "queries_grown": sum(1 for r in ok if r["tokens_new"] > r["tokens_old"])}


# ---------------------------------------------------------------- 호출

def kst(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, KST).isoformat(timespec="milliseconds")


def parse_line(line: str) -> Dict[str, Any]:
    try:
        rec = json.loads(line)
    except ValueError:
        rec = None
    if not isinstance(rec, dict):         # 잡음·잘린 줄: 오류 레코드로 남기고 나머지 줄·창·산출물은 계속 기록한다
        return {"i": None, "t0": None, "t1": None, "http": None, "elapsed_s": None, "result": None,
                "error": "non-json-line"}
    out = {"i": rec.get("i"), "t0": rec.get("t0"), "t1": rec.get("t1"), "http": rec.get("http"),
           "elapsed_s": (rec["t1"] - rec["t0"]) if rec.get("t0") is not None and rec.get("t1") is not None else None,
           "result": None, "error": rec.get("transport_error") or rec.get("fatal")}
    if "body" in rec:
        msg = McpClient.parse(rec.get("ctype", ""), rec["body"].encode("utf-8"))
        if isinstance(msg, dict) and "result" in msg:
            out["result"] = msg["result"]
        elif not out["error"]:
            out["error"] = "rpc-error" if isinstance(msg, dict) else "unparseable"
    return out


# issue #129: compose-go dotenv 파서는 형식이 틀린 .env 줄 원문(`=값`·따옴표 값)을 오류에 싣는다. stderr 는 기록·전송하기 전에
# 이 함수를 거친다 - 줄 번호·오류 종류·일반 문구는 남기고 값만 가린다. import 경로를 묶지 않으려고 파일마다 같은 헬퍼를 둔다.
_DOTENV_LINE = re.compile(r"\bline \d+: ")
_UNTERMINATED = re.compile(r"(unterminated quoted value ).*")
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')                  # %q 가 만든 따옴표 문자열(이스케이프 포함)
_ASSIGN = re.compile(r"""(?P<k>[A-Za-z_][\w.\[\]-]*)=(?:"(?P<q>(?:[^"\\]|\\.)*)"?|'[^']*'?|\S*)""")
# compose 경고 줄(time="..." level=warning msg="...")은 허용 목록에 맞는 값만 남긴다. 허용 밖은 전부 가린다(유출보다 진단 손실이 낫다).
_LOG_LEVELS = frozenset(("trace", "debug", "info", "warn", "warning", "error", "fatal", "panic"))
_TIMESTAMP = re.compile(r"[0-9T:.Z+-]+")


def _redact_assign(a, logfmt_ok):
    k, q = a["k"], a["q"]
    if logfmt_ok and (a.start() == 0 or a.string[a.start() - 1].isspace()):      # 키 앞이 줄 시작 또는 공백일 때만
        v = q if q is not None else a.group(0)[len(k) + 1:]
        if (k == "time" and _TIMESTAMP.fullmatch(v)) or (k == "level" and v in _LOG_LEVELS) \
                or (k == "msg" and q is not None and a.group(0) == f'{k}="{q}"' and "=" not in q):
            return a.group(0)
    return k + "=<redacted>"


def _mask_stderr(text: str) -> str:
    out = []
    for ln in (text or "").removesuffix("\n").split("\n"):    # Go 는 \n 에서만 줄을 자른다 - splitlines() 는 U+2028 등에서도 쪼갠다
        ln = ln.removesuffix("\r")
        m = _DOTENV_LINE.search(ln)
        if m:                                             # dotenv 오류 줄: `line N:` 뒤의 따옴표 값은 전부 가린다
            rest = _UNTERMINATED.sub(r"\1<redacted>", ln[m.end():])
            rest = re.sub(r'"[^"]*$', "<redacted>", _QUOTED.sub("<redacted>", rest))
            ln = ln[:m.end()] + rest
        out.append(_ASSIGN.sub(lambda a: _redact_assign(a, m is None), ln))
    return "\n".join(out)


def call_upstream(queries: List[Dict[str, Any]], compose_dir: str) -> Dict[str, Any]:
    if len(queries) > MAX_CALLS:
        raise SystemExit(f"refusing {len(queries)} calls (> {MAX_CALLS})")
    cmd = ["docker", "compose", "--env-file", ".env.local", "exec", "-T", "hindsight", "python", "-c", _MCP_RECALL]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=compose_dir, input=json.dumps({"bank": BANK, "queries": queries}),
                              capture_output=True, text=True)
        rc, stdout, stderr = proc.returncode, proc.stdout or "", proc.stderr or ""
    except OSError as e:                  # docker 없음 등 - 창은 그래도 남긴다
        rc, stdout, stderr = -1, "", type(e).__name__
    t1 = time.time()
    calls = [parse_line(l) for l in stdout.splitlines() if l.strip()]
    return {"rc": rc, "stderr_tail": _mask_stderr(stderr)[-500:], "t0": t0, "t1": t1, "calls": calls}


def _out(cmd: List[str], cwd: Optional[str] = None) -> Optional[str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _run(cmd: List[str], cwd: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """(stdout, None) 또는 (None, 실패 이유). _out 과 같은 호출이되 이유를 버리지 않는다."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError as e:
        return None, type(e).__name__
    if p.returncode != 0:
        return None, f"rc={p.returncode} {_mask_stderr(p.stderr or '').strip()[-200:]}".strip()
    return p.stdout.strip(), None


def _hindsight_image(compose_dir: str) -> Dict[str, Any]:
    """측정 대상(docker compose exec hindsight)의 upstream 이미지: id·설정 태그·RepoDigests. 이 이미지는 latest 태그라
    같은 태그가 다른 이미지일 수 있어 id·digest 로 못 박는다. 못 읽으면 값 None 으로만 두지 않고 error 에 어디서 왜 실패했는지 적는다."""
    out: Dict[str, Any] = {"id": None, "ref": None, "repo_digests": None, "error": None}
    cid, err = _run(["docker", "compose", "--env-file", ".env.local", "ps", "-q", "hindsight"], cwd=compose_dir)
    if not cid:
        out["error"] = f"container-not-found: {err or 'no running container'}"
        return out
    info, err = _run(["docker", "inspect", "-f", "{{.Image}}\t{{.Config.Image}}", cid.splitlines()[0]])
    if not info:
        out["error"] = f"inspect-failed: {err or 'empty output'}"
        return out
    out["id"], _, out["ref"] = info.partition("\t")
    digests, err = _run(["docker", "image", "inspect", "-f", "{{json .RepoDigests}}", out["id"]])
    try:
        out["repo_digests"] = json.loads(digests) if digests else None
    except ValueError:
        err = err or f"unparsable: {digests[:80]}"
    if out["repo_digests"] is None:
        if digests == "null":              # inspect 는 성공했고 RepoDigests 필드가 null(로컬 빌드·push 안 한 이미지) - 실패가 아니다
            out["error"] = "no-repo-digests"
        else:
            out["error"] = f"digest-inspect-failed: {err or 'empty output'}"
    return out


def provenance(compose_dir: str) -> Dict[str, Any]:
    """어떤 "현재" 프루너로 쟀는지: 실행 체크아웃 HEAD·dirty·recall_prune.py blob, 운영 게이트웨이 이미지 id, 측정이 실제로
    도는 hindsight(upstream) 이미지 id·digest. 값만 읽는다(docker compose ps / inspect). 못 읽으면 None, hindsight 는 error 사유."""
    status = _out(["git", "-C", REPO, "status", "--porcelain", "--untracked-files=no"])
    cid = _out(["docker", "compose", "--env-file", ".env.local", "ps", "-q", "gateway"], cwd=compose_dir)
    image = _out(["docker", "inspect", "-f", "{{.Image}}", cid.splitlines()[0]]) if cid else None
    return {"repo_head": _out(["git", "-C", REPO, "rev-parse", "HEAD"]),
            "repo_dirty": None if status is None else bool(status),
            "recall_prune_sha": _out(["git", "-C", REPO, "hash-object", os.path.join(REPO, "gateway", "recall_prune.py")]),
            "gateway_image": image,
            "hindsight_image": _hindsight_image(compose_dir)}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--compose-dir", required=True, help="운영 compose 체크아웃(.env.local 이 있는 곳)")
    ap.add_argument("--ledger", help="recall 원장 (기본: <compose-dir>/audit/recall-queries.log)")
    ap.add_argument("-n", type=int, default=MAX_CALLS)
    ap.add_argument("--out-root", default=OUT_ROOT)
    a = ap.parse_args(argv)
    n = min(a.n, MAX_CALLS)

    from bench.runner import Tokenizer
    from gateway import recall_prune
    tok = Tokenizer()
    old_mod = load_pruner(old_pruner_source())
    if hasattr(old_mod, "FORMAT_DERIVABLE"):
        raise SystemExit(f"{OLD_REV} pruner already has FORMAT_DERIVABLE - wrong baseline")

    queries = select_queries(a.ledger or os.path.join(a.compose_dir, "audit", "recall-queries.log"), n)
    if not queries:
        raise SystemExit("no queries selected - nothing to measure")
    prov = provenance(a.compose_dir)
    run = call_upstream(queries, a.compose_dir)

    recs = []
    for c in run["calls"]:
        if c["result"] is None:
            recs.append({"i": c["i"], "outcome": c["error"] or "no-result", "http": c["http"], "tokens_old": 0,
                         "tokens_new": 0, "n_results": 0, "chunk_id_restorable": 0, "chunk_id_kept": 0,
                         "invariant": None, "diff_other": []})
            continue
        rec = compare(c["result"], BANK, lambda r: old_mod.prune_tool_result(r)[0],
                      lambda r, b: recall_prune.prune_tool_result(r, b)[0], tok)
        rec.update(i=c["i"], http=c["http"], elapsed_s=round(c["elapsed_s"], 3))
        recs.append(rec)

    starts = [c["t0"] for c in run["calls"] if c.get("t0")]
    ends = [c["t1"] for c in run["calls"] if c.get("t1")]
    report = {"issue": 31, "bank": BANK, "old_rev": OLD_REV, "surface": "upstream MCP direct (docker compose exec)",
              "provenance": prov, "requested": len(queries), "calls": len(run["calls"]), "rc": run["rc"],
              "window_kst": {"start": kst(run["t0"]), "end": kst(run["t1"])},
              "calls_window_kst": {"start": kst(min(starts)), "end": kst(max(ends))} if starts else None,
              "summary": summarize(recs), "per_query": recs}
    if run["rc"] != 0:
        report["stderr_tail"] = run["stderr_tail"]

    out_dir = os.path.join(a.out_root, dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    raw_path = os.path.join(out_dir, "raw.jsonl")
    with open(os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        for c in run["calls"]:
            i = c.get("i")
            q = queries[i] if isinstance(i, int) and 0 <= i < len(queries) else None     # fatal 줄은 쿼리가 없다
            fh.write(json.dumps({"args": q, **c}, ensure_ascii=False) + "\n")
    report_path = os.path.join(out_dir, "report.json")
    with open(os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k != "per_query"}, ensure_ascii=False, indent=2))
    print(f"out: {out_dir}")
    s = report["summary"]
    return 0 if run["rc"] == 0 and s["ok"] == s["queries"] == len(queries) and s["invariant"] == s["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
