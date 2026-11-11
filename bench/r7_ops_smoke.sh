#!/usr/bin/env bash
# r7_ops_smoke.sh - 운영 연막 한 회차(issue #414, 상위 #393). 측정 코드: 운영에는 읽기만 한다(저장소 스냅샷·recall 읽기).
#
#   r7_ops_smoke.sh --label 10m|30m --store <운영 역할 저장소> --listing <본문 JSONL> <메타 JSONL>
#                   --owners <소유자 json> --default-owner <owner> --aliases <json> --work <리포 밖 dir>
#                   --state-query <이름>=<문안 파일> [--state-query ...] [--general-query <이름>=<문안 파일> ...]
#                   [--k 5] [--max-usd 2] [--soft-usd 1.6] [--gh-timeout 60] [--dry-run-attach]
#
# 모드(rework #414-1): 운영 모드(기본)는 운영 게이트웨이 응답의 표(`open_table`)를 그대로 판정한다(다시 그리지 않는다).
#   드라이런 모드(`--dry-run-attach`)는 S3 꺼진 운영에서 사전 연막을 다시 돌릴 때만, 스냅샷 저장소로 표를 다시 그려 붙인다.
#   두 모드 모두 스냅샷 저장소는 table-gate·smoke 의 평가 입력(최대 번호·교차 충돌 근거)이다.
# 순서(한 단계라도 실행기 실패면 거기서 멈춘다, 롤백은 끝까지 간다):
#   1) snapshot: 운영 저장소를 sqlite 백업 API(읽기 전용 연결)로 <work>/<label>/store.sqlite 에 복사, sha256·integrity 기록
#   2) smoke-recall: 모든 질의(상태 + 일반) 한 t0(= 지금, 초 단위)로 운영 recall 읽기(`--allow-prod --read-only`)
#   3) (드라이런 모드만) smoke-attach: 질의마다 스냅샷 저장소로 표 부착
#   4) table-presence: 상태 질의 응답에 표가 없으면 실패(`no-open-table:<질의>`), 일반 질의 응답에 표가 있으면 롤백
#      범주 `general_attached`(표 수)
#   5) table-gate: 상태 질의 표(결정적 검사 + 표 정확도)
#   6) 상태 질의마다 smoke-dllm(K) -> smoke(일반 질의는 --table-recall)
# 마지막 줄: `VERDICT pass|rollback|runner_failure mode=ops|dry-run-attach <범주>=<수> ...`(범주 = table-presence·
# table-gate·smoke 요약 gate.rollback 합). runner_failure 면 그 앞에 `CAUSE <단계> rc=<코드> fail=<요약 fail>` 줄.
# 단계마다 `STEP <단계> rc=<코드> verdict=<값> secs=<초>`(단계 이름: snapshot, smoke-recall, smoke-attach:<질의>,
# table-presence, table-gate, smoke-dllm:<질의>, smoke:<질의>).
# 종료 코드: 0 pass, 4 rollback, 1 runner_failure, 2 인자 오류.
#
# 환경: smoke-recall 운영 대상 선택(HC_ENV_FILE·HC_PROD_ROOT 등, #242)과 smoke-dllm 의 CLAUDE_CODE_OAUTH_TOKEN 은 호출자가 이
# 스크립트 env 로 넘긴다(값은 출력·기록하지 않는다). 하위 명령 로그는 <work>/<label>/logs(0600)에만 쓴다.
# R7_CMD(기본 uv run ... bench/r7_endtoend.py)·R7_PY(기본 python3)는 테스트에서 가짜로 바꾼다.
set -u
umask 077

die() { echo "r7_ops_smoke: $*" >&2; exit 2; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
R7_CMD="${R7_CMD:-uv run --python 3.12 --with-requirements $REPO/gateway/requirements.txt python -I $REPO/bench/r7_endtoend.py}"
R7_PY="${R7_PY:-python3}"

LABEL="" STORE="" BODIES="" META="" OWNERS="" DOWNER="" ALIASES="" WORK=""
K=5 MAX_USD=2 SOFT_USD=1.6 GH_TIMEOUT=60 MODE=ops
STATE_Q=() GENERAL_Q=()
while [ $# -gt 0 ]; do
  case "$1" in
    --label) LABEL="${2-}"; shift 2 ;;
    --store) STORE="${2-}"; shift 2 ;;
    --listing) BODIES="${2-}"; META="${3-}"; shift 3 || die "--listing needs two files" ;;
    --owners) OWNERS="${2-}"; shift 2 ;;
    --default-owner) DOWNER="${2-}"; shift 2 ;;
    --aliases) ALIASES="${2-}"; shift 2 ;;
    --work) WORK="${2-}"; shift 2 ;;
    --state-query) STATE_Q+=("${2-}"); shift 2 ;;
    --general-query) GENERAL_Q+=("${2-}"); shift 2 ;;
    --k) K="${2-}"; shift 2 ;;
    --max-usd) MAX_USD="${2-}"; shift 2 ;;
    --soft-usd) SOFT_USD="${2-}"; shift 2 ;;
    --gh-timeout) GH_TIMEOUT="${2-}"; shift 2 ;;
    --dry-run-attach) MODE=dry-run-attach; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
