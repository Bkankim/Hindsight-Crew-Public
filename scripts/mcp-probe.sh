#!/usr/bin/env bash
# mcp-probe.sh — N0 계약 fixture 채집기. **upstream 직결 전용**.
#
# 게이트웨이 포트(호스트 127.0.0.1:8888) 사용 금지 — 재채집은 항상 upstream 직결
# (`docker compose exec -T hindsight python -c '<고정 소스>' http://localhost:8888/...`)로만 한다.
# 컨테이너 안 전송은 이미지의 python(urllib)이다 - upstream 0.10.0부터 런타임 이미지에 curl이 없다.
# 게이트웨이 경유로 채집하면 게이트웨이가 이미 패치·리프레이밍한 출력을 다시 baseline으로
# 핀하게 되어(= 패치된 출력 재핀 오염) 드리프트 탐지가 자기 자신을 기준 삼는 사고가 난다.
# contract-probe.sh 의 `mcp()`는 게이트웨이 형식(`/mcp/<bank>/`)이라 여기서 재사용하지 않는다.
# upstream 형식은 `POST /mcp` + `X-Bank-Id: <bank>` 헤더.
#
# sourceable: `. scripts/mcp-probe.sh` 하면 main을 실행하지 않고 핸드셰이크 헬퍼만 정의한다
# (N6에서 verify/lib.sh 의 mcp_call()이 mcp_handshake/mcp_post 를 재사용한다).
set -u

