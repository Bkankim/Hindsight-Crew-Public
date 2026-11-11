#!/usr/bin/env bash
set -euo pipefail

caller_dir="$PWD"
cd "$(dirname "$0")/.."
set -a && . ./.env.local && set +a
. "$PWD/scripts/hc-host-env.sh"; hc_load_host_env "$PWD"

# 뱅크 id 는 .env.host.local 의 HC_PERSONAL_BANK 하나다(issue #186). 기본값이 없고, URL 경로에 들어가므로 문자도 제한한다.
[ -n "${HC_PERSONAL_BANK:-}" ] || { echo "TAG_MIGRATE_FAIL - config_missing HC_PERSONAL_BANK" >&2; exit 2; }
case "$HC_PERSONAL_BANK" in
  *[!A-Za-z0-9_-]*) echo "TAG_MIGRATE_FAIL - invalid HC_PERSONAL_BANK (allowed: A-Z a-z 0-9 _ -)" >&2; exit 2 ;;
esac

# --- 호스트 파일 플래그 (issue #43, #109, #143) ------------------------------------------------------
# 컨테이너는 호스트 파일을 못 본다. 아래 표의 다섯 플래그가 가리키는 호스트 파일은 컨테이너 임시 디렉터리에 스테이징하고
# (입력: 표준입력으로 넘겨 쓴다), 실행이 끝나면 성공·실패와 무관하게 출력 파일을 호스트 경로로 되돌린다.
#   in    --apply-manifest FILE / --ids-file FILE / --manifest FILE : 컨테이너가 읽기만 한다(호스트에 있어야 한다)
#   out   --manifest-out FILE                                       : 컨테이너가 쓴다. 파일이 실제로 생겼을 때만 회수한다
#   inout --state FILE                                              : 호스트에 있으면 주입(재개 체크포인트), 실행 뒤 회수
# --state 를 생략한 --manifest 실행은 컨테이너가 MANIFEST.state.json 을 쓰므로 호스트의 같은 이름 파일을 inout 으로 다룬다.
# 정책 alias 는 --apply-manifest/--ids-file(#43) 에서는 주입하지 않는다: 이 모드의 태그는 호스트 계획기(scripts/tag_retag_plan.py)가 이미 정했다.
# 옛 래퍼(#143 이전)로 돌린 --live 가 컨테이너 안에 남긴 체크포인트는 이 래퍼가 찾지 못한다: 재개하려면 --state 로 호스트 경로를 명시한다.
# 플래그 인식은 아래 표 하나다: 약어(--apply, --manif 등)는 표에 없어 그대로 컨테이너 argparse 로 가고 거부된다(allow_abbrev=False, #128).
flag_direction() {
  case "$1" in
    --apply-manifest|--ids-file|--manifest) echo in ;;
    --manifest-out) echo out ;;
    --state) echo inout ;;
  esac
}