case "$LABEL" in 10m|30m) ;; *) die "--label must be 10m or 30m" ;; esac
for v in STORE BODIES META OWNERS DOWNER ALIASES WORK; do
  [ -n "${!v}" ] || die "missing argument for $v"
done
[ "${#STATE_Q[@]}" -gt 0 ] || die "at least one --state-query"
for q in "${STATE_Q[@]}" "${GENERAL_Q[@]+"${GENERAL_Q[@]}"}"; do
  case "$q" in *=*) ;; *) die "query must be <name>=<file>: $q" ;; esac
done
mkdir -p "$WORK" || die "cannot create --work"
WORK="$(cd "$WORK" && pwd -P)"
case "$WORK/" in "$REPO"/*) die "--work must be outside the repository" ;; esac
D="$WORK/$LABEL"
[ -e "$D" ] && die "round directory already exists: $D"
mkdir -m 700 "$D" "$D/logs" "$D/rec" || die "cannot create the round directory"
STEPS="$D/steps.tsv"
: > "$STEPS"

# run <단계 이름> <명령...>: 표준 출력·오류를 logs 에, (이름, 종료 코드, 초) 를 steps.tsv 에.
run() {
  local name="$1"; shift
  local s=$SECONDS
  "$@" > "$D/logs/$name.out" 2> "$D/logs/$name.err"
  local rc=$?
  printf '%s\t%s\t%s\n' "$name" "$rc" "$((SECONDS - s))" >> "$STEPS"
  return $rc
}

finish() {
  "$R7_PY" -I - "$STEPS" "$D/logs" "$MODE" <<'PY'
import json, os, sys
steps_path, logs, mode = sys.argv[1], sys.argv[2], sys.argv[3]
cats = {}
verdict, causes = "pass", []
for line in open(steps_path, encoding="utf-8"):
    name, rc, secs = line.rstrip("\n").split("\t")
    rc = int(rc)
    last = None
    try:
        out = [x for x in open(os.path.join(logs, name + ".out"), encoding="utf-8").read().splitlines() if x.strip()]
        last = json.loads(out[-1]) if out else None
    except (OSError, ValueError):
        last = None
    is_gate = name in ("table-gate", "table-presence") or name.startswith("smoke:")      # 요약 gate 를 내는 단계
    v = last.get("verdict") if is_gate and isinstance(last, dict) else None
    print("STEP %s rc=%d verdict=%s secs=%s" % (name, rc, v if v is not None else "-", secs))
    fail = (last or {}).get("fail") if isinstance(last, dict) else None
    if is_gate:
        rb = (last or {}).get("rollback") if isinstance(last, dict) else None
        ok = isinstance(rb, dict) and ((rc == 0 and v == "pass") or (rc == 4 and v == "rollback"))
        if ok:
            for k, n in rb.items():
                cats[k] = cats.get(k, 0) + int(n)
            if rc == 4:
                verdict = "rollback" if verdict == "pass" else verdict
            continue
    elif rc == 0:
        continue
    causes.append("CAUSE %s rc=%d fail=%s" % (name, rc, json.dumps(fail, ensure_ascii=False) if fail else "-"))
    cats["runner_failure"] = cats.get("runner_failure", 0) + 1
    verdict = "runner_failure"
for c in causes:
    print(c)
print(("VERDICT %s mode=%s %s" % (verdict, mode, " ".join("%s=%d" % (k, cats[k]) for k in sorted(cats)))).rstrip())
sys.exit({"pass": 0, "rollback": 4}.get(verdict, 1))
PY
  exit $?
}

# 1) 스냅샷(운영 저장소는 읽기 전용 연결)
run snapshot "$R7_PY" -I - "$STORE" "$D/store.sqlite" "$D/store-copy.json" <<'PY' || finish
import datetime, hashlib, json, os, sqlite3, sys
src, dst, rec = sys.argv[1:4]
h = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
if not os.path.isfile(src):
    print(json.dumps({"fail": ["store-missing"]}))
    sys.exit(1)
before = h(src)
s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
d = sqlite3.connect(dst)
s.backup(d)
d.close()
s.close()
os.chmod(dst, 0o600)
c = sqlite3.connect(dst)
ok = c.execute("PRAGMA integrity_check").fetchone()[0]
c.close()
doc = {"src_sha256_before": before, "src_sha256_after": h(src), "copy_sha256": h(dst), "integrity": ok,
       "at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
fd = os.open(rec, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, json.dumps(doc, sort_keys=True).encode())
os.close(fd)
print(json.dumps(doc))
sys.exit(0 if ok == "ok" else 1)
PY
SNAP="$D/store.sqlite"

# 2) recall 읽기(모든 질의 같은 t0)
T0="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
QARGS=()
for q in "${STATE_Q[@]}" "${GENERAL_Q[@]+"${GENERAL_Q[@]}"}"; do QARGS+=(--query "$q"); done
# shellcheck disable=SC2086
run smoke-recall $R7_CMD smoke-recall --allow-prod --read-only "${QARGS[@]}" --t0 "$T0" --out-dir "$D/rec" || finish

# 3) 판정할 표: 운영 모드 = 운영 응답 그대로(rec/<질의>.json), 드라이런 모드 = 스냅샷으로 다시 그린 표(rec/<질의>-attached.json)
SUF=""
if [ "$MODE" = dry-run-attach ]; then
  SUF="-attached"
  for q in "${STATE_Q[@]}" "${GENERAL_Q[@]+"${GENERAL_Q[@]}"}"; do
    n="${q%%=*}"
    # shellcheck disable=SC2086
    run "smoke-attach:$n" $R7_CMD smoke-attach --recall "$D/rec/$n.json" --store "$SNAP" --aliases "$ALIASES" \
      --out "$D/rec/$n-attached.json" || finish
  done
fi

# 4) 표 유무: 상태 질의 응답(모든 run)에 표가 없으면 실패, 일반 질의 응답의 표는 롤백 범주 general_attached
SN=() GN=()
for q in "${STATE_Q[@]}"; do SN+=("${q%%=*}"); done
for q in "${GENERAL_Q[@]+"${GENERAL_Q[@]}"}"; do GN+=("${q%%=*}"); done
run table-presence "$R7_PY" -I - "$D/rec" "$SUF" "${SN[@]}" -- "${GN[@]+"${GN[@]}"}" <<'PY'
import json, os, sys
d, suf = sys.argv[1], sys.argv[2]
i = sys.argv.index("--")
states, generals = sys.argv[3:i], sys.argv[i + 1:]
def runs(n):
    with open(os.path.join(d, n + suf + ".json"), encoding="utf-8") as fh:
        return json.load(fh).get("runs") or []
fail = []
for n in states:
    rs = runs(n)
    if not rs or not all(r.get("open_table") for r in rs):
        fail.append("no-open-table:" + n)
ga = sum(1 for n in generals for r in runs(n) if r.get("open_table"))
rb = {"general_attached": ga, "runner_failure": len(fail)}
print(json.dumps({"fail": fail, "verdict": "rollback" if fail or ga else "pass", "rollback": rb}))
sys.exit(1 if fail else 4 if ga else 0)
PY
rc=$?; [ $rc -eq 0 ] || [ $rc -eq 4 ] || finish

COMMON=(--store "$SNAP" --aliases "$ALIASES" --listing "$BODIES" "$META" --github-owners "$OWNERS"
        --github-default-owner "$DOWNER" --gh-timeout "$GH_TIMEOUT")

# 5) 결정적 검사(상태 질의 표). 롤백(4)은 계속, 실패는 멈춘다.
TG=()
for q in "${STATE_Q[@]}"; do TG+=(--recall "$D/rec/${q%%=*}$SUF.json"); done
# shellcheck disable=SC2086
run table-gate $R7_CMD table-gate --t0 "$T0" "${TG[@]}" "${COMMON[@]}" --work "$D" --label "r7-ops-$LABEL-tg"
rc=$?; [ $rc -eq 0 ] || [ $rc -eq 4 ] || finish

# 6) 상태 질의마다 d-llm -> smoke
TR=()
for q in "${GENERAL_Q[@]+"${GENERAL_Q[@]}"}"; do TR+=(--table-recall "$D/rec/${q%%=*}$SUF.json"); done
for q in "${STATE_Q[@]}"; do
  n="${q%%=*}"
  # shellcheck disable=SC2086
  run "smoke-dllm:$n" $R7_CMD smoke-dllm --recall "$D/rec/$n$SUF.json" --t0 "$T0" --k "$K" \
    --label "r7-ops-$LABEL-$n" --gen-out "$D/$n-gen.json" --spent-dir "$D" --max-usd "$MAX_USD" --soft-usd "$SOFT_USD" \
    || finish
  # shellcheck disable=SC2086
  run "smoke:$n" $R7_CMD smoke --t0 "$T0" --gen "$D/$n-gen.json" --recall "$D/rec/$n$SUF.json" \
    "${TR[@]+"${TR[@]}"}" "${COMMON[@]}" --work "$D" --label "r7-ops-$LABEL-$n"
  rc=$?; [ $rc -eq 0 ] || [ $rc -eq 4 ] || finish
done
finish