HC_MCP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- 대상 선택 계약 (#242). 우선순위: 호출 시점 env > HC_ENV_FILE 안의 값 > 기본값 ---
# env 파일은 `set -a` 로 source 하므로 호출자가 준 값을 덮어쓴다. source 전에 이 변수들의 호출 값을
# 저장하고 source 뒤 되돌린다("있다" = 설정됨. 빈 값으로 설정해도 파일 값을 이긴다).
_hc_mcp_keep=""
# 호출자가 HC_ENV_FILE 을 줬는지(빈 값 제외). 기본 .env.local 만 없어도 되고, 명시한 파일이 없으면 아래에서 실패한다.
_hc_mcp_explicit=0; if [ -n "${HC_ENV_FILE:-}" ]; then _hc_mcp_explicit=1; fi
for _hc_mcp_v in HC_GATEWAY_BIND HC_PG0_VOLUME HC_HINDSIGHT_CONTAINER HC_COMPOSE_PROJECT HC_COMPOSE_FILES HC_SECRETS_DIR; do
  if [ -n "${!_hc_mcp_v+x}" ]; then
    printf -v "_hc_mcp_keep_$_hc_mcp_v" '%s' "${!_hc_mcp_v}"
    _hc_mcp_keep="$_hc_mcp_keep $_hc_mcp_v"
  fi
done
HC_ENV_FILE="${HC_ENV_FILE:-.env.local}"
case "$HC_ENV_FILE" in /*) : ;; *) HC_ENV_FILE="$HC_MCP_ROOT/$HC_ENV_FILE" ;; esac
# compose 보간 필수 — 미수행 시 docker compose가 HC_HINDSIGHT_API_KEY missing 으로 즉사한다.
[ -f "$HC_ENV_FILE" ] && { set -a; . "$HC_ENV_FILE"; set +a; } || true
for _hc_mcp_v in $_hc_mcp_keep; do
  _hc_mcp_s="_hc_mcp_keep_$_hc_mcp_v"
  printf -v "$_hc_mcp_v" '%s' "${!_hc_mcp_s}"
done
# 명시한 파일이 없으면 조용히 기본 대상으로 떨어지므로 실패한다(scripts/backup.sh 와 같은 규칙). source 중이면
# 호출자 셸을 끝내지 않고 source 만 실패시킨다.
if [ "$_hc_mcp_explicit" = 1 ] && [ ! -f "$HC_ENV_FILE" ]; then
  echo "mcp-probe: HC_ENV_FILE $HC_ENV_FILE does not exist" >&2
  case "${BASH_SOURCE[0]}" in "$0") exit 1 ;; *) return 1 ;; esac
fi
unset _hc_mcp_explicit
unset _hc_mcp_v _hc_mcp_s _hc_mcp_keep ${!_hc_mcp_keep_*}

HC_MCP_UPSTREAM="${HC_MCP_UPSTREAM:-http://localhost:8888}"  # 컨테이너 내부 주소 (게이트웨이 아님)
HC_MCP_BANK="${HC_MCP_BANK:-probe-n0}"                       # 프로브 전용 뱅크 — 개인 뱅크 오염 금지
HC_MCP_TENANT="${HC_MCP_TENANT:-default}"

# --- 전송 파라미터 (기본값 = upstream 직결. 채집 경로는 이 기본값에서 절대 벗어나지 않는다) ---
# N6 재사용 훅: verify/lib.sh 의 mcp_call() 이 이 파일을 source 한 뒤 아래 4개만 게이트웨이
# 값으로 덮어써서 mcp_handshake/mcp_post 를 그대로 돌린다(핸드셰이크 중복 구현 금지).
# 게이트웨이는 뱅크를 URL 경로(/mcp/<bank>/)에서 해석하므로 X-Bank-Id 헤더를 쓰지 않는다.
HC_MCP_BASE="${HC_MCP_BASE:-$HC_MCP_UPSTREAM}"
HC_MCP_PATH="${HC_MCP_PATH:-/mcp}"                     # upstream MCP 마운트 지점
HC_MCP_BANK_HEADER="${HC_MCP_BANK_HEADER:-X-Bank-Id}"  # 빈 값 = 헤더 생략(경로 기반 라우팅)
HC_MCP_CURL="${HC_MCP_CURL:-_upstream_curl}"           # 전송 함수 이름
HC_MCP_KEY="${HC_HINDSIGHT_API_KEY:-}"
HC_MCP_SESSION="${HC_MCP_SESSION:-}"
HC_REST_STATUS=""

die() { echo "mcp-probe: $*" >&2; exit 1; }
log() { echo "mcp-probe: $*" >&2; }

# compose 호출은 항상 리포 루트 기준 (sourced 사용 시 caller cwd에 의존하지 않도록).
# 형태: docker compose [--env-file F] --project-directory <루트> [-p P] -f <파일>... (#242 대상 선택 계약)
_compose() {
  local a=(docker compose) f files
  [ -f "$HC_ENV_FILE" ] && a+=(--env-file "$HC_ENV_FILE")
  a+=(--project-directory "$HC_MCP_ROOT")
  [ -n "${HC_COMPOSE_PROJECT:-}" ] && a+=(-p "$HC_COMPOSE_PROJECT")
  IFS=: read -r -a files <<< "${HC_COMPOSE_FILES:-docker-compose.yml}"   # read 는 글로빙하지 않는다
  for f in ${files[@]+"${files[@]}"}; do
    [ -n "$f" ] || continue
    case "$f" in /*) : ;; *) f="$HC_MCP_ROOT/$f" ;; esac
    a+=(-f "$f")
  done
  "${a[@]}" "$@"
}

# 컨테이너 안에서 도는 고정 python 소스(curl 대체). 작은따옴표를 쓰지 않는다(이 변수가 작은따옴표 문자열).
# curl 인자 중 호출부가 쓰는 것만 해석한다: -X -H(@- = stdin 한 줄당 헤더) -d -i -o -w(%{http_code}, \n) -s -S -m.
# 4xx/5xx(HTTPError)와 3xx(따라가지 않음)도 응답으로 읽어 상태·헤더·본문을 그대로 낸다(curl 이 -f/-L 없이 그러듯). env 프록시는 무시한다. 연결 실패만 exit 1.
HC_MCP_PYSRC='import http.client
import os
import sys
import urllib.error
import urllib.request

def fail(code, msg):
    sys.stderr.write("mcp-probe transport: " + msg + "\n")
    sys.exit(code)

args = sys.argv[1:]
method = url = data = out = fmt = timeout = None
show = False
hdrs = []
i = 0
while i < len(args):
    a = args[i]
    i += 1
    if a in ("-s", "-S", "-sS", "-Ss"):
        continue
    if a == "-i":
        show = True
        continue
    if a in ("-X", "-H", "-d", "-o", "-w", "-m", "--max-time"):
        if i >= len(args):
            fail(2, "option needs an argument: " + a)
        v = args[i]
        i += 1
        if a == "-X":
            method = v
        elif a == "-H":
            if v == "@-":
                hdrs += [ln for ln in sys.stdin.read().splitlines() if ln.strip()]
            else:
                hdrs.append(v)
        elif a == "-d":
            v = os.fsencode(v)
            data = v if data is None else data + b"&" + v
        elif a == "-o":
            out = v
        elif a == "-w":
            fmt = v
        else:
            timeout = float(v)
    elif a.startswith("-"):
        fail(2, "unsupported option: " + a)
    elif url is None:
        url = a
    else:
        fail(2, "unexpected argument: " + a)
if url is None:
    fail(2, "no URL")

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

# curl without -L/-x: a 3xx is the response (never followed, so the key stays on this host), env proxies are ignored
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect))
req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
req.add_header("Accept", "*/*")
for h in hdrs:
    name, _, value = h.partition(":")
    req.add_header(name.strip(), value.strip())

try:
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        resp = e
    code = resp.getcode()
    body = resp.read()
except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
    why = getattr(e, "reason", None)
    if not isinstance(why, BaseException):
        why = e
    fail(1, "request failed: " + type(why).__name__ + ": " + str(why))

payload = b""
if show:
    ver = "1.0" if getattr(resp, "version", 11) == 10 else "1.1"
    head = "HTTP/%s %d %s\r\n" % (ver, code, resp.reason or "")
    head += "".join("%s: %s\r\n" % kv for kv in resp.headers.items()) + "\r\n"
    payload = head.encode("latin-1", "replace")
payload += body
if out is None:
    sys.stdout.buffer.write(payload)
else:
    with open(out, "wb") as fh:
        fh.write(payload)
if fmt is not None:
    fmt = fmt.replace("%{http_code}", str(code)).replace("%%", "%")
    for k, v in (("\\n", "\n"), ("\\r", "\r"), ("\\t", "\t")):
        fmt = fmt.replace(k, v)
    sys.stdout.buffer.write(fmt.encode("utf-8"))
sys.stdout.buffer.flush()
'

# _upstream_curl <curl-args...> - hindsight 컨테이너 안에서 python 으로 요청 + Bearer 주입(함수 이름은 호출부 호환용).
# 키는 `compose exec` 의 stdin 으로만 넘기고 컨테이너 안 python 이 `-H @-` 로 stdin 에서 읽는다. 호스트 docker argv 에도
# 컨테이너 python argv 에도 키가 남지 않는다(`-e HC_MCP_KEY=...` 는 호스트 ps 에 보인다, issue #214). stdin 은 키 전용 채널이라
# 소스는 heredoc 이 아니라 `python -c` 인자로 준다. exec 은 stdin 을 항상 연결하고 -T 는 TTY 만 끈다.
_upstream_curl() {
  printf 'Authorization: Bearer %s\n' "$HC_MCP_KEY" |
    _compose exec -T hindsight python -c "$HC_MCP_PYSRC" -H @- "$@"
}

# mcp_post <json-body> [extra curl args...] — 응답 원문 그대로(SSE 프레이밍 보존) 출력.
mcp_post() {
  local body="$1"; shift
  local h=(-H 'Content-Type: application/json'
           -H 'Accept: application/json, text/event-stream')
  [ -n "$HC_MCP_BANK_HEADER" ] && h+=(-H "$HC_MCP_BANK_HEADER: $HC_MCP_BANK")
  [ -n "$HC_MCP_SESSION" ] && h+=(-H "Mcp-Session-Id: $HC_MCP_SESSION")
  "$HC_MCP_CURL" -X POST "$HC_MCP_BASE$HC_MCP_PATH" "${h[@]}" -d "$body" "$@"
}

# mcp_handshake — initialize -> Mcp-Session-Id 추출 -> notifications/initialized.
# 성공 시 HC_MCP_SESSION(세션 id) / HC_MCP_INIT_RAW(-i 원문: 헤더+본문) 설정.
mcp_handshake() {
  local raw
  HC_MCP_SESSION=""
  raw="$(mcp_post '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"hc-mcp-probe","version":"0"}}}' -i)" \
    || return 1
  HC_MCP_INIT_RAW="$raw"
  HC_MCP_SESSION="$(printf '%s' "$raw" | tr -d '\r' | awk 'tolower($1)=="mcp-session-id:"{print $2; exit}')"
  [ -n "$HC_MCP_SESSION" ] || return 1
  mcp_post '{"jsonrpc":"2.0","method":"notifications/initialized"}' -o /dev/null >/dev/null
}

# rest_probe <method> <path> [json-body] — "<응답 본문>\n<HTTP 상태>" 출력.
# 상태를 변수로 돌려주지 않는 이유: 호출부가 $(...) 로 받으면 서브셸이라 전역 대입이 증발한다.
# 분해는 rest_body / rest_status 헬퍼로.
rest_probe() {
  local m="$1" p="$2" b="${3:-}"
  local a=(-X "$m" "$HC_MCP_UPSTREAM$p" -H 'Content-Type: application/json' -w '\n%{http_code}')
  [ -n "$b" ] && a+=(-d "$b")
  _upstream_curl "${a[@]}"
}
rest_status() { printf '%s' "${1##*$'\n'}"; }
rest_body()   { printf '%s' "${1%$'\n'*}"; }

# ---------------------------------------------------------------- 대상 선택 (#242)

# 해석된 compose 프로젝트: HC_COMPOSE_PROJECT(-p) > COMPOSE_PROJECT_NAME > 리포 루트 basename(compose 규칙으로 정규화)
_mcp_project() {
  local p="${HC_COMPOSE_PROJECT:-${COMPOSE_PROJECT_NAME:-}}"
  [ -n "$p" ] || p="$(basename "$HC_MCP_ROOT" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-' | sed 's/^[-_]*//')"
  printf '%s' "$p"
}

# 실행 전 대상 한 줄(stderr)과 운영/사본 가드. main 과 --handshake-only 에서만 부른다(source 시에는 안 부른다).
# mcp-probe 는 compose `exec hindsight` 로만 컨테이너에 닿으므로 project 칸만 해석하고 나머지는 `-`.
_mcp_target_guard() {
  local proj; proj="$(_mcp_project)"
  echo "target: gw=- project=$proj container=- pg0=-" >&2
  if [ "${HC_TARGET:-}" = next ]; then
    [ "$proj" = hc-next ] || die "HC_TARGET=next 인데 해석된 프로젝트가 '$proj' (hc-next 여야 한다)"
  else
    case "$proj" in
      hc-next*) die "HC_TARGET 이 비어 있는데 해석된 프로젝트가 '$proj' (사본이면 HC_TARGET=next)";;
    esac
  fi
}