io_flags=()   # 병렬 배열: 플래그 이름 / 호스트 절대 경로 / 방향 / 컨테이너 임시 디렉터리 안의 파일명 / forward 안의 값 위치(-1 이면 없음)
io_hosts=()
io_dirs=()
io_names=()
io_pos=()
pair_flag=""
forward=()
while [ "$#" -gt 0 ]; do
  name="$1"
  value=""
  inline=0
  case "$1" in
    --*=*) name="${1%%=*}"; value="${1#*=}"; inline=1 ;;
  esac
  direction="$(flag_direction "$name")"
  if [ -z "$direction" ]; then
    forward+=("$1")
    shift
    continue
  fi
  if [ "$inline" = 1 ]; then
    # `--flag=FILE` 형태(issue #109): argparse 는 이 형태를 받지만 컨테이너는 호스트 파일을 못 보므로 같은 경로로 보낸다.
    [ -n "$value" ] || { echo "$name requires a FILE" >&2; exit 2; }
    shift
  else
    [ "$#" -ge 2 ] && [ -n "$2" ] || { echo "$name requires a FILE" >&2; exit 2; }
    # 값을 빠뜨리면 다음 옵션(예: --dry-run)을 경로로 먹어 그 옵션이 사라진다(argparse 는 값 누락으로 거부했다). `--flag=--x` 형태는 허용한다.
    case "$2" in
      --*) echo "$name requires a FILE (got option-like '$2'; use $name=$2 for a file with that name)" >&2; exit 2 ;;
    esac
    value="$2"
    shift 2
  fi
  case "$name" in
    --apply-manifest|--ids-file)
      [ -z "$pair_flag" ] || { echo "only one of --apply-manifest / --ids-file per run" >&2; exit 2; }
      pair_flag="$name"
      ;;
  esac
  for seen in ${io_flags[@]+"${io_flags[@]}"}; do
    [ "$seen" != "$name" ] || { echo "$name given more than once" >&2; exit 2; }
  done
  # 호출 셸은 `--flag=~/x` 의 ~ 를 펼치지 않는다(공백 형태는 펼친다): 같은 결과가 되도록 여기서 맨 앞 ~ 만 펼친다.
  case "$value" in
    "~") value="$HOME" ;;
    "~/"*) value="$HOME/${value#\~/}" ;;
  esac
  # 위에서 리포 루트로 cd 했으므로 상대 경로는 호출한 디렉터리 기준으로 되돌린다.
  case "$value" in
    /*) ;;
    *) value="$caller_dir/$value" ;;
  esac
  forward+=("$name")
  io_flags+=("$name")
  io_hosts+=("$value")
  io_dirs+=("$direction")
  io_names+=("${#io_flags[@]}-${name#--}")
  io_pos+=("${#forward[@]}")
  forward+=("")     # 컨테이너 임시 경로가 정해지면 채운다
done
# --manifest 는 --live 에서만 읽힌다(--dry-run 은 무시한다): 그때만 호스트에 있어야 하고 기본 state 도 그때만 쓴다.
is_live=0
for arg in ${forward[@]+"${forward[@]}"}; do
  [ "$arg" != "--live" ] || is_live=1
done
# 인자 검증(값 누락·중복·한 실행에 하나)이 모두 끝난 뒤에 입력 파일 존재를 본다.
for i in ${io_flags[@]+"${!io_flags[@]}"}; do
  if [ "${io_dirs[$i]}" = in ] && [ ! -f "${io_hosts[$i]}" ] && { [ "${io_flags[$i]}" != "--manifest" ] || [ "$is_live" = 1 ]; }; then
    echo "${io_flags[$i]}: no such file: ${io_hosts[$i]}" >&2
    exit 2
  fi
done
# --manifest 만 주고 --state 를 생략한 실행: 컨테이너가 MANIFEST.state.json 을 쓰고 읽는다(호스트 쪽 같은 이름과 짝지어 준다).
state_given=0
manifest_index=-1
for i in ${io_flags[@]+"${!io_flags[@]}"}; do
  [ "${io_flags[$i]}" != "--state" ] || state_given=1
  [ "${io_flags[$i]}" != "--manifest" ] || manifest_index="$i"
done
if [ "$state_given" = 0 ] && [ "$manifest_index" -ge 0 ] && [ "$is_live" = 1 ]; then
  io_flags+=("(default state)")
  io_hosts+=("${io_hosts[$manifest_index]}.state.json")
  io_dirs+=(inout)
  io_names+=("${io_names[$manifest_index]}.state.json")
  io_pos+=(-1)
fi
# 서로 다른 플래그가 같은 호스트 파일을 가리키면(경로 표기·심볼릭 링크가 달라도) 회수가 입력(예: 승인된 manifest)을 체크포인트로
# 덮어쓴다: 거부한다. --dry-run 은 --manifest 를 읽지 않으므로 그 쌍은 검사하지 않는다.
if [ "${#io_flags[@]}" -gt 1 ]; then
  pairs_for_check=()
  for i in "${!io_flags[@]}"; do
    pairs_for_check+=("${io_flags[$i]}" "${io_hosts[$i]}")
  done
  python3 -c '
import os, sys
live = sys.argv[1] == "1"
pairs = [(sys.argv[i], sys.argv[i + 1]) for i in range(2, len(sys.argv), 2)]
for a in range(len(pairs)):
    for b in range(a + 1, len(pairs)):
        names = (pairs[a][0], pairs[b][0])
        if not live and "--manifest" in names:
            continue
        first, second = os.path.realpath(pairs[a][1]), os.path.realpath(pairs[b][1])
        # macOS 기본 APFS 는 대소문자를 구분하지 않는다: 대소문자만 다른 경로도 같은 파일로 본다(대소문자 구분 FS 의 거짓 양성은 거부 쪽이라 안전하다).
        if first == second or first.lower() == second.lower() or (
                os.path.exists(first) and os.path.exists(second) and os.path.samefile(first, second)):
            print("%s and %s point to the same file: %s" % (names[0], names[1], pairs[a][1]), file=sys.stderr)
            raise SystemExit(2)
' "$is_live" "${pairs_for_check[@]}"
fi
set -- ${forward[@]+"${forward[@]}"}

io_dir=""
main_started=0
finish() {
  local main_status=$?
  local retrieve_status=0
  trap - EXIT
  set +e
  if [ -n "$io_dir" ] && [ "$main_started" = 1 ]; then
    local pairs=()
    local names=()
    local i
    for i in "${!io_flags[@]}"; do
      case "${io_dirs[$i]}" in
        out|inout) pairs+=("${io_names[$i]}" "${io_hosts[$i]}"); names+=("${io_names[$i]}") ;;
      esac
    done
    if [ "${#pairs[@]}" -gt 0 ]; then
      # 컨테이너에서 base64 JSON 으로 읽어 와 호스트에 원자적으로 쓴다. 파일이 없으면(null) 호스트를 건드리지 않는다.
      {
        printf '%s\n' 'import base64, json, os, sys'
        printf '%s\n' 'result = {}'
        printf '%s\n' 'for name in sys.argv[2:]:'
        printf '%s\n' '    path = os.path.join(sys.argv[1], name)'
        printf '%s\n' '    result[name] = base64.b64encode(open(path, "rb").read()).decode() if os.path.isfile(path) else None'
        printf '%s\n' 'print(json.dumps(result))'
      } | docker compose exec -T hindsight python - "$io_dir" "${names[@]}" | python3 -c '
import base64, json, os, sys
received = json.load(sys.stdin)
pairs = sys.argv[1:]
for index in range(0, len(pairs), 2):
    payload = received.get(pairs[index])
    if payload is None:
        continue
    host = pairs[index + 1]
    os.makedirs(os.path.dirname(host), exist_ok=True)
    temporary = host + ".tmp"
    with open(temporary, "wb") as handle:
        handle.write(base64.b64decode(payload))
    os.replace(temporary, host)
    print("tag-migrate.sh: wrote " + host, file=sys.stderr)
' "${pairs[@]}"
      local statuses=("${PIPESTATUS[@]}")
      if [ "${statuses[0]}" -ne 0 ] || [ "${statuses[1]}" -ne 0 ] || [ "${statuses[2]}" -ne 0 ]; then
        retrieve_status=1
        echo "tag-migrate.sh: could not bring the container output files back to the host" >&2
      fi
    fi
  fi
  if [ -n "$io_dir" ] && [ "$retrieve_status" -ne 0 ]; then
    # 회수가 실패했으면 컨테이너 쪽이 최신 체크포인트의 유일한 사본일 수 있다: 지우지 않고 복구 경로를 알린다.
    echo "tag-migrate.sh: kept the container temp dir for manual recovery: $io_dir (docker compose exec hindsight ls $io_dir)" >&2
  elif [ -n "$io_dir" ]; then
    docker compose exec -T hindsight python -c 'import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$io_dir" >/dev/null 2>&1
  fi
  # 본 실행의 종료 코드를 회수 실패가 덮어 성공으로 만들지 않는다. 본 실행이 성공이었는데 회수가 실패했으면 실패로 드러낸다.
  if [ "$main_status" -eq 0 ] && [ "$retrieve_status" -ne 0 ]; then
    exit "$retrieve_status"
  fi
  exit "$main_status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "${#io_flags[@]}" -gt 0 ]; then
  stage_pairs=()
  for i in "${!io_flags[@]}"; do
    case "${io_dirs[$i]}" in
      in|inout)
        if [ -f "${io_hosts[$i]}" ]; then
          stage_pairs+=("${io_names[$i]}" "${io_hosts[$i]}")
        fi
        ;;
    esac
  done
  # 스테이징: 호스트 파일 내용은 표준입력(base64 리터럴)으로만 간다. 컨테이너가 임시 디렉터리를 만들어 그 경로를 돌려준다.
  io_dir="$(python3 -c '
import base64, sys
pairs = sys.argv[1:]
items = [(pairs[i], base64.b64encode(open(pairs[i + 1], "rb").read()).decode()) for i in range(0, len(pairs), 2)]
print("import base64, os, tempfile")
print("directory = tempfile.mkdtemp(prefix=\"tag-migrate-io-\")")
print("for name, data in %r:" % (items,))
print("    with open(os.path.join(directory, name), \"wb\") as handle:")
print("        handle.write(base64.b64decode(data))")
print("print(directory)")
' ${stage_pairs[@]+"${stage_pairs[@]}"} | docker compose exec -T hindsight python -)"
  for i in "${!io_flags[@]}"; do
    [ "${io_pos[$i]}" -lt 0 ] || forward[${io_pos[$i]}]="$io_dir/${io_names[$i]}"
  done
  set -- "${forward[@]}"
fi

# 본 실행. alias 경로(--dry-run / --live)는 정책 alias 를 앞에 붙이고, manifest 모드는 붙이지 않는다.
# 컨테이너가 stdout 에 찍는 임시 경로(예: MANIFEST_PATH ...)는 호스트 경로로 바꿔 보여 준다(회수한 파일의 실제 위치).
esc_pattern() { printf '%s' "$1" | sed -e 's/[][\\.*^$|]/\\&/g'; }
esc_replacement() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
rewrite=()
for i in ${io_flags[@]+"${!io_flags[@]}"}; do
  rewrite+=(-e "s|$(esc_pattern "$io_dir/${io_names[$i]}")|$(esc_replacement "${io_hosts[$i]}")|g")
done
emit_script() {
  printf "PERSONAL_BANK = '%s'\n" "$HC_PERSONAL_BANK"
  if [ -z "$pair_flag" ]; then
    python3 -c '
import json, os, sys
# 게이트웨이 로더와 같은 병합(issue #187): 오버레이(HC_BANK_POLICIES_LOCAL, 기본 policies/local/)에
# 그 뱅크가 있으면 뱅크 통째로 대체하고, 뱅크에 tag_aliases 가 없으면 추적 defaults 를 상속한다.
with open("policies/bank-policies.json", encoding="utf-8") as handle:
    tracked = json.load(handle)
banks = tracked["banks"]
local_path = os.environ.get("HC_BANK_POLICIES_LOCAL") or "policies/local/bank-policies.json"
if os.path.exists(local_path):
    with open(local_path, encoding="utf-8") as handle:
        local = json.load(handle)
    if set(local) - {"banks"}:
        raise SystemExit("%s: a local overlay may hold only banks (gateway refuses it)" % local_path)
    banks.update(local.get("banks", {}))
aliases = banks[sys.argv[1]].get("tag_aliases", tracked.get("defaults", {}).get("tag_aliases", {}))
cleanup_path = os.environ.get("HC_TAG_MIGRATE_CLEANUP") or "policies/local/tag-migrate-cleanup.json"
cleanup = {}
if os.path.exists(cleanup_path):
    with open(cleanup_path, encoding="utf-8") as handle:
        cleanup = json.load(handle)
delete_tags = list(cleanup.get("delete_tags", []))
remove_tags = list(cleanup.get("remove_tags", []))
print("ALIAS_MAP = " + repr(aliases))
print("DELETE_TAGS = set(" + repr(delete_tags) + ")")
print("REMOVE_TAGS = set(" + repr(remove_tags) + ")")
' "$HC_PERSONAL_BANK"
    printf '\n'
  fi
  cat scripts/tag_migrate.py
}
main_started=1
if [ "${#rewrite[@]}" -gt 0 ]; then
  emit_script | docker compose exec -T hindsight python - "$@" | sed "${rewrite[@]}"
else
  emit_script | docker compose exec -T hindsight python - "$@"
fi
