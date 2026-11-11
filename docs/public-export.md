# 공개판 내보내기

운영 리포에서 공개판으로 가는 단방향 내보내기의 경계와 판정 기록이다.
결정 정본은 `docs/adr/0002-public-edition-is-a-one-way-export.md`다.
2026-11-10 개정으로 공개판은 이력 없는 단일 커밋 스냅샷이고, 하이브리드 이력 유지는 대체됐다.
아래 판정표의 과거 이력 서술은 그 전 기록이고, 공개 절차는 "사용법"을 따른다.
경계의 기계 판독 정의는 `scripts/public-export/manifest.json`, 검사기는 `scripts/public-export/check_manifest.py`다.

## 공개 판정표

기준: 2026-11-09, #423 브랜치 HEAD(7절 이름 규칙은 #439, 8절 유출 검사 예외는 #441).
재현: `python3 scripts/public-export/check_manifest.py`(HEAD), `--history --rev origin/main`(과거 이력), `--local-deny <리포 밖 파일>`(식별 패턴), `--local-rules <리포 밖 파일>`(로컬 규칙, 2절).

### 1. 경로 경계

원칙: 공개용으로 쓴 것만 싣는다(#455).
`docs/`와 `scripts/`는 디렉터리째 담지 않고 파일을 하나씩 적는다.
그래서 새 문서·스크립트는 `include`에 적어야 공개판에 실리고, 적지 않으면 아래 HEAD 검사 (a)가 실패한다.

- 담는 경로(`include`): 코드(gateway, rerank-server, bootstrap, install, verify, profiles, templates), `scripts/`의 명시 목록(분류는 `scripts/README.md`: 설치자용·운영자 전용·공개판 관리용), tests, bench, fixtures, `policies/`(공용 정책, `policies/local/`은 gitignore), 공개용 문서의 명시 목록(`docs/adr/`, `docs/operating-guide.md`, `docs/operations.md`, `docs/public-export.md`, `docs/THREAT-MODEL.md`, `docs/benchmarks.md`, `docs/model-refresh-cron.md`), `artifacts/arch-v2/`, 최상위 공개 파일, `.github/`.
- 이력 전체에서 빼는 경로(`exclude_history`): `artifacts/` 중 `arch-v2/` 밖 전부(운영 전환·검증 기록), 옛 개인 정책 fixture 디렉터리, 삭제된 운영 경로 8개(#4 감사: 운영 점검 메모, 캐시 패치, 운영 오버레이 코드와 그 테스트·검사 스크립트).
- 같은 키로 빼는 운영 전용 문서·표(#455): 운영자 런북 `docs/deploy-procedure.md`·`docs/RUNBOOK.md`, 계획 `docs/plans/`, 전환 창 `docs/*-window.md`, 리허설 기록 `docs/public-export-rehearsals.md`, 운영 뱅크 분석 `docs/analysis/`, 운영자 재태깅 표 `scripts/retag-map.json`.
  운영판에서는 그대로 두고(운영자의 기록이다) 공개판 이력에서만 뺀다.
  공개 문서가 기대던 절차는 `docs/operations.md`에 일반화해 옮겼다.
- 명시 목록 밖이라 이력에서 함께 빠지는 것: 지금은 `__PREFIX__` 템플릿으로 바뀐 `scripts/launchd/`의 옛 예시(운영자 라벨 접두 이름).
- HEAD 검사 (a): include 밖 추적 파일 목록과 exclude_history에 걸리는 HEAD 파일 목록이 같다(통과).
- 경계 회귀 테스트: `tests/test_public_boundary.py`(운영 전용 경로가 include 밖·exclude_history 안인지, `scripts/README.md` 분류가 공개 `scripts/` 항목과 맞는지, 공개 문서의 상대 링크가 공개 파일을 가리키는지).

### 2. 일반어 금칙(`deny_patterns`) - HEAD

일반어 금칙(`deny_patterns`), 커밋 메시지 치환(`message_rewrites`), 경로별 허용(`accept`)의 값은 리포 밖 로컬 규칙 파일에 있다(#456).
값 자체가 숨기는 맥락을 드러내는 해독표라서 매니페스트와 이 문서에는 적지 않는다.
리포 매니페스트의 세 키는 빈 목록이다(형식 안내로 키만 남긴다).

로컬 규칙 파일 계약(검사기 `check_manifest.py`와 내보내기 도구 `export.py`가 같은 로더 `check_manifest.load_local_rules`·`merge_local_rules`를 쓴다):

| 항목 | 계약 |
|---|---|
| 경로 | `--local-rules <파일>`. 기본 `~/.config/hindsight-crew/public-export/local-rules.json`. 리포 밖 파일이어야 하고 권한은 600으로 둔다 |
| 없을 때 | 기본 경로에 파일이 없으면 빈 규칙이다(매니페스트 값만 쓴다). 명시한 경로가 없거나 기본 경로가 깨진 심링크면 exit 2 |
| 형식 | JSON 객체 `{"deny_patterns": [...], "message_rewrites": [...], "accept": [...]}`. 키는 이 셋만 쓰고, 빠진 키는 빈 목록이다 |
| 항목 | 매니페스트와 같은 모양이다. `deny_patterns`: `{"id": ..., "regex": ...}`. `message_rewrites`: `{"regex": ..., "replace": ...}`(파이썬 `re.sub` 치환 문자열). `accept`: `{"path": ..., "pattern": <deny id>, "handling": "export-rewrite" | "keep"}`에 선택 `"reason"`(handling 누락·허용 밖 값은 형식 오류). 값은 모두 문자열이고 이 밖의 필드는 형식 오류다 |
| 합치기 | 키마다 매니페스트 목록 뒤에 로컬 목록을 이어 붙인다. `message_rewrites`는 그 순서대로 적용한다 |
| 형식 오류 | JSON 오류, 최상위가 객체가 아님, 키 밖 값, 목록이 아닌 값, 항목 형식 오류, handling 허용 밖 값, 잘못된 정규식(큰 반복 횟수 포함)·치환 문자열은 exit 2다. 메시지는 키 이름·항목 번호(1부터)·JSON 줄 번호만 낸다(규칙 값과 모르는 키 이름은 내지 않는다) |
| 출력 | 두 도구 모두 `로컬 규칙: deny_patterns N, message_rewrites N, accept N`(기본 경로에 파일이 없으면 `없음(빈 규칙)`)만 낸다. 검사기 (b) 실패 줄은 패턴 id 대신 합친 목록의 번호만 낸다(`(b) <경로>: deny_patterns #<번호> x<건수>`) |

- `accept`의 `handling`이 `export-rewrite`이면 HEAD 검사 (b)에서만 덮고, 내보낸 뒤 독립 유출 검사에서는 쓰지 않는다(그 경로는 치환으로 없어져야 한다). `keep`이면 내보낸 뒤에도 덮는다.
- 매니페스트 최상위 키는 `version`·`about`·`include`·`exclude_history`·`history_rewrite_paths`·`public_words`·`public_literals`와 위 세 키뿐이다. 이 밖의 키(오타 등)는 exit 2다(키 이름은 내지 않고 개수만).
- 재현: `python3 scripts/public-export/check_manifest.py --local-rules <리포 밖 파일>`(HEAD (b)), 여기에 `--history --rev origin/main`(과거 이력 (c), 정보 출력).
- 경로별 처리 내역과 과거 이력 적중 수치는 패턴을 좁히는 단서라 리포 밖 보고에만 둔다.

### 3. 식별 패턴(리포 밖 `local-deny.txt`) - HEAD

식별 패턴은 리포 밖 `local-deny.txt`에 있다.
형식은 한 줄에 정규식 하나, 빈 줄과 `#` 주석은 건너뛰고, 대소문자 무시로 적용한다("사용법" 1).
패턴 내용·개수·대상 맥락은 적지 않는다.
재현: `python3 scripts/public-export/check_manifest.py --local-deny <리포 밖 파일>` -> 패턴 번호별 건수만 출력한다.

### 4. 데모 fixture와 HEAD에 남긴 실제 이름

옛 개인 정책 fixture는 같은 구조의 데모 fixture `tests/fixtures/policies/sample/`로 바꿨다.
뱅크 5개·블록 키·트리거 1개의 구조는 테스트가 기대는 그대로 두었고, 데모 fixture의 project 어휘·캠페인 지침은 합성 값이다(트리거 id·질의·메모도 데모 값, 키워드는 공개 제품명이라 유지). `personal-alice`·`team-eng`·`defaults` 블록은 그대로다.
운영자 이름 -> 데모 이름 표는 리포 밖 로컬 파일이고 #426이 과거 이력 치환에 다시 쓴다.

HEAD에서 실제 이름을 유지한 테스트(내보내기 때 같은 표로 코드와 함께 치환, #429가 후보 안 pytest로 확인):

| 부류 | 파일 | 이유 |
|---|---|---|
| 가. 유출 가드 | `tests/test_no_owner_literals.py`, `tests/fixtures/owner_literal_allowlist.txt`, `tests/test_policy_overlay.py`(가드 1줄), `tests/test_next_stack.py`(가드 1줄) | 실제 소유자 이름이 런타임 파일에 없음을 검사한다. 데모 이름이면 가드가 공허하다 |
| 나. 운영 코드에 묶임 | `tests/test_bench_*.py`(5개), `tests/test_r5_labels.py`, `tests/test_host_env.py`, `tests/test_render_launchd.py` | bench·설치·launchd 템플릿의 실제 값을 단언한다 |
| 다. 운영자 표 | `scripts/retag-map.json` | 운영자 기본 재태깅 표. 운영 전용이라 공개판 이력에서 뺀다(#455, 1절). 테스트는 데모 사본 `tests/fixtures/tag_retag/retag-map.json`을 쓴다 |

### 5. gitleaks

`.gitleaks.toml`은 기본 규칙(`useDefault = true`)에 경로 + 줄 정규식 허용만 더한다(커밋 sha 허용 없음).
기본 규칙만 쓰면 이력에서 generic-api-key 8건, 이 설정으로는 0건이다.

| 경로 | 건수 | 판정 |
|---|---|---|
| `tests/test_item_keys_v21.py` | 1 | 규칙 버전 상수 문자열 |
| `gateway/item_open_table.py` | 1 | 판정 사유 코드 문자열 |
| `tests/test_verify_gate9_llm.py` | 2 | 환경변수 이름 + 대문자 테스트 상수 할당(허용은 이 꼴로 한정) |
| `tests/test_bench_refresh.py` | 1 | 환경변수 이름 목록 |
| `tests/test_next_stack.py` | 1 | 환경변수 이름 목록 |
| `rerank-server/tests/test_server.py` | 2 | 로그 노출 검사용 가짜 표식 문자열 |


### 6. 내보내기 때 치환할 HEAD 경로(운영자 이름이 남은 운영 경로)

HEAD에서 운영자 뱅크·프로젝트 이름이 남은 include 경로는 `tests/` 밖 59개이고(#455 경계, 2026-11-09 로컬 표 기준), 모두 매니페스트 `history_rewrite_paths` 안에 있다.
운영 리포에서는 실제 운영에 쓰이므로 고치지 않고, #426이 리포 밖 이름 표로 치환한다.
재현: `python3 scripts/public-export/check_manifest.py --rename-table <리포 밖 표>` -> `rename-table HEAD: inside N, outside 0`(밖에 남으면 경로를 출력하고 실패).
검사기는 내보내기 치환과 같은 이름 규칙(아래 7절)으로 센다.
출력 경로의 이름 부분은 경계 없는 부분 문자열(대소문자 무시)로 가린다.
2026-11-09 #439 규칙 기준 `inside 946, outside 0`, #455 경계(운영 런북·계획·재태깅 표 제외) 뒤 `inside 552, outside 0`.

| 묶음 | 경로 수 | 무엇 |
|---|---|---|
| `.gitignore` | 1 | 로컬 도구 디렉터리 이름 |
| `CONTEXT.md` | 1 | 용어 예시 |
| `artifacts/` | 19 | 실험 보고서의 뱅크·프로젝트 이름 인용 |
| `bench/` | 14 | 운영자 측정 도구의 기본 뱅크·소유자 값 |
| `docs/` | 2 | ADR 0001 결정 배경의 태그 예시, 이 문서(운영 런북·계획 문서는 1절대로 이력째 뺐다) |
| `fixtures/` | 1 | MCP 녹화 메모 |
| `gateway/` | 7 | 주석·docstring의 예시 |
| `profiles/` | 1 | 운영 프로필 주석 |
| `rerank-server/` | 1 | 주석 |
| `scripts/` | 9 | 운영 스크립트 주석(운영자 재태깅 표는 1절대로 이력째 뺐다) |
| `verify/` | 3 | 게이트 스크립트 주석 |

`tests/`의 경로(위 4절의 (가)(나) 부류 테스트 12개 포함, 같은 날 33개)도 같은 치환 대상이다.
`tests/` 아래 이름 표 적중 경로 수가 4절 가·나 부류보다 많다(4절 부류 밖 테스트에도 내보내기 치환 대상 이름이 있다).

과거 판본에만 운영자 이름이 남은 `policies/context-triggers.json`, `README.md`, `README.en.md`, `docker-compose.yml`은 #426 실데이터 dry-run의 독립 유출 검사 적중으로 `history_rewrite_paths`에 더했다(HEAD 기준 위 표와 별개).
경로 이름에 든 운영자 이름도 내보내기가 같은 표로 바꾼다(`--paths-from-file` rename).
#455 뒤 include HEAD 경로 이름에는 없다(그런 이름이 든 `docs/plans/` 2개는 1절대로 이력째 빠졌고, 과거 launchd 예시 6개 중 `scripts/launchd/`의 3개는 명시 목록 밖이라 빠지고, `rerank-server/launchd/`의 3개는 이름을 바꿔 남는다).
검사기 `--rename-table`도 HEAD 경로 이름을 함께 센다.

### 7. 이름 규칙: 치환과 독립 유출 검사는 다른 구현이다 (#439)

#429 리허설에서 둘이 같은 정규식(밑줄을 단어 문자로 보는 경계, 대소문자 구분)을 써서, 치환이 놓친 밑줄 접미·대소문자 변형을 유출 검사도 0으로 통과시켰다.
그래서 둘을 나눈다.

| | 치환(`export.Rewriter`, 검사기 `--rename-table`) | 독립 유출 검사(`export.Scanner`) |
|---|---|---|
| 맞추는 법 | 대소문자 무시. 앞뒤가 `[A-Za-z0-9]`가 아닐 때만(밑줄·하이픈은 경계). 긴 이름 먼저 | 표 왼쪽 열 모든 이름(길이 제한 없음)을 경계 없는 부분 문자열, 대소문자 무시로 센다. 치환 정규식을 쓰지 않는다 |
| 결과 | 맞은 문자열이 표의 어떤 행 왼쪽과 꼴까지 같으면 그 행의 데모를 그대로 쓴다. 아니면 그 이름(소문자화한 키)의 파일 순서상 첫 행 데모에 꼴을 옮긴다(전부 대문자 -> 전부 대문자, 첫 글자만 대문자 -> 첫 글자 대문자, 그 밖 -> 데모 그대로) | 0건이어야 성공(fail-closed). 허용 목록 없음(좁은 예외는 아래 8절 둘과 공개 문자열 둘: 이 절의 `public_literals`, 8절 끝 공개 식별자 `public_words`, #441·#468) |
| 표면 | `history_rewrite_paths` 안 blob, 그 경로 이름, 모든 커밋 메시지(trailer 포함) | 거른 리포의 모든 blob, 파일 경로, 커밋·태그 메시지(trailer 포함), 작성자·커미터·태거 이름·주소 |

- 작성자 표면의 예외는 `--mailmap` 첫째 칸(대상) 이름·주소와 정확히 같은 값뿐이다(이름과 주소를 따로 비교). 도구가 mailmap으로 만든 공개 신원이고, 대상 주소에는 공개 리포가 걸릴 GitHub 계정명이 들어간다.
- 작성자 표면의 local-deny는 이름·주소 각각과 함께 `이름 <주소>` 결합 문자열에도 쓴다(이름·주소에 걸친 패턴). 대상 신원과 정확히 같은 결합 값만 뺀다.
- 하이픈이 든 표 이름은 유출 검사에서 구분자 변형(하이픈을 `_`로 바꾼 꼴, 하이픈을 없앤 꼴)도 같은 표 행으로 센다. 치환은 원래 철자만 바꾸므로, 변형은 표 소유자가 행으로 더한다(`--suggest-rows`가 제안한다).
- 대소문자 무시는 치환과 유출 검사 모두 파이썬 `re.I` 접기를 쓴다(`İ`·`ı`·`ſ`·켈빈 기호 `K` 같은 변형도 맞춘다).
- 커밋 메시지 표면에서는 도구가 더한 마지막 `Exported-From: <sha>` 줄을 세지 않는다(원본 sha 속 hex 부분 일치는 표로 고칠 수 없다). 메시지의 나머지는 그대로 센다.
- 영숫자에 붙은 이름(`x이름y`)은 치환하지 않고 유출 검사가 잡는다. 표 소유자가 그 토큰을 표에 행으로 더한다(`--suggest-rows`, "사용법" 1).
- 공개 리포 소유자 GitHub 계정명은 매니페스트 `public_words`라 치환하지 않는다(#468). 그래서 `github.com/<계정>` 링크와 HEAD 값(`<계정>/<리포>#N` 꼴 키, 소유자 기본값) 속 계정명은 공개판에서 공개 리포 URL과 같은 실제 계정으로 남는다.
  매니페스트 `public_literals`의 정확한 문자열(공개 리포 URL 접두와 공개 MCP 서버 이름, `/` 또는 `-`가 든 값만, 첫 글자나 마지막 글자가 `/`·`-`인 값과 이름 표 이름 자체는 안 된다)은 보호한다(치환 전에 자리표시자로 감췄다가 되돌리고, 유출 검사는 그 범위 안 적중만 뺀다).
  `-`도 받는 까닭은 `memory-vault`처럼 하이픈으로 이은 공개 이름을 지키려는 것이다. 짧은 단어 하나로 이름을 통째로 보호하지 못하게 하는 원래 뜻은 그대로다(`이름-`·`-이름` 꼴은 그 이름의 하이픈 합성어를 전부 보호하므로 받지 않는다).
  한계: `이름-x`처럼 구분자 뒤에 글자를 붙인 값은 그 꼴로 시작하는 합성어를 보호해도 도구가 막지 못한다. 매니페스트는 리포 안에서 리뷰되는 파일이라 리뷰로 막는다.

### 8. 독립 유출 검사의 좁은 예외: 검토 토큰과 base64 해독 재검사 (#441, #448)

이름 표를 보강한 뒤 실데이터 dry-run에 남은 유출은 전부 오탐이었다.
표 이름이 일반 영어 단어 안에 우연히 든 경우와, fixture의 base64 덩어리 안 우연 일치(해독본에는 이름·식별 패턴 없음)였다.
일반 단어를 표에 넣어 치환하면 공개판 코드의 영어가 깨지고, 그대로 두면 유출 검사가 영원히 0이 되지 않는다.
그래서 "허용 목록 없음"(direction-3)에 아래 두 예외를 둔다.
그 밖의 예외는 공개 문자열 둘뿐이다: `public_literals` 범위 안 적중(7절)과 `public_words` 온전한 출현 안 이름 적중(이 절 끝 공개 식별자).
예외가 넓으면 실제 식별자가 일반 단어 이름으로 새어 나가므로, 검토 토큰은 토큰 정확 일치·이름 단위·blob과 메시지 표면으로만 좁힌다.

base64 해독 재검사는 면제이기 전에 검사다(#448).
원문에 이름 적중이 있는 덩어리만 해독하면, 이름을 base64로 인코딩한 blob은 원문 적중이 없어 해독 없이 통과한다(인코딩된 이름 사각지대).
그래서 blob의 64자 넘는 덩어리는 이름 적중 여부와 상관없이 해독해 원문과 같은 검사기로 본다.

| | base64 해독 재검사 | 검토 토큰 |
|---|---|---|
| 표면 | blob만 | blob·커밋/태그 메시지만(경로·작성자에는 쓰지 않는다) |
| 순서 | 먼저 판정 | base64 판정이 끝난 적중(해독 실패 덩어리 안 적중 포함)에만 |
| 해독 대상 | 64자 넘는 `[A-Za-z0-9+/]+={0,2}` 덩어리 전부(원문 이름 적중 여부와 상관없이, #448). 해독본 적중은 원래 라벨로 실패다. 해독·UTF-8이 안 되는 덩어리는 원문 적중이 없으면 무시한다 | 해당 없음 |
| 조건 | 적중이 64자 넘는 `[A-Za-z0-9+/]+={0,2}` 덩어리 안에 있고, 그 덩어리가 엄격 해독(`base64.b64decode(..., validate=True)`)과 엄격 UTF-8 해독이 되며, 해독본을 같은 검사기(이름 부분 문자열·local-deny·mailmap 원주소·`deny_patterns`, 원 blob 경로의 accept 판정 포함)로 다시 본 결과가 0건 | 적중을 감싼 영숫자 토큰(`--suggest-rows`와 같은 규칙)이 검토 토큰 파일의 토큰과 대소문자까지 같고, 그 줄의 이름이 적중한 표 이름(대소문자 무시)이다. 토큰이 그 이름(하이픈 이름의 구분자 변형 포함, 대소문자 무시)과 같으면 빼지 않는다 |
| 빼지 않는 경우 | 해독본에 적중이 있으면 덩어리 안 이름 적중 전부와 해독본 적중을 원래 라벨(`blob/rename-table:N`, `blob/local-deny` 등)로 실패로 센다. 해독·UTF-8이 안 되면 덩어리 안 적중은 검토 토큰 판정으로 넘어간다. 단 전체 해독이 안 되면 앞 1~3자를 뗀 해독본(JSON `\n`의 `n`처럼 앞에 붙은 글자로 4자 경계가 어긋난 경우)도 같은 검사기로 보고, 적중이 있으면 덩어리 안 적중과 함께 실패로 센다 | 다른 이름의 적중은 같은 토큰 안에 있어도 빼지 않는다 |
| 재귀 | 한 단계만(해독본 안의 base64는 다시 풀지 않는다) | 없음 |
| 알려진 한계 | 줄바꿈한 base64(줄마다 64자 이하이거나 줄 경계에 걸친 이름)는 해독 대상이 아니다 | - |
| 출력 | `blob/encoded-checked:<표 행 번호> <건수>` | `<표면>/reviewed:<표 행 번호> <건수>` |

- 적중별 면제는 이름 정규식이 찾은 위치로 판정한다. 위치 없이 `lower()` 대체 셈으로만 잡힌 적중은 면제하지 않는다.
- 면제 건수는 실패 건수와 따로 세어 독립 유출 검사 결과 끝에 `; 면제 ...`로 덧붙인다. 성공 판정(0건)에는 들어가지 않고, 내용(토큰·해독본)은 출력하지 않는다.
- 검토 토큰 파일의 키는 표 행 번호가 아니라 이름이다(행 번호는 표 편집에 밀린다). 파일 형식과 검사(lint)는 "사용법" 1.
- 공개판에 실리는 이 문서에는 오탐의 경로·파일별 건수·이름 길이·면제 건수를 적지 않는다(이름 표 행을 좁히는 단서가 된다). 실데이터 dry-run 수치는 리포 밖 보고에만 둔다.

사설 IPv4 리터럴 검사(`private-ipv4`, #456):
운영 LAN 주소가 공개 문서에 남지 않게, 독립 유출 검사와 별도의 검사 줄로 사설 IPv4 리터럴을 센다.

| | 사설 IPv4 리터럴 검사 |
|---|---|
| 검사 줄 | `[PASS/FAIL] private-ipv4: <총 건수> (blob N, message N, path N)`. 0건이어야 통과한다. 이름 유출 건수와 섞지 않는다 |
| 표면 | 거른 리포 전 이력의 모든 blob, 파일 경로, 커밋·태그 메시지(도구가 더한 `Exported-From` 줄은 뺀다). 작성자 표면은 보지 않는다 |
| 대상 | 점으로 이은 숫자 덩어리(앞뒤가 숫자가 아님) 안의 연속한 네 조각(조각마다 1~3자리, 0~255) 중 RFC1918(10/8, 172.16/12, 192.168/16)과 100.64/10. 숫자·점 뒤에 붙은 주소(`host-1.<주소>.nip.io`)도 센다(fail-closed) |
| 세지 않는 것 | loopback(127/8), 문서 예시 대역(192.0.2/24, 198.51.100/24, 203.0.113/24), 그 밖의 공인 주소. 다섯 조각 이상 버전 문자열은 그 안의 사설 대역 네 조각만 센다(오탐은 아래 면제 줄로). IPv6와 base64 해독본은 대상이 아니다 |
| 면제 | 검토 토큰 파일의 `<IPv4>\tprivate-ipv4` 줄(둘째 칸 대소문자 무시). 적중한 리터럴 전체가 첫째 칸과 정확히 같을 때만 blob·경로·메시지 모든 표면에서 뺀다. 면제 건수는 `; 면제 blob N, ...`로 따로 낸다 |
| 출력 | 표면별 건수와 적중 blob 경로(`  private-ipv4 적중 blob 경로: <경로> <건수>`)뿐이다. 경로 속 표 이름은 `<이름>`, 사설 주소는 `<사설IPv4>`로 가린다. 주소는 출력하지 않는다 |

- 검토 토큰 파일 계약 확장(`export.load_reviewed_tokens`): 둘째 칸이 `private-ipv4`인 줄은 이름 줄이 아니다. "표에 없는 이름"·"이름을 포함하지 않는 토큰" 검사에서 빠지고, 첫째 칸이 사설 IPv4 리터럴 하나가 아니면 `사설 IPv4 리터럴이 아닌 토큰: <줄 번호>`로 exit 2다. 쓰이지 않은 줄 수에는 함께 센다.
- fail-closed: 다섯 조각 이상 버전 문자열 안의 사설 대역 네 조각도 적중한다. 빼려면 검토 토큰 IPv4 줄에 그 네 조각을 적는다(같은 주소가 단독으로 나온 곳도 함께 빠진다).
- 면제는 패키지 버전 문자열·테스트 리터럴 같은 오탐용이다. 실제 주소는 HEAD에서 문서 예시 대역으로 바꾸고, 과거 판본의 실제 주소는 이름 표 치환으로 없앤다.
- 테스트·문서는 사설 대역 주소를 점 리터럴로 쓰지 않는다(테스트는 실행 때 조각을 이어 만든다). 이 리포 자신이 공개판 검사에 걸리지 않게 하는 규칙이다.

공개 식별자(`public_words`, 매니페스트 선택 키, 문자열 목록, 없으면 빈 목록):
치환하지 않고 표 누락 검사·독립 유출 검사에서 뺀다.
비교는 대소문자 무시이고, 표 행은 이름이 이 단어와 같을 때만 빠진다(이 단어를 부분으로 품은 다른 이름은 그대로 치환·검사한다).
독립 유출 검사는 이 단어의 온전한 출현(대소문자 무시, 앞뒤가 `[A-Za-z0-9]`가 아님) 범위 안에 완전히 든 이름 적중을 실패가 아니라 면제 `public-word`(표 행 번호 없음)로 따로 센다(#468).
출현을 걸친 이름 적중(구분자 변형 포함), 영숫자가 붙은 꼴, 이름 밖 검사기(local-deny·mailmap 원주소·`deny_patterns`) 적중, base64 해독본 적중은 그대로 실패다.
주의: `public_words`에 값을 더하면 그 값 안의 이름 적중이 면제된다. 값 추가는 공개 리포 소유자 계정처럼 이미 공개된 토큰만 한다.
리포 매니페스트 값은 `aside`, `github`, `orca`, `personal`, `hindsight-crew`, `Bkankim`(공개 리포 소유자 GitHub 계정명, 공개 리포 URL과 작성자 noreply 주소에 이미 있다)이다.
공개 MCP 서버 이름 `memory-vault`의 하이픈 뒤 단어는 홀로 공개 단어로 두지 않는다(#468).
단어 전체 일치라 그 단어로 된 `project:`·`topic:` 태그까지 치환·검사에서 빠지기 때문이다.
공개 MCP 서버 이름 `memory-vault`(`mcp__memory-vault__` 꼴 포함, `bench/corpus.py`의 외부 기록 호환)는 대신 `public_literals`로 보호한다.
그 단어가 홀로 나오면 이름 표에 행이 있을 때 치환되고, 없으면 표 누락 검사에 걸린다(이 단락은 그 단어를 홀로 적지 않는다. 적으면 공개판에서 데모로 바뀌어 문장이 어긋난다).

## 사용법

도구: `scripts/public-export/export.py`(#426).
운영 리포의 `--source-ref` 하나만 새 임시 리포로 가져와 `git filter-repo`로 거르고, 아래 검사가 전부 통과해야 성공한다.
기본은 dry-run이다.
운영 리포의 작업 트리·브랜치·이력은 바꾸지 않고, `--apply`만 운영 리포에 `export/NNN` 태그를 만들어 `origin`에 push한다.
`--apply`는 스냅샷 공개 절차에서는 쓰지 않음(아래 5).
공개판 push는 도구가 하지 않는다(아래 4).

공개 절차는 단일 커밋 스냅샷이다(ADR 0002 2026-11-10 개정).
도구는 고치지 않고 그대로 쓴다.
운영 리포의 별도 클론에서 원본 HEAD 트리로 고아 커밋 하나를 만들고,
그 커밋을 `--source-ref`·`--split-rev`로 준다(아래 2).
순서: 별도 클론 -> 원본 HEAD로 detach -> 고아 커밋 -> `refs/remotes/origin/snap` -> dry-run 2회와 공개 HEAD sha 비교 -> 후보 pytest -> 독립 리뷰 2명 -> 공개 리포 갈아 끼우기 -> `export/NNN` 주석 태그 -> 추적 이슈 댓글.

필요한 외부 명령: `git filter-repo` 2.47 이상(`--file-info-callback`이 있어야 한다), `gitleaks`.
`git filter-repo --version`은 버전 번호가 아니라 소스 해시를 찍으므로, 도구는 `--file-info-callback` 옵션 유무로 2.47 이상인지 판정한다.

### 1. 로컬 입력 파일 준비

다섯 파일 모두 리포 밖에 두고(도구가 리포 안 경로를 거부한다), 권한은 600, 내용은 출력·커밋·이슈에 옮기지 않는다.

| 인자 | 기본 경로 | 형식 | 비고 |
|---|---|---|---|
| `--local-deny` | `~/.config/hindsight-crew/public-export/local-deny.txt` | 한 줄에 정규식 하나, `#` 주석 | #423 `check_manifest.py --local-deny`와 같은 파일. 검출 전용(치환하지 않는다). 대소문자 무시(`re.I`)로 적용한다 |
| `--rename-table` | `~/.config/hindsight-crew/public-export/rename-table.tsv`(인자 기본값) | 한 줄 `<원래>\t<데모>`, `#` 주석 | #423에서 만든 표. 치환 규칙은 "공개 판정표" 7절(대소문자 무시, 앞뒤가 `[A-Za-z0-9]`가 아닐 때, 긴 이름 먼저, 꼴 보존) |
| `--mailmap` | `~/.config/hindsight-crew/public-export/mailmap` | git mailmap | 운영 주소 -> `<id>+Bkankim@users.noreply.github.com`. 새로 만든다 |
| `--reviewed-tokens` | `~/.config/hindsight-crew/public-export/reviewed-tokens.tsv`(인자 기본값) | 한 줄 `<토큰>\t<표 왼쪽 이름>`, `#` 주석 | 독립 유출 검사의 검토 토큰("공개 판정표" 8절). 표 소유자가 오탐으로 판정한 토큰만 둔다. 사설 IPv4 오탐 면제 줄은 `<IPv4>\tprivate-ipv4` |
| `--local-rules` | `~/.config/hindsight-crew/public-export/local-rules.json`(인자 기본값) | JSON 객체 `{"deny_patterns": [...], "message_rewrites": [...], "accept": [...]}` | 일반어 금칙·커밋 메시지 치환·경로별 허용의 값("공개 판정표" 2절 계약). 매니페스트 값 뒤에 합친다. 검사기 `check_manifest.py --local-rules`와 같은 파일 |

`--local-deny`·`--mailmap`은 필수 인자이고, `--rename-table`은 파일이 없으면 실패한다.
`--reviewed-tokens`는 인자를 주지 않았고 기본 경로에 파일이 없으면 빈 목록이다. 명시한 경로에 파일이 없으면 실패한다.
기본 경로가 깨진 심링크(대상이 옮겨짐)여도 빈 목록으로 넘어가지 않고 실패한다(입력 누락이 오탐 회귀처럼 보이지 않게).
`--local-rules`도 같다.
인자를 주지 않았고 기본 경로에 파일이 없으면 빈 규칙(매니페스트 값만)이고,
명시한 경로가 없거나 기본 경로가 깨진 심링크면 exit 2다.

로컬 규칙 파일 준비:

```sh
C=~/.config/hindsight-crew/public-export
test -f $C/local-rules.json || (umask 077; printf '{"deny_patterns": [], "message_rewrites": [], "accept": []}\n' > $C/local-rules.json)
chmod 600 $C/local-rules.json
python3 scripts/public-export/check_manifest.py --local-rules $C/local-rules.json; echo "exit $?"   # 형식 확인
```

- 키는 `deny_patterns`·`message_rewrites`·`accept` 셋만 쓰고,
  항목 모양·합치기·형식 오류는 "공개 판정표" 2절 계약을 따른다.
- 형식 확인의 성공 기준은 `로컬 규칙: deny_patterns N, message_rewrites N, accept N` 줄이 나오고 exit가 2가 아닌 것이다(형식 오류는 exit 2).
  검사기는 이 줄 뒤에 HEAD 검사 줄과 요약도 내고,
  HEAD 검사가 실패하면 exit 1이다(그때는 로컬 규칙 형식이 아니라 HEAD를 본다).
- 규칙 값은 출력하지 않는다.
  값은 커밋·이슈에도 옮기지 않는다.

도구는 거르기 전에 이름 표를 검사(lint)하고, 어기면 exit 2로 멈춘다.
실패 메시지는 표의 줄 번호만 낸다.

- 데모가 빈 행.
- `public_words`와 같은 이름(대소문자 무시)의 행은 빼고 본다.
- 소문자화한 왼쪽 이름이 같은 행끼리 데모를 소문자화한 값이 다르다(예: `Foo\tBar` + `foo\tbar`는 허용, `Foo\tA` + `foo\tB`는 실패).
- 어떤 행의 데모를 소문자화한 문자열에 (자기 행 포함) 어느 왼쪽 이름의 소문자 부분 문자열이라도 들어 있다. 자기 매핑 행(`값\t값`)과 데모가 다른 행의 왼쪽과 같은 행도 여기에 걸린다. 공개해도 되는 값은 자기 매핑 대신 매니페스트 `public_words`에 넣는다.

이름 표 주의:
이름 표에는 일반 단어와 같은 이름의 행이 있을 수 있다.
그 단어를 일반 용법으로 쓴 곳도 치환되거나,
치환 밖 표면이면 독립 유출 검사에서 실패한다.
그래서 행을 더하거나 바꾼 뒤에는 후보 diff에서 문서 뜻과 식별자가 깨지지 않았는지 확인하고,
`check_manifest.py --rename-table <리포 밖 표>`의 `rename-table HEAD: inside N` 수가 어떻게 바뀌었는지 본다.

검토 토큰 파일도 거르기 전에 검사(lint)하고, 어기면 exit 2로 멈춘다.
실패 메시지는 파일의 줄 번호만 낸다.

- 형식 오류(탭으로 나눈 칸이 둘이 아니거나 빈 칸).
- 줄의 이름이 이름 표(`public_words` 행을 뺀 뒤)에 없다(대소문자 무시).
- 토큰이 그 이름을 품지 않는다(대소문자 무시, 하이픈 이름은 `_`·하이픈 없는 변형도 인정).
- 토큰이 64자를 넘는다.

한 번도 면제에 쓰이지 않은 줄은 실패가 아니고 `검토 토큰: 줄 N, 쓰이지 않은 줄 M`으로 건수만 낸다.
토큰이 이름과 같은 줄(대소문자 무시)은 검사를 통과하지만 면제에 쓰이지 않는다.

표 보강 제안(`--suggest-rows <리포 밖 파일>`, 선택):
독립 유출 검사에 남은 이름 적중을 감싼 영숫자 토큰마다 `<토큰>\t<이름 부분만 데모로 바꾼 토큰>` 행을 그 파일에 쓴다.
이름 길이가 4 이하면 셋째 칸에 `short`를 붙인다(일반 단어 속 부분 일치일 수 있어 따로 검토한다).
표 이름과 대소문자만 다른 토큰은 표가 이미 덮으므로 빼고, 64자보다 긴 토큰은 건수만 센다.
파일은 원본 리포 작업 트리와 `--out` 밖, 있는 디렉터리 안이어야 하고, 입력 파일(`--rename-table`·`--local-deny`·`--mailmap`·`--reviewed-tokens`·`--manifest`·`--gitleaks-config`)과 같은 파일이면 안 된다(어기면 거르기 전에 exit 2).
검토 토큰으로 빠진 적중과 해독 재검사로 빠진 적중은 제안하지 않는다.
있으면 덮어쓰며, 내용을 쓰기 전에 권한을 600으로 맞춘다.
도구는 내용을 출력하지 않고 `제안 행: N`만 낸다.
행을 표에 더할지는 표 소유자가 정한다.

로컬 입력 백업:
다섯 파일은 리포 밖에만 있어서 git 이력으로 되살릴 수 없다.
장비를 옮기거나 디스크를 잃기 전에 암호화 사본을 따로 둔다.
아래 경로·수신자는 자리표시자다.
실제 보관 위치와 수신자 키는 리포·이슈에 적지 않는다.

1. 백업 대상: 위 표의 다섯 파일(`local-deny.txt`·`rename-table.tsv`·`mailmap`·`reviewed-tokens.tsv`·`local-rules.json`)이다.
   `$C`에 없는 파일은 인자로 넘기는 실제 위치에서 따로 묶는다.
   만들지 않은 파일(쓰지 않는 `reviewed-tokens.tsv`·`local-rules.json`)만 건너뛰고,
   빠진 파일 이름을 적어 둔다.
2. 권한: 묶기 전에 원본 디렉터리는 700, 파일은 600인지 확인한다(`stat -f '%Lp %N' "$C" "$C"/*`, Linux는 `stat -c '%a %n' "$C" "$C"/*`).
   묶음과 암호문도 `umask 077` 아래에서 만든다.
3. 암호화: 평문 묶음을 디스크에 남기지 않고 파이프로 바로 암호화한다.
   예(`age` 공개키 방식):
   `(umask 077; tar -C "$C" -cf - <있는 파일...> | age -r <수신자 공개키> -o <보관 위치>/public-export-inputs.tar.age)`
   비밀키는 암호문과 다른 곳에 둔다.
   `gpg --encrypt`처럼 공개키로 암호화하는 다른 도구도 된다.
4. 복원 확인: 백업을 만든 직후 새 임시 디렉터리에 풀어 원본과 비교한다.
   그 뒤 파일을 바꿀 때마다 3단계 암호화를 다시 한 뒤,
   새 암호문을 같은 방법으로 풀어 비교한다.
   `T=$(mktemp -d)`, `age -d -i <비밀키 파일> <암호문> | tar -C "$T" -xf -`, `diff -rq "$C" "$T"`(출력 없음이 기대값, `$C`에 다섯 파일 밖의 파일이 있으면 그 파일의 `Only in` 줄만 나온다), `stat`으로 풀린 파일 권한 600 확인.
   비교는 내용이 출력되지 않게 `-q`로 하고,
   끝나면 `test -n "$T" && rm -rf "$T"`로 그 디렉터리만 지운다.
   복원 뒤에는 위 형식 확인(`check_manifest.py --local-rules`)과 dry-run이 거르기 전 검사(lint)를 통과하는지도 본다.

### 2. 스냅샷 커밋과 dry-run

운영 체크아웃은 건드리지 않고, 운영 리포의 별도 클론에서 만든다.
값 `<...>`는 자리표시자다.

```sh
C=~/.config/hindsight-crew/public-export
SNAP=$(mktemp -d)/src                                          # 운영 리포의 별도 클론
git clone <운영 리포 원격> "$SNAP"
git -C "$SNAP" fetch origin 'refs/tags/export/*:refs/tags/export/*'
git -C "$SNAP" checkout --detach origin/main                   # 원본 HEAD로 detach
SRC=$(git -C "$SNAP" rev-parse HEAD)                           # 원본 운영 HEAD sha
TRAILERS="Source-HEAD: $SRC"
# 외부 기여를 싣는 공개면 같은 문단에 줄을 더한다:
# TRAILERS="$TRAILERS"$'\n'"Co-authored-by: <기여자 이름> <기여자 주소>"
mk_orphan() {                                                  # 원본 HEAD 값으로 고아 커밋을 만들고 sha를 찍는다
  (cd "$SNAP" && \
    GIT_AUTHOR_NAME="$(git log -1 --format=%an HEAD)" \
    GIT_AUTHOR_EMAIL="$(git log -1 --format=%ae HEAD)" \
    GIT_AUTHOR_DATE="$(git log -1 --format=%ad --date=raw HEAD)" \
    GIT_COMMITTER_NAME="$(git log -1 --format=%cn HEAD)" \
    GIT_COMMITTER_EMAIL="$(git log -1 --format=%ce HEAD)" \
    GIT_COMMITTER_DATE="$(git log -1 --format=%cd --date=raw HEAD)" \
    git commit-tree --no-gpg-sign "HEAD^{tree}" -m "Public snapshot" -m "$TRAILERS")
}
ORPHAN=$(mk_orphan)
test -n "$ORPHAN" && test "$ORPHAN" = "$(mk_orphan)" && echo "같은 고아 sha: $ORPHAN" || echo "고아 sha 불일치 - 멈춘다"
git -C "$SNAP" update-ref refs/remotes/origin/snap "$ORPHAN"   # 도구에 줄 ref

OUT1=$(mktemp -d)/export
OUT2=$(mktemp -d)/export
for OUT in "$OUT1" "$OUT2"; do                                 # 같은 입력 같은 sha 확인용 2회
  (cd "$SNAP" && uv run --no-project --python 3.14 python -I scripts/public-export/export.py \
    --source-ref origin/snap --split-rev "$ORPHAN" --out "$OUT" \
    --local-deny $C/local-deny.txt --rename-table $C/rename-table.tsv --mailmap $C/mailmap \
    --reviewed-tokens $C/reviewed-tokens.tsv --local-rules $C/local-rules.json \
    --suggest-rows "$(mktemp -d)/suggest.tsv")
  rc=$?
  echo "dry-run $OUT exit $rc"
  [ "$rc" -eq 0 ] || break                                     # 0이 아니면 멈추고 출력을 본다
done
PUB=$(git -C "$OUT1/repo" rev-parse main)                      # 공개 HEAD sha
test "$PUB" = "$(git -C "$OUT2/repo" rev-parse main)" && echo "같은 sha: $PUB"
```

- 도구는 클론 안에서(`--repo` 기본 `.`) 원본 HEAD의 `scripts/public-export/`판으로 돈다.
  도구가 읽는 `export/*` 태그 번호와 `--split-rev`도 클론 기준이다.
- 고아 커밋의 작성자·커미터·날짜는 원본 HEAD 커밋 값이라 같은 원본 HEAD면 같은 고아 sha가 나온다.
  제목은 고정 리터럴 `Public snapshot`이다(바꾸면 sha가 바뀐다).
  `--no-gpg-sign`은 서명 설정이 sha를 바꾸지 않게 한다.
  `mk_orphan`을 두 번 불러 같은 sha인지 먼저 보고, 다르면 dry-run으로 넘어가지 않는다.
- 메시지의 `Source-HEAD: <원본 운영 HEAD sha>`가 공개판에 남는 원본 표시다.
  도구가 더하는 `Exported-From:`은 고아 커밋 sha다.
- `Source-HEAD`의 40자 sha는 독립 유출 검사의 message 표면에서 빠지지 않는다.
  경계 없는 부분 문자열 비교라 짧은 이름 행과 우연히 맞아 오탐이 날 수 있고,
  그때는 표 소유자가 검토 토큰 파일에 그 토큰 줄을 더해 처리한다("공개 판정표" 8절).
- 외부 기여를 싣는 공개는 `TRAILERS`에 `Co-authored-by:` 줄을 더한다.
  줄 목록과 순서가 같으면 고아 sha도 같다.
- 반복문은 회차마다 exit를 찍고, 0이 아니면 다음 회차로 가지 않는다.
- `--prev-public`은 주지 않는다. 스냅샷 공개 절차에서는 쓰지 않음(옵션 설명은 아래).
- `--suggest-rows`는 선택이다(표 보강 제안, 위 1).
- `--manifest`를 주지 않으면 `--source-ref`의 `scripts/public-export/manifest.json`을 쓴다. `--gitleaks-config`도 같은 방식으로 `--source-ref`의 `.gitleaks.toml`이 기본이다.
- 옵션 `--prev-public <공개 리포 로컬 클론>`: 그 HEAD가 새 HEAD의 조상인지 본다(조상 검사, 주지 않으면 SKIP).
  이력을 잇는 내보내기용 옵션이고, 스냅샷 공개 절차에서는 쓰지 않음(매 공개가 갈아 끼우기라 조상 관계가 없다).
- `--out`은 운영 리포 작업 트리 밖의, 비어 있거나 없는 디렉터리여야 한다. 도구는 그 안에 `repo/`(거른 리포), `comment.md`(추적 이슈 댓글 본문), `result.json`(검사 결과)을 남기고 지우지 않는다.
- 거르기는 임시 위치에서 하고 성공한 뒤에만 `repo/`로 옮긴다. 거르기 전에 실패하면 `--out`에 원본 이력이 남지 않는다.
- exit: 성공 0, 검사 실패 1, 사용·도구 오류 2.

걸러 내기 규칙:

- include 밖 경로와 `exclude_history` 경로를 이력 전체에서 뺀다(경로 해석은 `check_manifest.path_matches`).
- `history_rewrite_paths` 경로의 blob과 경로 이름, 그리고 커밋 메시지(trailer 포함)에 이름 표 -> `message_rewrites` 순으로 치환한다. 이름 규칙은 "공개 판정표" 7절이다.
- 작성자·커미터는 `--mailmap`으로 바꾸고, 커밋 메시지 trailer 블록에 `Exported-From: <원본 sha>`를 더한다. 기존 trailer(`Co-authored-by` 등)는 그대로 둔다.
  스냅샷 절차에서 원본 sha는 고아 커밋 sha이고, 원본 운영 HEAD sha는 그 앞의 `Source-HEAD:` 줄이다.
- 빈 커밋은 `--prune-empty auto --prune-degenerate auto`로 고정한다.
- 현재 시각·난수를 쓰지 않으므로 같은 입력·규칙·도구 버전이면 같은 공개 HEAD sha가 나온다.
- 공개판 이력 커밋의 테스트 통과는 보장하지 않는다(HEAD만 보장).
  스냅샷 절차의 공개판은 커밋 하나라 HEAD가 전부다.

검사(전부 통과해야 성공, 출력은 통과/실패와 건수·표 행 번호·가린 경로만):

| 검사 | 통과 조건 | 실패하면 |
|---|---|---|
| 주소 집합 | 작성자·커미터 주소가 mailmap 대상 주소, `noreply@github.com`(웹 머지), `*@users.noreply.github.com`뿐 | mailmap에 빠진 주소를 더한다 |
| 독립 유출 검사 | 거른 리포 전 이력의 모든 blob·커밋/태그 메시지(trailer 포함)·파일 경로·작성자/커미터/태거 이름·주소에 local-deny(대소문자 무시), 이름 표 왼쪽 열(경계 없는 부분 문자열, 대소문자 무시, "공개 판정표" 7절), mailmap 원래 주소, `deny_patterns` 0건. 작성자 표면에서는 mailmap 대상 이름·주소와 정확히 같은 값만 뺀다. blob의 64자 넘는 base64 덩어리는 원문 이름 적중이 없어도 해독본을 같은 검사기로 다시 보고, 해독본 적중은 실패로 센다(#448). blob의 base64 해독 재검사와 blob·메시지의 검토 토큰으로 빠진 적중은 실패에 넣지 않고 `; 면제 blob/encoded-checked:<표 줄 번호> <건수>, <표면>/reviewed:<표 줄 번호> <건수>`로 따로 낸다("공개 판정표" 8절). `handling: export-rewrite` accept는 적용하지 않는다. 이름 적중은 `<표면>/rename-table:<표 줄 번호> <건수>`로 낸다 | 경로 범위 문제면 출력된 blob 경로를 `history_rewrite_paths`에 더한다(매니페스트 수정은 PR로). 영숫자에 붙은 이름이면 표 소유자가 표에 행을 더한다(`--suggest-rows`). 일반 단어 속 우연 일치로 판정되면 표 소유자가 검토 토큰 파일에 줄을 더한다. base64 해독본에서 나온 적중은 검토 토큰·`--suggest-rows`로 풀리지 않으니, 그 blob 경로를 `history_rewrite_paths`에 더하거나 파일을 고친다 |
| 표 누락 검사 | 원본 이력의 옛 개인 fixture(`tests/fixtures/policies/b?/`)와 분리 커밋(`--split-rev`, 기본 252320e) 이전 정책 파일 판본의 뱅크 이름·별칭·`project` 어휘·트리거 id가, 데모 fixture에 그대로 있는 값과 매니페스트 `public_words`를 빼고 전부 이름 표 왼쪽 열에 있다(대소문자 무시 비교). JSON으로 읽지 못한 대상 판본도 0이어야 한다. 스냅샷 절차는 `--source-ref`·`--split-rev`에 고아 커밋을 주므로 대상은 그 커밋(HEAD 트리)에 있는 옛 fixture뿐이다(고아 커밋에 부모가 없어 분리 전 정책 판본은 대상이 아니다) | 표 소유자가 이름 표에 행을 더한다. 공개해도 되는 값은 매니페스트 `public_words`에 넣는다(자기 매핑 행은 표 검사에서 실패한다). 도구는 개수만 낸다 |
| 제외 경로 | 거른 이력에 `exclude_history` 경로·include 밖 경로 0건 | 도구 결함. 이슈로 올린다 |
| gitleaks | `gitleaks git` 거른 리포 전 이력 0건(`--redact`) | `.gitleaks.toml` 허용 판정은 "공개 판정표" 5절 기준으로 PR에서 |
| Exported-From | 모든 커밋에 하나씩 있고 filter-repo commit-map의 원본 sha와 같다(스냅샷 절차에서는 고아 커밋 sha) | 도구 결함 |
| private-ipv4 | 거른 리포 전 이력의 blob·경로·커밋/태그 메시지에 사설 IPv4 리터럴(RFC1918, 100.64/10) 0건(`[PASS/FAIL] private-ipv4: <총 건수> (blob N, message N, path N)`) | 실제 주소면 이름 표에 행을 더해 치환한다(HEAD의 실제 주소는 운영판에서 문서 예시 대역으로 고친다). 버전 문자열·테스트 리터럴 같은 오탐이면 표 소유자가 검토 토큰 파일에 `<IPv4>\tprivate-ipv4` 줄을 더한다. 근거는 "공개 판정표" 8절 |
| 조상 검사 | `--prev-public` HEAD가 새 HEAD의 조상. 스냅샷 공개 절차에서는 쓰지 않음(`--prev-public` 없이 SKIP) | 이력을 잇는 내보내기에서만 뜻이 있다. 스냅샷은 아래 4의 갈아 끼우기 |

### 3. 관문 확인

공개판 관리 주체(에이전트)가 기계 관문 4개와 독립 LLM 리뷰 관문을 관문마다 아래 근거로 확인한다.
스냅샷 절차에서 기계 관문 4개(독립 유출 0, 후보 안 전체 pytest 실패 0, 같은 입력 같은 sha, gitleaks 0)는 고아 커밋 하나, 곧 원본 HEAD 트리를 본다.
도구의 나머지 검사(주소 집합, private-ipv4, 표 누락, 제외 경로, Exported-From, 구조 치환)도 전부 통과해야 하고, 조상 검사는 SKIP이다.
- 독립 유출 0, gitleaks 0: dry-run 출력의 검사 결과(`comment.md`에도 같은 내용이 실린다).
- 후보 안 전체 pytest 실패 0: `$OUT1/repo`를 리포 밖 새 임시 디렉터리에 `git clone`하고,
  그 안에서 `HC_*_LOCAL` 환경변수 없이 아래 명령으로 돌린 요약 줄(실패 0, 수집 오류 0).

  ```sh
  CAND=$(mktemp -d)/candidate
  git clone "$OUT1/repo" "$CAND"
  (cd "$CAND" && uv run --no-project --python 3.11 --with-requirements gateway/requirements.txt --with-requirements requirements-dev.txt python -m pytest)
  ```

  서브셸로 돌려 셸 작업 디렉터리를 바꾸지 않는다(뒤의 명령은 `$SNAP`·`$OUT1` 절대 경로로 돈다).
  `pytest.ini`의 `addopts`가 이미 `-q`라서 명령에 `-q`를 더하지 않는다(더하면 요약 줄이 사라진다).
- 같은 입력 같은 sha: 위 2에서 새 `--out`으로 2회 돌린 dry-run의 공개 HEAD 비교(`같은 sha` 줄).
- 독립 LLM 리뷰 관문: 아래 표대로 리뷰어 2명이 모두 통과로 판정한다(ADR 0002 개정).

| 항목 | 내용 |
|---|---|
| 리뷰어 | 독립 LLM 리뷰어 2명. 서로 다른 모델이고, 서로의 결과를 보지 않는다 |
| 범위 | 매 공개 전체 범위: 공개판 전체(스냅샷 트리의 모든 경로·blob과 커밋 메시지). 직전 공개 이후 변경분으로 한정하던 범위는 2026-11-10 대체됨 |
| 보는 것 | 기계 관문이 보지 못하는 사적 맥락: 이름 없이 근무·생활 맥락을 담은 자유 문장, 운영 인프라를 드러내는 문서, 공개 독자에게 맞지 않는 운영 전용 문서 |
| 통과 조건 | 두 리뷰 모두 차단·높음 등급 사적 정보 0건 |
| 지적 기록 | 값을 가린 꼴과 위치(후보 sha:경로:줄)로 리포 밖에 남긴다. 값 원문은 이슈·커밋·PR에 옮기지 않는다 |
| 해결 뒤 | 운영판에서 고치고(또는 매니페스트·이름 표를 보강하고) 새 후보를 만든 뒤 같은 범위를 두 리뷰어가 다시 리뷰한다 |
| 실행 주체 | 공개판 관리 주체(에이전트) |

관문을 모두 통과하면 공개판 관리 주체가 다음 단계를 실행하고 결과를 운영자에게 보고한다.
`$OUT1/repo`는 아직 어디에도 push되지 않았다.

### 4. 공개 리포 갈아 끼우기

매 공개는 공개 리포 main을 새 스냅샷으로 갈아 끼운다.
조상 검사와 fast-forward push는 쓰지 않는다(스냅샷끼리는 이력이 이어지지 않는다).
갈아 끼우는 길은 둘이다.
- 삭제·재생성: 공개 리포를 지우고 같은 이름으로 다시 만든 뒤 아래 push를 한다.
  옛 이력을 지워야 할 때 쓴다.
  이력 공개판에서 첫 스냅샷으로 바꾸는 전환은 이 길이다(관리 주체·운영자 선택).
- force push: 아래 `--force-with-lease` push로 main만 바꾼다.
  옛 공개 커밋이 GitHub에 sha로 남아 열릴 수 있으므로,
  지울 이력이 없는 스냅샷 다음 갱신에 쓴다.
공개 원격 이름은 운영 원격(`origin`)과 다르게 `public`으로 둔다.
push 대상은 거른 리포의 `main` 하나뿐이다(태그·다른 ref는 없다).

```sh
git -C "$OUT1/repo" remote add public git@github.com:<공개 리포 소유자>/<공개 리포>.git
PREV=$(git -C "$OUT1/repo" ls-remote public refs/heads/main | cut -f1)   # 직전 공개 HEAD(첫 공개면 빈 값)
git -C "$OUT1/repo" push --force-with-lease=main:"$PREV" public main      # 갈아 끼우기
```

- `--force-with-lease`는 확인한 직전 공개 HEAD일 때만 덮는다. 값이 비면 main이 없을 때만 push된다(삭제·재생성 직후).
- 삭제·재생성하면 공개 리포의 설정과 이슈가 함께 사라지므로 아래 설정을 다시 건다.
- 스냅샷 공개 절차에는 로컬 공개 클론이 필요 없다(`--prev-public`을 쓰지 않는다).
  확인용 클론을 두었다면 갈아 끼운 뒤 새로 클론한다(옛 클론의 이력은 새 HEAD와 이어지지 않는다).

공개 리포 설정(공개판 관리 주체가 첫 공개 전에 건다):

- 공개 리포 main은 브랜치 보호를 건다.
  PR 머지를 막고,
  force push는 공개판 관리 주체만 할 수 있게 한다.
  근거: 공개판은 내보내기로만 바뀐다.
- **외부 PR은 공개 리포에서 머지하지 않는다.**
  머지한 변경은 운영판에 없으므로 다음 공개의 갈아 끼우기로 사라진다.
  외부 기여는 운영판에서 같은 변경으로 다시 만들어 다음 내보내기로 싣고,
  공개 PR은 반영한 공개판 스냅샷 sha(또는 `export/NNN`)를 알리는 댓글과 함께 닫는다(ADR 0002 Consequences).
  기여자 표기는 고아 커밋 메시지의 `Co-authored-by:` 줄이다(위 2의 `TRAILERS`).

### 5. `export/NNN` 태그(직접)

갈아 끼운 뒤 공개판 관리 주체가 원본 운영 HEAD sha에 주석 태그를 직접 만들어 `origin`에 push한다.
번호는 기존 최대 + 1, 세 자리다.

```sh
git -C "$SNAP" tag -l 'export/*' | sort | tail -1              # 기존 최대 번호 확인
N=export/<NNN>
git -C "$SNAP" tag -a "$N" "$SRC" -m "공개판 내보내기 $N" -m "스냅샷: $ORPHAN" -m "공개 HEAD: $PUB"
git -C "$SNAP" push origin "refs/tags/$N"
```

- 그 번호가 이미 있으면(로컬 또는 `git -C "$SNAP" ls-remote --tags origin "refs/tags/$N"`) 만들지 않는다.
- 옵션 `--apply`: 검사가 모두 통과했을 때 `--source-ref` 커밋에 다음 번호 주석 태그를 만들고 `origin`에만 push한다.
  스냅샷 공개 절차에서는 쓰지 않음(태그가 원본 운영 HEAD가 아닌 고아 커밋에 달린다).
  그래서 위처럼 원본 운영 HEAD sha에 직접 단다.

### 6. 추적 이슈 댓글

`--apply` 없이 dry-run `$OUT1/comment.md`(원본 범위, 공개 HEAD, 매니페스트 sha256, 검사 결과) 본문에 수동 태그 이름·`Source-HEAD`·공개 HEAD sha 한 줄을 더해 공개판 영구 추적 이슈에 댓글로 단다.
제목의 dry-run 표시(`(dry-run, 다음 번호 ...)`)와 '원본 범위' 줄(직전 태그..고아 커밋)은 스냅샷에서 뜻이 없다.

```sh
BODY=$(mktemp -d)/comment.md
(cat "$OUT1/comment.md"; printf -- '- 태그: %s, Source-HEAD: %s, 공개 HEAD: %s\n' "$N" "$SRC" "$PUB") > "$BODY"
gh issue comment <추적 이슈 번호> --body-file "$BODY"
```

### 7. 추적 이슈가 무거워지면

댓글이 많아 이슈가 무거워지면 새 추적 이슈를 연다.
새 이슈 본문 첫 줄에 이전 추적 이슈 번호와 마지막 `export/NNN`·공개 HEAD를 적고, 이전 이슈에는 새 이슈 링크를 마지막 댓글로 남긴 뒤 닫는다.
이후 댓글은 새 이슈에 단다.
`export/NNN` 번호는 이슈와 무관하게 운영 리포 태그에서 이어진다.