# 공통 사전 점검: 대상 가드, 키, probe-* 뱅크 전용.
_mcp_preflight() {
  _mcp_target_guard
  [ -n "$HC_MCP_KEY" ] || die "HC_HINDSIGHT_API_KEY missing (HC_ENV_FILE 로드 실패 - compose 보간 관례 확인)"
  case "$HC_MCP_BANK" in
    probe-*) : ;;
    *) die "refusing to probe bank '$HC_MCP_BANK' — probe-* 전용 (개인/팀 뱅크 오염 금지)";;
  esac
}

# _mcp_save_handshake <dir> - initialize 헤더/본문 분리 저장(HC_MCP_INIT_RAW). main 과 --handshake-only 가 같은 파일 이름을 쓴다.
_mcp_save_handshake() {
  local d="$1"
  printf '%s' "$HC_MCP_INIT_RAW" | awk 'BEGIN{h=1} {sub(/\r$/,"")} h && /^$/ {h=0; next} h {print}' > "$d/initialize.headers"
  printf '%s' "$HC_MCP_INIT_RAW" | awk 'BEGIN{h=1} {sub(/\r$/,"")} h && /^$/ {h=0; next} !h {print}' > "$d/initialize.raw"
}

# --handshake-only [--out <디렉터리>] - 쓰기 없는 비교용. initialize, notifications/initialized, tools/list 만 보낸다.
# retain·REST 쓰기가 없고 리포 fixtures/ 도 건드리지 않는다(기본 --out 은 새 임시 디렉터리).
handshake_only() {
  local out=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --out) [ $# -ge 2 ] || die "--out needs a directory"; out="$2"; shift 2;;
      *) die "unknown argument: $1";;
    esac
  done
  _mcp_preflight
  case "$out" in ""|/*) : ;; *) out="${HC_MCP_CALLER_PWD:-$PWD}/$out" ;; esac
  if [ -z "$out" ]; then out="$(mktemp -d "${TMPDIR:-/tmp}/mcp-probe.XXXXXX")" || die "mktemp failed"; else mkdir -p "$out" || die "cannot create $out"; fi
  log "handshake-only (no writes): bank=$HC_MCP_BANK out=$out"
  mcp_handshake || die "MCP handshake failed (hindsight 컨테이너가 Up 인지, 키가 맞는지 확인)"
  _mcp_save_handshake "$out"
  mcp_post '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' > "$out/tools_list.raw" || die "tools/list failed"
  log "session=$HC_MCP_SESSION"
  log "wrote: $out"
}

# ---------------------------------------------------------------- 응답 가공 회귀용 추가 채집 (#247)

# 시드와 쿼리는 고정 문자열이다(재채집해도 같은 입력). 채집 때 fixtures/mcp/seed_cases.json 으로도 남겨 테스트가 같은 값을 읽는다.
HC_SEED_BODY='[발견] 프로브 시드 - 헤드리스 브라우저를 재시작하면 로그인 쿠키가 사라져 사용자 데이터 디렉터리를 고정해야 세션이 유지된다'
HC_SEED_RECALL_QUERY='브라우저를 다시 띄워도 로그인 세션을 유지하려면 어떻게 해야 하나'
# retain_gates 시험의 retain 입력. 시드와 같은 본문이면 recurrence.similar_gotchas 가 "같은 기억"으로 건너뛰므로(본문 동일) 시드를 다시 쓴 꼴로 한다.
HC_SEED_RETAIN_BODY='[발견] 프로브 시드 - 헤드리스 브라우저를 다시 띄우면 로그인 쿠키가 사라진다. 사용자 데이터 디렉터리를 고정해야 세션이 유지된다'
HC_SEED_REFLECT_QUERY='프로브 시드 함정은 무엇이고 어떻게 피하나'
HC_SEED_BAD_TIMESTAMP='not-a-timestamp'

# _mcp_split_response <-i 로 받은 원문 파일> <출력 접두사> - 헤더와 본문을 나눈다.
# <접두사>.headers = 상태줄 + 헤더(CR 제거, initialize.headers 와 같은 모양), <접두사>.raw = 본문 바이트 그대로(후행 개행 포함).
_mcp_split_response() {
  local tmp="$1" out="$2" top pre hlen
  top="$(LC_ALL=C head -c 16384 "$tmp")"
  pre="${top%%$'\r\n\r\n'*}"
  [ "$pre" != "$top" ] || return 1
  hlen=$(( $(printf '%s' "$pre" | LC_ALL=C wc -c) + 4 ))
  printf '%s\n' "$pre" | tr -d '\r' > "$out.headers"
  LC_ALL=C tail -c +$((hlen + 1)) "$tmp" > "$out.raw"
}

# _mcp_capture <이름> <json-rpc 요청> [기대 HTTP 상태 glob, 기본 2??] - upstream 직결로 한 건 보내 fixtures/mcp/<이름>.raw(.headers) 로 저장한다.
# 상태가 기대와 다르면 fixture 를 쓰지 않고 멈춘다(오류 본문이 기준 원본으로 핀되는 것을 막는다). 임시 파일에 먼저 받고 성공했을 때만 옮겨 기존 fixture 를 지킨다.
# 의도적으로 실패를 채집하는 케이스는 호출부가 기대 상태를 적는다(MCP 의 검증 오류·성공형 실패 프레임은 HTTP 200 위의 본문 수준 실패라 2??).
_mcp_capture() {
  local name="$1" body="$2" expect="${3:-2??}" tmp pfx status
  tmp="$(mktemp "${TMPDIR:-/tmp}/mcp-probe-cap.XXXXXX")" || die "mktemp failed"
  pfx="$tmp.part"
  mcp_post "$body" -i > "$tmp" || { rm -f "$tmp"; die "MCP call failed: $name"; }
  _mcp_split_response "$tmp" "$pfx" || { rm -f "$tmp" "$pfx".*; die "cannot split the response: $name"; }
  status="$(awk 'NR==1{print $2}' "$pfx.headers")"
  case "$status" in
    $expect) : ;;
    *) rm -f "$tmp" "$pfx".*; die "MCP call $name returned HTTP ${status:-?} (기대 $expect) - fixture 를 쓰지 않는다";;
  esac
  mv "$pfx.raw" "$HC_MCP_FIXTURE_DIR/$name.raw" && mv "$pfx.headers" "$HC_MCP_FIXTURE_DIR/$name.headers" \
    || { rm -f "$tmp" "$pfx".*; die "cannot write fixture: $name"; }
  rm -f "$tmp" "$pfx".*
}

# _frame_is_success_framed_error <.raw> - SSE 본문의 JSON-RPC 응답이 isError 가 false 거나 없는데(= 성공형 프레임) structuredContent.status 가 "error" 인가.
_frame_is_success_framed_error() {
  tr -d '\r' < "$1" | sed -n 's/^data: //p' \
    | jq -e '.result | ((.isError // false) == false) and ((((.structuredContent // {}) | .status) // "") == "error")' >/dev/null 2>&1
}

# _reflect_has_based_on <.raw> - reflect 결과 본문(content[0].text JSON)에 값이 있는 based_on 이 있는가(null·빈 값이면 거짓).
_reflect_has_based_on() {
  tr -d '\r' < "$1" | sed -n 's/^data: //p' \
    | jq -e '.result.content[0].text | fromjson | (.based_on // {}) | [.[]? | select(length > 0)] | length > 0' >/dev/null 2>&1
}

# _rest_capture_full <out> <method> <path> <request-json> - _rest_fixture 형식에 재생용 contentType 과 원본 본문 바이트(bodyBase64)를 더해 저장한다.
_rest_capture_full() {
  local out="$1" method="$2" path="$3" req="$4" tmp pfx status ct resp b64
  tmp="$(mktemp "${TMPDIR:-/tmp}/mcp-probe-rest.XXXXXX")" || die "mktemp failed"
  pfx="$tmp.part"
  _upstream_curl -X "$method" "$HC_MCP_UPSTREAM$path" -H 'Content-Type: application/json' -H 'Accept: application/json' -d "$req" -i > "$tmp" \
    || { rm -f "$tmp"; die "REST $method $path failed"; }
  _mcp_split_response "$tmp" "$pfx" || { rm -f "$tmp" "$pfx".*; die "cannot split the REST response: $path"; }
  status="$(awk 'NR==1{print $2}' "$pfx.headers")"
  case "$status" in 2??) : ;; *) rm -f "$tmp" "$pfx".*; die "REST $method $path returned HTTP ${status:-?} (2xx 아님) - fixture 를 쓰지 않는다";; esac
  ct="$(awk -F': ' 'tolower($1)=="content-type"{print $2; exit}' "$pfx.headers")"
  resp="$(jq -c . "$pfx.raw")" || { rm -f "$tmp" "$pfx".*; die "REST $method $path: JSON 이 아닌 본문 - fixture 를 쓰지 않는다"; }
  b64="$(jq -Rrs '@base64' < "$pfx.raw")" || { rm -f "$tmp" "$pfx".*; die "base64 failed: $path"; }
  # 직렬화에 성공한 뒤에만 기존 fixture 를 바꾼다(실패가 빈 파일로 남지 않게 임시 파일에 쓴다)
  jq -n --arg method "$method" --arg path "$path" --arg ts "$(date -u +%FT%TZ)" \
        --argjson request "$req" --argjson status "$status" --arg ct "$ct" \
        --arg b64 "$b64" --argjson response "$resp" \
        '{probedAt:$ts, method:$method, path:$path, request:$request, status:$status, contentType:$ct, bodyBase64:$b64, response:$response}' > "$out.tmp" \
    && mv "$out.tmp" "$out" || { rm -f "$tmp" "$pfx".* "$out.tmp"; die "cannot write $out"; }
  rm -f "$tmp" "$pfx".*
}

# _capture_regression_cases - 응답 가공(recall_prune/unescape/honesty/reflect_based_on/retain_gates) 회귀용 원본 채집.
# 시드 retain 1건을 완료까지 기다린 뒤 그 본문과 비슷한 쿼리로 recall 한다. 전부 upstream 직결, 쓰기는 probe-* 뱅크에만 간다.
_capture_regression_cases() {
  local bank_path="$1" req seed_id
  # ⑩ 시드: 한국어 본문 + kind:gotcha. sync_retain 은 완료(검색 가능)까지 기다린다.
  _mcp_capture retain_seed_ok "$(jq -nc --arg c "$HC_SEED_BODY" \
    '{jsonrpc:"2.0",id:10,method:"tools/call",params:{name:"sync_retain",arguments:{content:$c,tags:["project:hindsight-crew","kind:gotcha"],timestamp:"2026-07-26T00:00:00Z",context:"probe-seed"}}}')"
  grep -q '"status":"completed"' "$HC_MCP_FIXTURE_DIR/retain_seed_ok.raw" || die "seed retain did not complete (retain_seed_ok.raw 확인)"
  # ⑪ MCP recall 성공. v1.0.0 의 recall 은 한글을 escape 하지 않는다(실측) - 있는지는 로그로만 남기고 NOTES.md 에 적는다.
  _mcp_capture recall_ok "$(jq -nc --arg q "$HC_SEED_RECALL_QUERY" \
    '{jsonrpc:"2.0",id:11,method:"tools/call",params:{name:"recall",arguments:{query:$q,max_tokens:4096}}}')"
  if grep -Eq '\\\\u[0-9a-fA-F]{4}' "$HC_MCP_FIXTURE_DIR/recall_ok.raw"; then log "recall_ok.raw: non-ASCII \\uXXXX escapes present"; else log "recall_ok.raw: no \\uXXXX escapes (Korean is sent as UTF-8)"; fi
  # ⑪b unescape 시험 원본: 허용 목록 도구(get_memory)는 v1.0.0 에서도 한글을 \uXXXX 로 보낸다. 없으면 unescape 시험이 공허하므로 채집을 멈춘다.
  seed_id="$(sed -n 's/^data: //p' "$HC_MCP_FIXTURE_DIR/retain_seed_ok.raw" | jq -r '.result.structuredContent.memory_ids[0]')"
  [ -n "$seed_id" ] && [ "$seed_id" != null ] || die "seed memory id not found in retain_seed_ok.raw"
  _mcp_capture get_memory_ok "$(jq -nc --arg id "$seed_id" \
    '{jsonrpc:"2.0",id:15,method:"tools/call",params:{name:"get_memory",arguments:{memory_id:$id}}}')"
  grep -Eq '\\\\u[0-9a-fA-F]{4}' "$HC_MCP_FIXTURE_DIR/get_memory_ok.raw" || die "get_memory_ok.raw 에 비ASCII \\uXXXX escape 가 없다 - unescape 시험이 공허해진다"
  # ⑫ REST recall: gateway/context_triggers.py upstream_recall 이 retain_gates 를 위해 보내는 그 요청(POST .../memories/recall, {query, max_tokens}).
  req="$(jq -nc --arg q "$HC_SEED_RETAIN_BODY" '{query:$q,max_tokens:4096}')"
  _rest_capture_full "$HC_REST_FIXTURE_DIR/recall_gate.json" POST "$bank_path/memories/recall" "$req"
  # ⑬ reflect 2건: include_based_on 미지정 / true. true 원본에는 based_on 이 있어야 제거 시험이 공허하지 않다.
  _mcp_capture reflect_default "$(jq -nc --arg q "$HC_SEED_REFLECT_QUERY" \
    '{jsonrpc:"2.0",id:12,method:"tools/call",params:{name:"reflect",arguments:{query:$q}}}')"
  _mcp_capture reflect_based_on "$(jq -nc --arg q "$HC_SEED_REFLECT_QUERY" \
    '{jsonrpc:"2.0",id:13,method:"tools/call",params:{name:"reflect",arguments:{query:$q,include_based_on:true}}}')"
  _reflect_has_based_on "$HC_MCP_FIXTURE_DIR/reflect_based_on.raw" || die "reflect_based_on.raw 에 값이 있는 based_on 이 없다(없음·null·빈 값) - 제거 시험이 공허해진다"
  # ⑭ 성공형 프레임 안의 저장 실패: upstream 은 파싱 못 하는 timestamp 를 {"status":"error"} 로 돌려주고 isError 는 false 다.
  # 기존 sync_retain_fail.raw(검증 오류, isError true)는 "무변조" 사례로 그대로 둔다.
  _mcp_capture sync_retain_error_frame "$(jq -nc --arg t "$HC_SEED_BAD_TIMESTAMP" \
    '{jsonrpc:"2.0",id:14,method:"tools/call",params:{name:"sync_retain",arguments:{content:"[probe] error frame",tags:["kind:probe"],timestamp:$t}}}')" 2??   # 의도한 실패: 성공형 프레임, HTTP 는 200
  if _frame_is_success_framed_error "$HC_MCP_FIXTURE_DIR/sync_retain_error_frame.raw"; then
    rm -f "$HC_MCP_FIXTURE_DIR/sync_retain_error_frame.UNAVAILABLE.md"
  else
    printf '# sync_retain_error_frame UNAVAILABLE\n\n채집 시각: %s\n사유: upstream 이 timestamp 오류를 isError:false + status:error 로 반환하지 않음(원문 아래).\n\n```\n%s\n```\n' \
      "$(date -u +%FT%TZ)" "$(cat "$HC_MCP_FIXTURE_DIR/sync_retain_error_frame.raw")" > "$HC_MCP_FIXTURE_DIR/sync_retain_error_frame.UNAVAILABLE.md"
    rm -f "$HC_MCP_FIXTURE_DIR/sync_retain_error_frame.raw" "$HC_MCP_FIXTURE_DIR/sync_retain_error_frame.headers"
    log "WARN: 성공형 실패 프레임 미재현 - sync_retain_error_frame.UNAVAILABLE.md 기록"
  fi
  jq -n --arg ts "$(date -u +%FT%TZ)" --arg bank "$HC_MCP_BANK" --arg seed "$HC_SEED_BODY" --arg rb "$HC_SEED_RETAIN_BODY" \
        --arg rq "$HC_SEED_RECALL_QUERY" --arg fq "$HC_SEED_REFLECT_QUERY" --arg bad "$HC_SEED_BAD_TIMESTAMP" \
        '{probedAt:$ts, bank:$bank, seedBody:$seed, retainBody:$rb, seedTags:["project:hindsight-crew","kind:gotcha"], recallQuery:$rq, reflectQuery:$fq, badTimestamp:$bad}' \
    > "$HC_MCP_FIXTURE_DIR/seed_cases.json"
}

# ---------------------------------------------------------------- main (채집)

main() {
  HC_MCP_CALLER_PWD="$PWD"     # --out 의 상대 경로는 호출자 cwd 기준(리포 루트로 cd 하기 전에 잡는다)
  cd "$HC_MCP_ROOT"
  # --handshake-only 는 인자 위치와 무관하게 쓰기 없는 모드다(첫 인자가 아니면 쓰기 채집으로 빠지던 결함).
  local hs=0 rest=() a
  for a in "$@"; do if [ "$a" = "--handshake-only" ]; then hs=1; else rest+=("$a"); fi; done
  [ "$hs" = 1 ] && { handshake_only ${rest[@]+"${rest[@]}"}; return; }
  command -v jq >/dev/null || die "jq required"
  _mcp_preflight

  local mcp_dir="fixtures/mcp" rest_dir="fixtures/rest"
  HC_MCP_FIXTURE_DIR="$mcp_dir"; HC_REST_FIXTURE_DIR="$rest_dir"
  mkdir -p "$mcp_dir" "$rest_dir"
  local mark="probe-n0-$(date -u +%Y%m%dT%H%M%SZ)"
  log "upstream-direct probe: bank=$HC_MCP_BANK mark=$mark"

  # ① initialize (헤더 + 본문 분리 저장). instructions 부재 여부가 S3 전제.
  mcp_handshake || die "MCP handshake failed (hindsight 컨테이너가 Up 인지, 키가 맞는지 확인)"
  _mcp_save_handshake "$mcp_dir"
  log "session=$HC_MCP_SESSION"

  # ② tools/list 원본 (retain류 inputSchema 핀)
  mcp_post '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' > "$mcp_dir/tools_list.raw"

  # ③ sync_retain 성공 (content + tags + timestamp)
  # (#247: ③④⑤ 도 Content-Type 을 재생에 쓰므로 .headers 를 함께 저장한다 - 본문 .raw 는 이전과 같은 바이트다)
  _mcp_capture sync_retain_ok "$(jq -nc --arg c "[probe] sync_retain ok $mark" '{jsonrpc:"2.0",id:3,method:"tools/call",params:{name:"sync_retain",arguments:{content:$c,tags:["project:hindsight-crew","kind:probe"],timestamp:"2026-07-26T00:00:00Z"}}}')"

  # ④ sync_retain 실패 (content 누락 → upstream 검증 실패). isError:true 여야 N5 전제 성립.
  _mcp_capture sync_retain_fail '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"sync_retain","arguments":{"tags":["kind:probe"]}}}' 2??   # 의도한 실패: 본문 수준 isError, HTTP 는 200
  if grep -q '"isError":true' "$mcp_dir/sync_retain_fail.raw"; then
    rm -f "$mcp_dir/sync_retain_fail.UNAVAILABLE.md"
  else
    printf '# sync_retain_fail UNAVAILABLE\n\n채집 시각: %s\n사유: upstream 이 검증 실패를 isError:true 로 반환하지 않음(원문 아래).\n\n```\n%s\n```\n' \
      "$(date -u +%FT%TZ)" "$(cat "$mcp_dir/sync_retain_fail.raw")" > "$mcp_dir/sync_retain_fail.UNAVAILABLE.md"
    rm -f "$mcp_dir/sync_retain_fail.raw" "$mcp_dir/sync_retain_fail.headers"
    log "WARN: isError:true 미재현 — $mcp_dir/sync_retain_fail.UNAVAILABLE.md 기록"
  fi

  # ⑤ 비동기 retain (operation 참조 확인 — N5 안내 문구 전제)
  _mcp_capture retain_async_ok "$(jq -nc --arg c "[probe] async retain $mark" '{jsonrpc:"2.0",id:5,method:"tools/call",params:{name:"retain",arguments:{content:$c,tags:["project:hindsight-crew","kind:probe"],timestamp:"2026-07-26T00:00:00Z"}}}')"

  # ⑥ REST POST memories 성공 (items[] 실형상)
  local bank_path="/v1/$HC_MCP_TENANT/banks/$HC_MCP_BANK"
  local req raw
  req="$(jq -nc --arg c "[probe] rest ok $mark" '{items:[{content:$c,tags:["project:hindsight-crew","kind:probe"],timestamp:"2026-07-26T00:00:00Z",context:"probe"}],async:false}')"
  raw="$(rest_probe POST "$bank_path/memories" "$req")" || die "REST POST memories failed"
  _rest_fixture "$rest_dir/memories_post_ok.json" POST "$bank_path/memories" "$req" "$(rest_status "$raw")" "$(rest_body "$raw")"

  # ⑦ REST POST memories 실패 (timestamp 형식 오류 → 422). 응답의 'unset' 특수값 언급 보존.
  req="$(jq -nc --arg c "[probe] rest fail $mark" '{items:[{content:$c,tags:["kind:probe"],timestamp:"not-a-timestamp"}],async:false}')"
  raw="$(rest_probe POST "$bank_path/memories" "$req")" || die "REST POST memories (fail case) failed"
  _rest_fixture "$rest_dir/memories_post_fail.json" POST "$bank_path/memories" "$req" "$(rest_status "$raw")" "$(rest_body "$raw")"

  # ⑧ GET /config (retain_strategies 존재 여부 = N1 드리프트 게이트 전제)
  raw="$(rest_probe GET "$bank_path/config")" || die "REST GET config failed"
  _rest_fixture "$rest_dir/bank_config.json" GET "$bank_path/config" "" "$(rest_status "$raw")" "$(rest_body "$raw")"

  # ⑨ MCP_INSTRUCTIONS env 실측 (2회차 S2 전제 — 기록만)
  local instr
  if instr="$(_compose exec -T hindsight printenv HINDSIGHT_API_MCP_INSTRUCTIONS 2>/dev/null)" && [ -n "$instr" ]; then
    printf '# HINDSIGHT_API_MCP_INSTRUCTIONS 실측\n\n채집: %s (upstream 직결, `docker compose exec hindsight printenv`)\n\n상태: **set**\n\n```\n%s\n```\n' \
      "$(date -u +%FT%TZ)" "$instr" > "$mcp_dir/env_mcp_instructions.note.md"
  else
    printf '# HINDSIGHT_API_MCP_INSTRUCTIONS 실측\n\n채집: %s (upstream 직결, `docker compose exec hindsight printenv`)\n\n상태: **unset - S2 착수 전 실측 필요**\n\n`printenv` 가 rc=1(미설정). compose 에 이 env 가 없어 upstream 은 initialize instructions 를 발신하지 않는다.\nS2(description suffix / instructions upstream 위임) 착수 전, env 주입 -> hindsight 재기동 -> upstream 직결 재채집 순으로 실측할 것.\n' \
      "$(date -u +%FT%TZ)" > "$mcp_dir/env_mcp_instructions.note.md"
    log "HINDSIGHT_API_MCP_INSTRUCTIONS: unset (S2 게이트 기록)"
  fi

  # ⑩~⑭ 응답 가공 회귀용 추가 채집 (#247): 시드 retain, MCP/REST recall, reflect 2건, 성공형 실패 프레임
  _capture_regression_cases "$bank_path"

  log "wrote:"
  ls -1 "$mcp_dir" "$rest_dir" >&2
}

# _rest_fixture <out> <method> <path> <request-json|""> <status> <response-body>
_rest_fixture() {
  local out="$1" method="$2" path="$3" req="$4" status="$5" body="$6"
  local rjson="null"; [ -n "$req" ] && rjson="$req"
  jq -n --arg method "$method" --arg path "$path" --arg ts "$(date -u +%FT%TZ)" \
        --argjson request "$rjson" --argjson status "$status" \
        --argjson response "$(jq -c . <<<"$body" 2>/dev/null || jq -nc --arg t "$body" '$t')" \
        '{probedAt:$ts, method:$method, path:$path, request:$request, status:$status, response:$response}' > "$out"
}

# sourced 면 main 실행 안 함 (verify/lib.sh 재사용 경로)
case "${BASH_SOURCE[0]}" in
  "$0") main "$@" ;;
esac
