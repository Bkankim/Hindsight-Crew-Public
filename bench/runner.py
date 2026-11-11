"""recall 재생 하네스 - 측정기 (issue #13).

코퍼스의 실사용 recall 을 게이트웨이 팔(arm)들에 **실제 클라이언트 경로(MCP, demo 토큰)**로 재생하고
호출마다 결과·토큰·지연을 기록한다. 판정은 하지 않는다(judge.py 몫). 측정 원칙:

- 직렬 실행. 동시 호출은 upstream TEI 세마포어(1)에서 서로를 기다리게 만들어 지연을 오염시킨다.
- 교차 배치. 라운드마다 쿼리 순서를 시드 셔플하고, 쿼리마다 팔 순서를 회전·역순시킨다(arm_order, 팔 2개면
  ABBA). 시간 드리프트와 자리 효과가 팔들에 대칭으로 걸린다. 판정용 run 은 기준·대조·후보 3팔을 권장한다.
- 시계 고정. recall 점수의 recency 와 "어제·지난주" 해석이 서버 시계(utcnow)를 쓰므로, 인자에
  query_timestamp 가 없으면 벤치 시계를 넣는다. 원래 인자에 있으면 그대로 둔다.
- 간섭 기록. 리랭크 사이드카는 운영과 공유된다. 큐 깊이(/info)는 대기 작업만 세서 운영 요청이 처리 중인 순간을
  못 본다(2026-09-23 재리뷰: 1,144호출 중 오염 0건). 그래서 run 동안 운영 게이트웨이 감사 로그에 새로 찍힌
  리랭크 사용 요청(recall·reflect·context-triggers·멘탈모델)의 시각을 prod_events.jsonl 로 남기고, 판정기가
  호출 구간(wall_start~wall_end)과 겹친 쌍을 뺀다. 감사 로그에는 쿼리 원문이 없다(시각·종류만 옮긴다).
- 워밍업은 측정 코퍼스와 겹치지 않는 쿼리로 한다. 측정 쿼리로 워밍업하면 후보의 캐시가 미리 채워진다.
  지연이 안정될 때까지(연속 두 창의 중앙값 차 < 5%) 돌고 --warmup 이 상한이다.
- 호출 간 고정 간격(--gap-ms, 기본 500). 연달아 치면 앞 호출의 upstream 뒷정리와 겹쳐 다음 호출이 느려지고,
  느린 팔(응답 뒤 지연) 뒤의 호출은 쉬는 시간을 얻어 빨라진다 - 2026-09-23 실측: +400ms fixture 바로 뒤 팔이
  2~3% 빨라져 A/A 분산이 두 배가 됐다. 모든 호출에 같은 휴지를 주면 이월 효과가 줄고 운영의 드문 호출에도 가깝다.
- 간섭 가드(bench/guard.py, issue #19). 시작 전과 호출마다 호스트 swapout·압박·여유 메모리를 보고 임계를 넘으면
  재개될 때까지 멈춘다(정지 구간은 meta.guard.windows - 판정기가 겹친 호출을 뺀다). 가드가 중단을 내면 측정을
  이어가지 않고 meta 를 남긴 뒤 끝낸다. run 동안 운영 사이드카 로그의 503·데드라인 초과는 meta.prod_sidecar 로 센다.
- keep-warm(bench/keepwarm.py, issue #79). 가드는 호출 사이에서만 멈추므로 이미 도는 벤치 호출과 운영 cold 요청의 충돌을 못 막는다
  (#21 운영 503 6건). run 동안 호출 앞에서 운영 사이드카에 아주 작은 리랭크 요청을 간격마다 보내 cold 로 떨어지지 않게 한다.
  보낸 시각·실패와 run 구간(카나리 제외 파일 형식 한 줄)은 meta.keepwarm 에 남고, 실패해도 run 은 멈추지 않는다.
  CLI(main)만 실제 송신기를 켠다 - run() 은 keepwarm 을 주입받을 때만 보낸다(테스트가 운영 사이드카를 치지 않게).
- 운영 보호. 팔 포트는 벤치가 띄운 컨테이너(hc-bench-gw-*, label hc-bench=1)여야 한다 - 오타로 운영(8888)을
  demo 토큰으로 치면 운영 원장·재발 탐지 입력이 오염된다.
- holdout run 산출물(결과 본문 포함)은 holdout 디렉터리 아래(0700)에 쓴다 - dev 쪽에서 보이지 않게.
- 출처. run meta 에 복제본(백업 sha·이미지·시계)·팔(commit·patch·image)·동결 지문(시작/끝)·팔별
  rewrite_failures(시작/끝)를 싣는다. 판정기는 지문이 달라졌거나 rewrite 실패가 늘었으면 판정을 거부한다.
- 토큰은 클라이언트 LLM 이 실제로 받는 tool content 텍스트를 o200k 로 센다. 결과 비교용으로는 payload 전체
  (results 밖 최상위 키, 추가 text 블록, structuredContent 일관성)를 저장한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import random
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from typing import Any, Dict, List, Optional, Tuple

from bench.guard import Guard, GuardAbort, _stat, sidecar_errors_since, sidecar_log_offset
from bench.keepwarm import KeepWarm

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_DIR = os.path.join(REPO, "bench", ".data", "runs")
DEV = os.path.join(REPO, "bench", ".data", "dev.jsonl")
HOLDOUT = os.path.expanduser("~/.local/share/hindsight-bench/holdout/holdout.jsonl")
HOLDOUT_RUNS_DIR = os.path.expanduser("~/.local/share/hindsight-bench/holdout/runs")
STATE_DIR = os.path.expanduser("~/.local/share/hindsight-bench/state")
PROD_AUDIT = os.path.join(REPO, "audit", "gateway.log")
PROD_PORT = 8888
CONTEXT_TRIGGER_DECISION = "context-trigger"     # gateway/context_triggers.py DECISION - 길목 주입은 allow 줄이 없다
CONTEXT_TRIGGER_MISS = "miss"                          # reason 기본값: 매칭 트리거가 없어 upstream recall 을 부르지 않는다 (issue #62)
# MCP 멘탈 모델 조작 중 reflect 를 돌리는 것(생성은 source_query 로, refresh 는 재생성). update_mental_model 은 SQL UPDATE 뿐이라
# 제외(재생성은 refresh 로). get·list·delete·clear 도 제외 (issue #62)
MENTAL_MODEL_RERANK_TOOLS = ("create_mental_model", "refresh_mental_model")
TEI_INFO = "http://127.0.0.1:8787/info"
BANK = "personal-demo"
CALL_TIMEOUT = 90.0
WARMUP_MIN = 10              # 워밍업 최소 쿼리 수
WARMUP_WINDOW = 10           # 안정 판정 창(기준 팔 호출 수)
WARMUP_STABLE = 0.05         # 연속 두 창 중앙값의 상대 차가 이보다 작으면 안정
# 측정 코퍼스와 겹치는 워밍업 풀이 없을 때(dev 전체 run) 쓰는 합성 쿼리 - 결과는 기록하지 않는다
WARMUP_SYNTH = ("최근 결정 사항", "게이트웨이 운영 규칙", "배포 검증 절차", "recall 튜닝 기준",
                "멘탈 모델 갱신", "태그 정규화", "리랭커 지연", "백업 복원")


# ---------------------------------------------------------------- 토큰

class Tokenizer:
    def __init__(self) -> None:
        import tiktoken                      # uv run --with tiktoken
        self.enc = tiktoken.get_encoding("o200k_base")

    def count(self, text: str) -> int:
        return len(self.enc.encode(text, disallowed_special=()))


# ---------------------------------------------------------------- MCP 클라이언트

class McpClient:
    """게이트웨이 /mcp/<bank> 에 붙는 최소 MCP(streamable HTTP) 클라이언트. 세션은 팔마다 1개."""

    def __init__(self, port: int, token: str) -> None:
        self.url = f"http://127.0.0.1:{port}/mcp/{BANK}"
        self.token = token
        self.session: Optional[str] = None
        self.next_id = 1

    def _post(self, payload: Dict[str, Any], timeout: float = CALL_TIMEOUT) -> Tuple[int, Dict[str, str], bytes]:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()

    def open(self) -> None:
        self.session = None
        code, hdr, _ = self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                   "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                              "clientInfo": {"name": "hc-bench", "version": "1"}}})
        if code != 200:
            raise RuntimeError(f"initialize {code} on {self.url}")
        self.session = hdr.get("mcp-session-id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    @staticmethod
    def parse(content_type: str, body: bytes) -> Optional[Dict[str, Any]]:
        text = body.decode("utf-8", "replace")
        if "text/event-stream" in (content_type or ""):
            # SSE: 이벤트는 빈 줄로 끝나고, 한 이벤트의 data: 줄이 여럿이면 개행으로 이어 붙인다(스펙).
            # JSON-RPC 응답(result/error)을 담은 마지막 이벤트를 고른다.
            msg, data = None, []
            for line in text.splitlines() + [""]:
                if line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
                elif line == "" and data:
                    try:
                        cand = json.loads("\n".join(data))
                    except ValueError:
                        cand = None
                    data = []
                    if isinstance(cand, dict) and ("result" in cand or "error" in cand):
                        msg = cand
            return msg
        try:
            return json.loads(text)
        except ValueError:
            return None

    def recall(self, args: Dict[str, Any]) -> Dict[str, Any]:
        rid = self.next_id
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                   "params": {"name": "recall", "arguments": args}}
        t0 = time.perf_counter()
        try:
            code, hdr, body = self._post(payload)
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            return {"latency_ms": (time.perf_counter() - t0) * 1000, "outcome": "transport-error",
                    "detail": type(e).__name__}
        latency = (time.perf_counter() - t0) * 1000
        msg = self.parse(hdr.get("content-type", ""), body)
        return {"latency_ms": latency, "http": code, "bytes": len(body), "msg": msg}


# ---------------------------------------------------------------- 결과 정규화

def classify(raw: Dict[str, Any]) -> Dict[str, Any]:
    """호출 1건 -> 결과 분류와 비교용 정규형. 결과 본문은 판정에 쓰도록 results 로 남긴다."""
    if raw.get("outcome") == "transport-error":
        return {"outcome": "transport-error", "detail": raw.get("detail")}
    msg = raw.get("msg")
    if not isinstance(msg, dict):
        return {"outcome": "unparseable", "http": raw.get("http")}
    if "error" in msg:
        return {"outcome": "rpc-error", "code": (msg["error"] or {}).get("code")}
    res = msg.get("result") or {}
    texts = [b.get("text", "") for b in res.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"]
    content_text = "\n".join(texts)
    if res.get("isError"):
        rule = next((t.split("RULE:", 1)[1].split()[0] for t in texts if "RULE:" in t), None)
        return {"outcome": "tool-error", "rule": rule, "content_text": content_text, "stored": {"text": content_text}}
    payload: Any = None
    try:
        payload = json.loads(texts[0]) if texts else None
    except ValueError:
        payload = None
    if isinstance(payload, dict) and "error" in payload and not payload.get("results"):
        # upstream 은 recall 예외를 isError 가 아니라 {"error": .., "results": []} 로 돌려준다(mcp_tools.py:925).
        # ok 로 두면 빈 결과가 prefix-cut·비용 개선으로 둔갑한다.
        return {"outcome": "upstream-error", "content_text": content_text, "stored": {"text": content_text}}
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        results = payload["results"]
        stored = {"results": results,
                  "payload_rest": {k: v for k, v in payload.items() if k != "results"},
                  "blocks": texts[1:],
                  "structured": structured_state(res.get("structuredContent"), payload)}
        return {"outcome": "ok", "content_text": content_text, "results": results, "stored": stored,
                "extra_blocks": len(texts) - 1,
                "top_keys": sorted(k for k in payload if k != "results")}
    # 결과 목록이 없는 응답도 클라이언트가 받는 텍스트는 저장해 비교한다 - 안 그러면 같은 오류 분류 안에서
    # 텍스트만 줄인 후보가 "정확도 동일 + 비용 개선"으로 통과한다(final-review 2026-09-23)
    return {"outcome": "ok-unstructured", "content_text": content_text, "stored": {"text": content_text}}


def structured_state(structured: Any, payload: Dict[str, Any]) -> str:
    """structuredContent 가 content 와 같은 내용인지. upstream 은 structuredContent.result 에 같은 JSON 을
    문자열로 한 번 더 담는다(이중 인코딩). 어떤 클라이언트는 이쪽을 모델에 넘기므로, 한쪽만 바꾸는 후보를 잡는다."""
    if structured is None:
        return "absent"
    inner = structured.get("result", structured) if isinstance(structured, dict) else structured
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            return "inconsistent"
    return "consistent" if json.dumps(inner, sort_keys=True) == json.dumps(payload, sort_keys=True) else "inconsistent"


def result_digest(stored: Any) -> str:
    return hashlib.sha256(json.dumps(stored, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 간섭 신호

def tei_queue_depth(url: str = TEI_INFO) -> Optional[int]:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return int(json.loads(r.read()).get("queue_depth"))
    except Exception:
        return None


def arm_queue_urls(replica: Optional[Dict[str, Any]]) -> List[str]:
    """팔이 기다릴 수 있는 사이드카 큐. 운영 사이드카는 벤치 사이드카와 GPU 를 공유하므로 세트 팔도 늘 본다.
    벤치 사이드카 포트는 팔 세트의 현재 state 에서 읽는다 - 팔 json 의 값은 gw-up 시점 스냅숏이다."""
    urls = [TEI_INFO]
    port = (replica or {}).get("sidecar_port")          # 기본 세트는 None - 운영 사이드카만 본다
    if port:
        urls.append(f"http://127.0.0.1:{port}/info")
    return urls


def combined_queue_depth(depths: List[Optional[int]]) -> Optional[int]:
    return None if any(d is None for d in depths) else sum(depths)   # type: ignore[arg-type]


def prod_audit_offset() -> Optional[Dict[str, Any]]:
    """운영 감사 로그 집계 시작 표지: 현재 크기 + 회전본(.1.gz) stat. 크기만으로는 회전 뒤 새 파일이 offset 을 넘겼을 때
    알아채지 못한다(copytruncate, issue #178). bench/guard.py sidecar_log_offset 과 같은 모양이고 JSON 직렬화된다."""
    size = _stat(PROD_AUDIT)
    if size is None:
        return None
    return {"offset": size[1], "rotated": _stat(f"{PROD_AUDIT}.1.gz")}


def rerank_kind(row: Dict[str, Any]) -> Optional[str]:
    """운영 감사 로그 한 줄이 리랭크 사이드카를 쓸 수 있는 요청이면 그 종류. **요청당 1줄 기준 줄만** 센다
    (issue #48). 한 요청이 감사 줄 여러 개를 남기기 때문이다:

    - `allow` : MCP 는 요소마다, REST 는 요청마다 1줄(gateway/app.py, rules 판정 이전에 기록). 기준 줄.
    - `context-trigger` : POST /context-triggers 는 allow 줄 없이 이 1줄뿐이라 이 종류만 이 decision 을 기준으로 센다.
      단 `reason == "miss"`(매칭 트리거 없음 -> upstream recall 없음)는 세지 않는다. match·match-empty·recall-failed 는 센다 (issue #62).
    - `mental-models` : reflect 를 돌리는 조작만 - REST POST 컬렉션·POST <id>/refresh, MCP create·refresh_mental_model.
      조회·clear·delete·update(PATCH/PUT, MCP update_mental_model: SQL UPDATE 뿐, 재생성 없음)는 세지 않는다 (issue #62).
    - 세지 않음 : 같은 요청의 부가 기록(enforce-autofill·enforce-normalize·retain-tags·contradiction-warn·
      recurrence·injection-correction·reflect-demand·rewrite-failure)과 사이드카에 닿지 않은 요청(deny·enforce-reject).
    - `decision` 키가 없는 행(옛 행·합성 행)은 tool 기준으로 센다.

    retain 도 넣는다: 게이트가 걸린 retain(gotcha·부정 표현)은 저장 전에 리랭크된 recall 을 부른다
    (gateway/retain_gates.py) - 감사 행만으로는 게이트 여부를 알 수 없으니 보수적으로 센다.
    알려진 소폭 과대계수: allow 줄은 rules 판정 이전에 남으므로 그 뒤 rules 에서 거부된 요청(retain, 그리고
    max_tokens 하한에 걸린 MCP recall)과, 통째로 거부된 MCP 배치의 allow 줄도 센다. 배치 거부는 두 가지다:
    deny 는 요소를 차례로 기록하다 첫 deny 에서 멈추므로 deny 요소보다 앞선 요소의 allow 만 남고, rules 위반은
    모든 요소의 allow 가 이미 기록된 뒤에 판정되므로 배치 전체의 allow 가 남는다(줄 사이 상관이 없어 가릴 수 없다)."""
    kind = _rerank_tool_kind(row)
    decision = row.get("decision")
    if decision is None or decision == "allow":
        return kind
    if kind == "context-triggers" and decision == CONTEXT_TRIGGER_DECISION:
        return None if row.get("reason") == CONTEXT_TRIGGER_MISS else kind      # miss 는 리랭크 없음 (issue #62)
    return None


def _resource_parts(tool: str) -> Optional[List[str]]:
    """REST 경로에서 banks/<bank>/ 뒤의 리소스 세그먼트들. banks 세그먼트가 없으면 None. 뱅크 이름 자리는 건너뛴다 -
    뱅크가 mental-models 여도 리소스로 읽히지 않게(issue #106)."""
    parts = tool.rstrip("/").split("/")
    if "banks" not in parts:
        return None
    return parts[parts.index("banks") + 2:]


def _rest_mental_model_rerank(tool: str, method: str) -> bool:
    """REST 멘탈 모델 요청 중 reflect 를 돌리는 것만: POST .../mental-models(생성), POST .../mental-models/<id>/refresh.
    부분 문자열이 아니라 경로 끝 세그먼트로 판정한다(GET 조회·clear·delete·update 는 reflect 가 없다).
    banks/<bank>/ 경로면 리소스 자리부터만 본다."""
    rest = _resource_parts(tool)
    parts = tool.rstrip("/").split("/") if rest is None else rest
    if rest is not None and rest[:1] != ["mental-models"]:
        return False
    if parts[-1:] == ["mental-models"]:
        return method.upper() == "POST"
    if len(parts) >= 3 and parts[-1] == "refresh" and parts[-3] == "mental-models":
        return method.upper() in ("", "POST")                # 메서드 없는 옛·합성 행은 세고, GET 등 다른 메서드는 세지 않는다
    return False


def _rerank_tool_kind(row: Dict[str, Any]) -> Optional[str]:
    tool = str(row.get("tool") or "")
    if tool in ("recall", "reflect", "context-triggers"):
        return tool
    if tool in ("retain", "sync_retain"):
        return "retain"
    if tool in MENTAL_MODEL_RERANK_TOOLS or _rest_mental_model_rerank(tool, str(row.get("method") or "")):
        return "mental-models"
    rest = _resource_parts(tool)
    if rest is not None:                                     # banks/<bank>/ 경로: 뱅크 이름이 아니라 리소스 자리로만 정한다(issue #106)
        if rest[:1] == ["mental-models"]:
            return None                                      # 리랭크 없는 멘탈 모델 REST 경로가 id·bank 이름 때문에 /recall·/reflect 로 새지 않게
        if rest[:2] == ["memories", "recall"]:
            return "recall"
        if rest[:1] == ["reflect"]:
            return "reflect"
        return "retain" if rest == ["memories"] and str(row.get("method") or "").upper() == "POST" else None
    if "mental-models" in tool.split("/"):
        return None                                          # banks 세그먼트가 없는 옛·합성 경로는 예전 규칙 그대로
    for part, kind in (("/recall", "recall"), ("/reflect", "reflect")):
        if part in tool:
            return kind
    if tool.endswith("/memories") and str(row.get("method") or "").upper() == "POST":
        return "retain"                                      # REST retain
    return None


def prod_events_since(mark: Optional[Dict[str, Any]]) -> Optional[List[Dict[str, str]]]:
    """표지 이후 운영 감사 로그에 찍힌 리랭크 사용 요청의 (ts, kind). 그 사이 회전했으면 옛 파일의 표지 이후 꼬리
    (.1.gz 의 offset 이후)와 새 파일 전체를 잇는다(bench/guard.py sidecar_text_since 와 같은 방식, issue #178).
    새 파일 앞 NUL 구멍(colima virtiofs truncate, #166)은 줄마다 걷어내고 파싱한다.
    감사 로그 ts 는 요청 도착 시각(초 단위)이다."""
    if mark is None:
        return None
    offset = int(mark["offset"])
    try:
        rot = _stat(f"{PROD_AUDIT}.1.gz")
        with open(PROD_AUDIT, "rb") as fh:
            if rot is not None and rot != (tuple(mark["rotated"]) if mark.get("rotated") else None):
                try:
                    with gzip.open(f"{PROD_AUDIT}.1.gz", "rb") as old:
                        old.seek(offset)
                        tail = old.read()
                except (OSError, EOFError, zlib.error):      # 쓰는 중(EOFError)·손상(zlib.error, BadGzipFile)이면 새 파일 전체만 쓴다
                    tail = b""
                if tail and not tail.endswith(b"\n"):
                    tail += b"\n"                             # 꼬리가 줄 중간에서 끝나도 새 파일 첫 줄과 융합되지 않게
                raw = tail + fh.read()
            else:
                fh.seek(offset if os.path.getsize(PROD_AUDIT) >= offset else 0)
                raw = fh.read()
    except (OSError, EOFError):
        return None
    out = []
    for line in raw.splitlines():
        try:
            row = json.loads(line.lstrip(b"\x00"))
        except ValueError:
            continue
        kind = rerank_kind(row) if isinstance(row, dict) else None
        if kind and row.get("ts"):
            out.append({"ts": row["ts"], "kind": kind})
    return out


def check_arm_ports(arms: Dict[str, int]) -> None:
    """팔 포트는 벤치 게이트웨이 컨테이너여야 한다. 운영 포트나 다른 프로세스면 거부."""
    for arm, port in arms.items():
        if port == PROD_PORT:
            raise SystemExit(f"arm {arm}: port {PROD_PORT} is the production gateway")
        out = subprocess.run(["docker", "ps", "--filter", f"publish={port}", "--filter", "label=hc-bench=1",
                              "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
        if len(out) != 1 or not out[0].startswith("hc-bench-gw-"):
            raise SystemExit(f"arm {arm}: port {port} is not a running bench gateway (found {out})")


def arm_container(port: int) -> Optional[str]:
    out = subprocess.run(["docker", "ps", "--filter", f"publish={port}", "--filter", "label=hc-bench=1",
                          "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
    return out[0] if len(out) == 1 else None


def rewrite_failures(port: int) -> Optional[int]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            return int(json.loads(r.read()).get("rewrite_failures"))
    except Exception:
        return None


def provenance(arms: Dict[str, int]) -> Dict[str, Any]:
    """복제본·팔 출처. replica.py 가 state 에 남긴 기록을 싣는다(없으면 None - 판정기가 경고)."""
    def load(path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None
    from bench.replica import instance_names         # 순수 함수만 쓴다(docker 호출 없음)
    out: Dict[str, Any] = {"replica": load(os.path.join(STATE_DIR, "replica.json")), "arms": {}, "replicas": {}}
    for arm, port in arms.items():
        name = arm_container(port)
        prov = load(os.path.join(STATE_DIR, "arms", name[len("hc-bench-gw-"):] + ".json")) if name else None
        out["arms"][arm] = {"container": name, **(prov or {})}
        s = (prov or {}).get("set", "")                  # 세트 없는 팔(구 gw-up) = 기본 세트
        out["replicas"][s] = load(os.path.join(STATE_DIR, instance_names(s)["state"]))
    return out


# ---------------------------------------------------------------- 실행

def load_corpus(split: str, limit: Optional[int], seed: int) -> List[Dict[str, Any]]:
    path = {"dev": DEV, "holdout": HOLDOUT}[split]
    rows = [json.loads(line) for line in open(path) if line.strip()]
    rows.sort(key=lambda r: r["key"])
    if limit and limit < len(rows):
        rows = random.Random(seed).sample(rows, limit)
        rows.sort(key=lambda r: r["key"])
    return rows


def warmup_pool(corpus: List[Dict[str, Any]], seed: int) -> List[Dict[str, Any]]:
    """측정 코퍼스와 텍스트가 겹치지 않는 워밍업 쿼리. dev 에서 고르고(holdout run 이면 dev 전체가 후보),
    남는 것이 없으면 합성 쿼리를 쓴다. 측정 쿼리로 워밍업하면 후보 캐시가 미리 채워져 속도가 부풀려진다."""
    measured = {r.get("split_key", r["key"]) for r in corpus} | {" ".join(str(r["args"].get("query", "")).split())
                                                                for r in corpus}
    pool = []
    if os.path.exists(DEV):
        for line in open(DEV):
            if not line.strip():
                continue
            r = json.loads(line)
            text = " ".join(str(r["args"].get("query", "")).split())
            if r.get("split_key", r["key"]) not in measured and text not in measured:
                pool.append({"key": "warmup-" + r["key"], "args": r["args"]})
    if not pool:
        pool = [{"key": f"warmup-synth-{i}", "args": {"query": q}} for i, q in enumerate(WARMUP_SYNTH)]
    random.Random(seed).shuffle(pool)
    return pool


def common_bench_clock(replicas: Dict[str, Optional[Dict[str, Any]]]) -> str:
    """팔들이 붙은 세트 복제본의 벤치 시계는 하나여야 한다 - 다르면 recency 점수가 달라져 결과 비교가 무의미하다."""
    clocks = {s: (m or {}).get("bench_clock") for s, m in replicas.items()}
    if not clocks or any(not c for c in clocks.values()) or len(set(clocks.values())) != 1:
        raise SystemExit(f"sets do not share one bench clock: {clocks}")
    return next(iter(clocks.values()))  # type: ignore[return-value]


def resolve_bench_clock(replicas: Dict[str, Optional[Dict[str, Any]]], clock_set: Optional[str] = None
                        ) -> Tuple[str, Optional[Dict[str, Any]]]:
    """run 의 벤치 시계. clock_set 이 없으면 common_bench_clock 그대로(시계가 다르면 거부, 기록 없음).
    clock_set 이 있으면 그 세트의 시계를 모든 팔에 쓰고, 세트별 시계를 meta.bench_clock_common 으로 남긴다(issue #297:
    재통합한 arm 세트는 last_memory_write_at 이 바뀌어 D10 세트와 시계가 다르다 - 명시해야만 기준 세트 시계로 맞춘다)."""
    if clock_set is None:
        return common_bench_clock(replicas), None
    clocks = {s: (m or {}).get("bench_clock") for s, m in replicas.items()}
    if clock_set not in clocks:
        raise SystemExit(f"--clock-from-set {clock_set!r} is not a set of this run's arms: {sorted(clocks)}")
    if any(not c for c in clocks.values()):
        raise SystemExit(f"every set needs a bench clock: {clocks}")
    return clocks[clock_set], {"from_set": clock_set, "set_clocks": dict(sorted(clocks.items())),  # type: ignore[return-value]
                               "clocks_differ": len(set(clocks.values())) != 1}


def bench_clock() -> str:
    """벤치 시계 = 복제본 데이터의 마지막 쓰기 시각(=백업 직후). 설정 파일에 기록된 값이 있으면 그것."""
    with open(os.path.join(STATE_DIR, "replica.json")) as fh:
        meta = json.load(fh)
    if meta.get("bench_clock"):
        return meta["bench_clock"]
    raise SystemExit("replica.json has no bench_clock - run `bench/replica.py clock` after replica up")


def arm_order(arms: List[str], rnd: int, idx: int) -> List[str]:
    """쿼리마다 팔 순서: 회전(라틴 방진) + 주기마다 역순. 연속 2n 개 쿼리마다 모든 팔이 모든 자리에 정확히
    같은 빈도로 선다(쿼리 수가 2n 의 배수가 아니면 자리당 ±1 호출 차이가 남는다). 이렇게 모든 팔이 자리를 고루 돌고,
    팔 2개면 ABBA 가 된다. 단순 정/역 교대는 팔 3개 이상에서 가운데 자리를 한 팔이 독점해
    위치 효과(같은 쿼리의 n번째 호출이 체계적으로 느리거나 빠름)가 팔 차이로 둔갑한다(2026-09-23 실측:
    팔 5개 교대 배치에서 동일 코드 A/A 가 자리별로 +4.6% / +0.9%).
    직전 팔(이월 효과) 균형은 팔 3개까지만 성립한다 - 4개 이상은 어떤 순서쌍이 한 번도 안 나온다(재리뷰 실측).
    그래서 팔이 3개를 넘는 run 은 meta.carryover_balanced=false 로 남고 판정기가 속도를 미결로 둔다
    (정확도·비용 fixture 처럼 속도를 안 보는 run 에만 쓴다)."""
    n = len(arms)
    k = (idx + rnd) % n
    seq = arms[k:] + arms[:k]
    return list(reversed(seq)) if ((idx + rnd) // n) % 2 else seq


def freeze_fp(sets: Optional[List[str]] = None) -> Dict[str, Any]:
    from bench import replica                       # docker 를 쓰는 모듈 - run 할 때만 부른다
    sets = sorted(sets or [""])
    if sets == [""]:
        return replica.freeze_fingerprint()         # 기본 세트만 - 기존 run 과 같은 모양
    return {s: replica.freeze_fingerprint(s) for s in sets}


MAX_ARMS = 3


def run(arms: Dict[str, int], split: str, rounds: int, warmup: int, limit: Optional[int], seed: int,
        run_id: str, token: str, note: str = "", fingerprint=freeze_fp, check_ports=check_arm_ports,
        gap_ms: int = 500, selfcheck=None, guard=None, keepwarm: Optional[KeepWarm] = None,
        clock_set: Optional[str] = None) -> str:
    if len(arms) < 2:
        raise SystemExit("need at least 2 arms")
    check_ports(arms)
    if selfcheck is None:
        from bench.selfcheck import require_fixtures as selfcheck   # 자기검증 fixture 가 깨졌으면 판정 run 을 시작하지 않는다
    selfcheck()
    if guard is None:
        guard = Guard()
    try:
        guard.check()                                        # 시작 전 - 호스트가 이미 압박 중이면 기다리거나 거부한다
    except GuardAbort as e:
        raise SystemExit(f"bench guard refused to start: {e}")
    tok = Tokenizer()
    corpus = load_corpus(split, limit, seed)
    prov = provenance(arms)
    sets = sorted(prov["replicas"])
    clock, clock_common = resolve_bench_clock(prov["replicas"], clock_set)
    arm_sets = {a: (prov["arms"].get(a) or {}).get("set", "") for a in arms}
    queue_urls = {a: arm_queue_urls(prov["replicas"].get(arm_sets[a])) for a in arms}
    root = HOLDOUT_RUNS_DIR if split == "holdout" else RUNS_DIR
    os.makedirs(root, mode=0o700 if split == "holdout" else 0o755, exist_ok=True)
    out_dir = os.path.join(root, run_id)
    os.makedirs(out_dir, exist_ok=False)
    clients = {a: McpClient(p, token) for a, p in arms.items()}
    for c in clients.values():
        c.open()
    arm_names = sorted(arms)

    def call(arm: str, row: Dict[str, Any]) -> Dict[str, Any]:
        args = dict(row["args"])
        args.setdefault("query_timestamp", clock)
        if gap_ms > 0:
            time.sleep(gap_ms / 1000)
        guard.check()                                        # 정지 조건이면 재개까지 막고, 중단이면 GuardAbort
        if keepwarm is not None:
            keepwarm.tick()                                  # 호출 직전 - 가드 정지 뒤에도 운영 사이드카를 데우고 시작한다 (issue #79)
        qd = combined_queue_depth([tei_queue_depth(u) for u in queue_urls[arm]])
        wall = dt.datetime.now(dt.timezone.utc)
        raw = clients[arm].recall(args)
        retried = False
        if raw.get("http") == 404 or (isinstance(raw.get("msg"), dict) and "error" in raw["msg"]
                                       and "session" in json.dumps(raw["msg"]).lower()):
            clients[arm].open()                              # 세션 만료 - 1회 재개 후 재시도
            raw = clients[arm].recall(args)
            retried = True                                   # 판정기가 속도 표본에서 뺀다
        wall_end = dt.datetime.now(dt.timezone.utc)
        qa = combined_queue_depth([tei_queue_depth(u) for u in queue_urls[arm]])
        cls = classify(raw)
        rec = {"arm": arm, "key": row["key"], "latency_ms": round(raw["latency_ms"], 2),
               "wall": wall.isoformat(), "wall_end": wall_end.isoformat(),
               "tei_queue_before": qd, "tei_queue_after": qa, "outcome": cls["outcome"]}
        if retried:
            rec["retried"] = True
        for k in ("rule", "code", "detail", "http", "extra_blocks", "top_keys"):
            if k in cls:
                rec[k] = cls[k]
        if "content_text" in cls:
            rec["tokens"] = tok.count(cls["content_text"])
            rec["chars"] = len(cls["content_text"])
        if "results" in cls:
            rec["n_results"] = len(cls["results"])
            rec["ids"] = [r.get("id") for r in cls["results"]]
            rec["structured"] = cls["stored"]["structured"]
        if "stored" in cls:
            rec["results_digest"] = result_digest(cls["stored"])
            rec["_stored"] = cls["stored"]
        return rec

    meta = {"run_id": run_id, "arms": arms, "split": split, "bank": BANK, "rounds": rounds, "warmup_max": warmup,
            "limit": limit, "seed": seed, "n_queries": len(corpus), "bench_clock": clock, "note": note,
            "carryover_balanced": len(arms) <= MAX_ARMS, "gap_ms": gap_ms,
            "corpus_keys_sha256": hashlib.sha256("\n".join(r["key"] for r in corpus).encode()).hexdigest()[:16],
            "provenance": prov,
            "freeze_before": fingerprint(sets),
            "rewrite_failures_before": {a: rewrite_failures(p) for a, p in arms.items()},
            "started": dt.datetime.now(dt.timezone.utc).isoformat()}
    if clock_common is not None:                             # 옵션을 쓴 run 에만 - 옛 run meta 모양은 그대로
        meta["bench_clock_common"] = clock_common
    audit_offset = prod_audit_offset()
    sidecar_offset = sidecar_log_offset()
    calls_path = os.path.join(out_dir, "calls.jsonl")
    results_dir = os.path.join(out_dir, "results")
    os.makedirs(results_dir)
    stored = set()
    rng = random.Random(seed)
    aborted: Optional[str] = None
    if keepwarm is not None:
        keepwarm.start()
    try:
        with open(calls_path, "w") as fh:
            # 워밍업: 측정과 겹치지 않는 쿼리로, 기준 팔 지연이 안정될 때까지(상한 warmup). 기록하되 phase=warmup.
            base_lat: List[float] = []
            used = 0
            for row in warmup_pool(corpus, seed)[:max(0, warmup)]:
                for arm in arm_names:
                    rec = call(arm, row)
                    rec.pop("_stored", None)
                    if arm == arm_names[0] and rec["outcome"] == "ok":
                        base_lat.append(rec["latency_ms"])
                    fh.write(json.dumps({**rec, "phase": "warmup", "round": -1}, ensure_ascii=False) + "\n")
                used += 1
                w = WARMUP_WINDOW
                if used >= WARMUP_MIN and len(base_lat) >= 2 * w:
                    prev, last = statistics.median(base_lat[-2 * w:-w]), statistics.median(base_lat[-w:])
                    if abs(last - prev) / prev < WARMUP_STABLE:
                        break
            meta["warmup_used"] = used
            for r in range(rounds):
                order = corpus[:]
                rng.shuffle(order)
                for i, row in enumerate(order):
                    seq = arm_order(arm_names, r, i)
                    for pos, arm in enumerate(seq):
                        rec = call(arm, row)
                        body = rec.pop("_stored", None)
                        if body is not None and (arm, row["key"], rec["results_digest"]) not in stored:
                            stored.add((arm, row["key"], rec["results_digest"]))
                            with open(os.path.join(results_dir, f"{arm}.{row['key']}.{rec['results_digest']}.json"),
                                      "w") as rf:
                                json.dump(body, rf, ensure_ascii=False, sort_keys=True)
                        fh.write(json.dumps({**rec, "phase": "measure", "round": r, "pos": pos, "idx": i},
                                            ensure_ascii=False) + "\n")
                        fh.flush()
    except GuardAbort as e:
        aborted = str(e)                                     # 측정을 이어가지 않는다 - 여기까지의 기록과 meta 만 남긴다
    except BaseException:
        if keepwarm is not None:                             # 예외·Ctrl-C 로 죽어도 이미 운영 사이드카에 나간 구간의 제외 줄은 남긴다.
            keepwarm.stop()                                  # meta.json 은 쓰지 않는다(없으면 판정기가 이 부분 run 을 판정하지 않는다)
            with open(os.path.join(out_dir, "keepwarm.json"), "w") as kf:
                json.dump(keepwarm.meta(run_id), kf, ensure_ascii=False, indent=2)
        raise
    finally:
        if keepwarm is not None:
            keepwarm.stop()                                  # 끝·중단·예외 어느 쪽이든 run 이 끝나면 더는 보내지 않는다
    events = prod_events_since(audit_offset)
    meta["prod_events"] = None if events is None else len(events)
    if events is not None:
        with open(os.path.join(out_dir, "prod_events.jsonl"), "w") as fh:
            for ev in events:
                fh.write(json.dumps(ev) + "\n")
    meta["prod_sidecar"] = sidecar_errors_since(sidecar_offset)
    meta["guard"] = guard.meta()
    meta["keepwarm"] = None if keepwarm is None else keepwarm.meta(run_id)     # None = 꺼짐(테스트·옛 run)
    # prod_sidecar 의 503·데드라인·큐 포화에는 keep-warm 자신의 실패도 들어 있다 - 운영 영향을 볼 때(#21 6건과 비교) 이 수를 뺀다
    meta["prod_sidecar_keepwarm"] = None if keepwarm is None else keepwarm.own_sidecar_errors()
    meta["rewrite_failures_after"] = {a: rewrite_failures(p) for a, p in arms.items()}
    meta["freeze_after"] = fingerprint(sets)
    meta["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    if aborted:
        raise SystemExit(f"bench guard aborted the run ({aborted}); partial run at {out_dir}")
    return out_dir


def read_token() -> str:
    """팔에 보내는 벤치 전용 토큰(replica.py 가 만든 벤치 ACL 에만 있다). 운영 토큰은 쓰지 않는다 - 팔이 운영
    토큰을 받으면 후보 코드가 그것을 볼 수 있다."""
    try:
        with open(os.path.join(STATE_DIR, "bench-client.key")) as fh:
            tok = fh.read().strip()
    except OSError:
        tok = ""
    if not tok:
        raise SystemExit("bench client token missing - bring arms up with `bench/replica.py gw-up` first")
    return tok


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arm", action="append", required=True, help="name=port (2개 이상)")
    ap.add_argument("--split", choices=("dev", "holdout"), default="dev")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=30, help="워밍업 쿼리 상한(안정되면 일찍 끝난다)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--run-id", default=dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--note", default="")
    ap.add_argument("--gap-ms", type=int, default=500, help="호출 사이 고정 휴지(이월 효과 억제)")
    ap.add_argument("--clock-from-set", help="세트 시계가 다를 때 이 세트의 시계를 모든 팔에 쓴다(meta.bench_clock_common 에 기록)")
    a = ap.parse_args(argv)
    arms = {k: int(v) for k, v in (s.split("=", 1) for s in a.arm)}
    out = run(arms, a.split, a.rounds, a.warmup, a.limit, a.seed, a.run_id, read_token(), a.note, gap_ms=a.gap_ms,
              keepwarm=KeepWarm.production(), clock_set=a.clock_from_set)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
