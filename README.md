# Hindsight-Crew

**한국어** · [English](README.en.md)

[![ci](https://github.com/Bkankim/Hindsight-Crew-Public/actions/workflows/ci.yml/badge.svg)](https://github.com/Bkankim/Hindsight-Crew-Public/actions/workflows/ci.yml)

**팀 메모리를 권한과 규칙으로 통제하는 게이트웨이 서버.**

**지키고, 다듬고, 꺼내 준다.**<br>
AI 에이전트가 쓰는 기억([Hindsight](https://github.com/vectorize-io/hindsight) 메모리 서버)을 누가 읽고 쓸 수 있는지 통제하고, 어떤 형식으로 남길지 정리하고, 필요한 순간에 알맞게 꺼내 주는 관문이다.<br>
명령 하나로 스택을 세우고 스스로 검증하는 자체 호스팅 패키지로 묶었다.<br>
여러 사람이 각자의 `personal` 뱅크와 공유 `team` 뱅크를 함께 쓰는 구성을 목표로 설계했다.

## 무엇을 하나

| | 하는 일 |
|---|---|
| **지킨다** | 토큰마다 허용된 뱅크만 연다. 본문·헤더로 뱅크를 바꿔치기하는 시도도 기본으로 거부한다. 누가 어디에 접근했는지 감사 로그로 남긴다(토큰 원문은 남기지 않는다). |
| **다듬는다** | 뱅크별 태그 어휘·형식 규칙을 서버에서 강제한다. 고칠 수 있는 태그는 정규화하고, 고칠 수 없으면 위반한 규칙의 이름을 돌려준다. 성공처럼 보이는 저장 실패를 실패로 알리고, 모순되는 기록은 경고한다. |
| **꺼내 준다** | 미리 정해 둔 작업 지점에서 관련 기억을 자동으로 넣어 준다. 같은 함정이 반복되면 알림 초안을 만든다. recall 응답에서 내부 메타를 걷어내 토큰을 줄이고, "그때 한 말"과 "현재 상태 후보"를 구분해 표시한다(옵트인 `HC_RECALL_KIND_LABEL`, 기본 0). |

작업 상태 표(끝난 일과 열린 일 구분) 같은 기능은 실험 기능이라서 기능 플래그로 꺼 두었다.<br>
근거는 [`docs/benchmarks.md`](docs/benchmarks.md)에 있다.

**recall 종류 표시는 켜야 붙는다.**<br>
`HC_RECALL_KIND_LABEL`의 기본값은 0(꺼짐)이고,<br>
꺼져 있으면 recall 응답 바이트는 표시 기능이 없을 때와 같다.<br>
켜면 recall 결과 행마다 `gateway_kind_label` 필드가 추가된다.

원문 사실(world·experience)에는 "스냅샷(그 시점에 말한 것)",<br>
관찰(observation)에는 "통합됨(현재 상태 후보)"이라는 표시가 붙는다.<br>
종류를 알 수 없는 행은 그대로 두고,<br>
결과 id·순서·기존 필드는 바뀌지 않는다.<br>
켜는 값은 `1`·`true`·`yes`·`on`이다.

셸 변수로 값을 주고 게이트웨이만 다시 띄우는 방법이 기본이다(`HC_RECALL_KIND_LABEL=1 docker compose --env-file .env.local up -d gateway`).<br>
이 방법은 그 한 번만 기능을 켠다.<br>
다음에 변수 없이 `up`을 실행하면 기본값 0으로 돌아간다.<br>
`.env.local`에 `HC_RECALL_KIND_LABEL=1`을 적어도 켜진다.<br>
하지만 `.env.local`은 게이트웨이와 upstream hindsight가 함께 쓰는 `env_file`이라서,<br>
`up -d gateway`를 실행하면 upstream hindsight 컨테이너까지 다시 만들어진다([`docker-compose.yml`](docker-compose.yml) gateway `env_file` 주석).

끄려면 변수 없이 게이트웨이를 다시 띄운다(`docker compose --env-file .env.local up -d gateway`).<br>
`.env.local`에 적었다면 그 줄을 먼저 지운다.<br>
근거는 [`gateway/recall_label.py`](gateway/recall_label.py) 모듈 설명과 [`docker-compose.yml`](docker-compose.yml)의 `HC_RECALL_KIND_LABEL` 배선이다.

**자동 기억 주입은 따로 연결해야 켜진다.**<br>
설치기는 게이트웨이의 주입 엔드포인트(`POST /context-triggers/<bank>`)까지만 세운다.<br>
공용 트리거 표 [`policies/context-triggers.json`](policies/context-triggers.json)는 비어 있고,<br>
클라이언트 훅도 설치하지 않는다.

이 기능을 쓰려면 트리거 행을 `policies/local/context-triggers.json`에 적고,<br>
작업 문장을 그 엔드포인트로 보내는 훅(예: Claude Code PreToolUse 훅)을 직접 연결한다.<br>
트리거 행의 최소 예제는 [`tests/fixtures/policies/sample/context-triggers.json`](tests/fixtures/policies/sample/context-triggers.json)이고,<br>
요청·응답 계약은 [`gateway/context_triggers.py`](gateway/context_triggers.py) 모듈 설명과 아래 "데모" 절 끝의 예시에 있다.

## 누구에게 좋은가

Claude Code 같은 AI 에이전트를 쓰는 개인이 설치하면 다음과 같이 달라진다.

1. 에이전트가 지난 대화의 결정·함정·선호를 다음 세션에서 이어받는다.
2. 태그 규칙과 정규화 덕분에 몇 달 뒤에도 기억을 잘 찾을 수 있다.
3. 전에 막혔던 작업을 시작하면 관련 기억이 먼저 제시되고 같은 실수가 반복되면 알려 준다.
4. recall 응답의 내부 메타를 걷어내 토큰 비용을 줄인다(실측에서 응답 토큰의 59.5%만 실제 본문이었다. 근거는 [`gateway/recall_prune.py`](gateway/recall_prune.py) 모듈 설명).
5. 저장이 실패했는데 성공으로 보이는 경우를 잡아 준다.
6. 여러 에이전트가 하나의 기억을 나눠 쓰면서도, 개인 뱅크와 팀 뱅크는 권한으로 분리된다.
7. 자체 호스팅이라서 기억이 내 컴퓨터에 저장된다(LLM 처리는 본인의 Claude 토큰으로 한다).
8. 명령 하나로 설치하고, 격리가 실제로 작동하는지 10개 항목을 스스로 점검한다.

제약: macOS Apple Silicon 전용, 메모리 16GB 이상(권장 24GB), Claude Code 필요(아래 설치 절).

## 무엇을 해결하나

Hindsight의 기본 인증은 공유 키 하나뿐이다.<br>
그래서 "누가 어느 뱅크를 읽고 쓸 수 있는가"를 강제하는 계층이 없다.<br>
클라이언트를 Hindsight에 바로 연결하면 한 사람의 토큰으로 다른 사람의 뱅크를 읽을 수 있다.

Hindsight-Crew는 그 앞에 게이트웨이를 하나 둔다.

- **토큰별 뱅크 강제:** 토큰마다 허용된 뱅크만 열린다.<br>
  URL 경로뿐 아니라 요청 본문(`arguments.bank`)과 헤더(`X-Bank-Id`)로 뱅크를 바꿔치기하는 시도도 거부한다(기본 거부).
- **기록 형식 강제:** 뱅크마다 선언한 규칙(태그 어휘, 내용 머리표)을 서버에서 검사한다.<br>
  고칠 수 있는 태그는 정규화하고 고칠 수 없는 요청은 위반한 규칙의 이름과 함께 422로 돌려준다.
- **검증 가능한 격리:** `verify/verify-all.sh`가 격리·귀속·우회 시도 거부를 실제 스택을 대상으로 확인한다.<br>
  게이트 9개와 계약 드리프트 검사 1개를 합친 10개 항목이 모두 통과해야 exit 0으로 끝난다.

위협 모델은 정직하지만 호기심 있는(honest-but-curious) 사용자다.<br>
실수나 호기심으로 남의 뱅크에 접근하는 것을 막는다.<br>
호스트 접근 권한을 가진 사용자의 악의적인 우회는 범위 밖이다.<br>
자세한 범위와 남은 위험은 [`docs/THREAT-MODEL.md`](docs/THREAT-MODEL.md)에 있다.

## 설치 (4단계)

**지원 플랫폼은 macOS Apple Silicon(arm64)뿐이다.**<br>
리랭커가 호스트의 MPS 사이드카로 실행되고, 컨테이너용 Claude Code CLI를 linux-arm64로만 받기 때문이다.<br>
메모리는 최소 16GB(빠듯함), 권장 24GB 이상이고 디스크 여유 공간은 30GB를 권장한다.<br>
`./install`은 메모리가 16GB 미만이거나 디스크가 30GB 미만이면 경고만 하고 멈추지 않는다.

**1. 소스를 받는다.**

```sh
git clone https://github.com/Bkankim/Hindsight-Crew-Public
cd Hindsight-Crew-Public
```

git이 없으면 `xcode-select --install`을 먼저 실행한다.<br>
아래 명령은 모두 이 디렉터리에서 실행한다.

**2. 사전 도구를 설치하고 Docker VM을 띄운다.**

```sh
brew install colima docker docker-compose uv
xcode-select --install      # git
colima start --cpu 6 --memory 10
```

Claude Code도 필요하다.<br>
`./install`은 시작할 때 macOS arm64, colima, docker, `docker compose`, uv, git, claude의 설치 여부와 docker 데몬 접속 가능 여부를 점검한다.

**3. LLM 토큰을 환경 변수에 둔다.**

```sh
export CLAUDE_SETUP_TOKEN=...   # `claude setup-token` 출력값
```

설치기에는 변수 이름만 넘긴다.<br>
토큰 값은 명령줄에 올리지 않는다.

**4. 설치한다.**

```sh
./install --owner <slug> --dry-run                                   # 할 일만 출력, 아무것도 바꾸지 않음
./install --owner <slug> --claude-token-env CLAUDE_SETUP_TOKEN       # 뱅크 personal-<slug>, ACL identity <slug>
```

`./install`은 스택, 개인 뱅크와 토큰, 호스트 리랭커 LaunchAgent, Claude Code MCP 연결을 한 번에 세운다.<br>
스택 기동은 `./bootstrap --mac-mps`가 담당하고 `verify-all`이 GREEN일 때만 다음 단계로 넘어간다.<br>
그 뒤에 `~/.claude.json`을 수정한다(수정하기 전에 백업한다).<br>
여러 번 다시 실행해도 안전하다.

| 옵션 | 뜻 |
|---|---|
| `--owner SLUG` | 소유자 슬러그. 뱅크 `personal-<slug>`, ACL identity `<slug>` |
| `--claude-token-env VAR` | `claude setup-token` 값을 담은 환경 변수 이름(값은 명령줄로 받지 않는다) |
| `--write-claude-md` | `templates/CLAUDE.memory.md`를 `~/.claude/CLAUDE.md`에 넣는다 |
| `--launchd-prefix PREFIX` | LaunchAgent 레이블 접두사(기본 `com.hindsight-crew`) |
| `--uninstall` | 에이전트, MCP 항목, CLAUDE.md 블록을 지운다 |
| `--purge` | `--uninstall`과 함께: docker 데이터 볼륨까지 지운다 |
| `--dry-run` | 계획만 출력하고 아무것도 바꾸지 않는다 |

실제로 사용할 슬러그는 데모 멤버 이름인 `alice`/`bob`과 다른 이름을 쓴다.<br>
메모리 사용 규칙 스니펫은 [`templates/CLAUDE.memory.md`](templates/CLAUDE.memory.md)에 있다.<br>
개인 정책과 트리거를 두는 `policies/local/`은 gitignore 대상이라서 공개판에는 없다(사적인 맥락이 들어가는 위치다).<br>
`./install --owner <slug>`가 템플릿을 바탕으로 `policies/local/bank-policies.json`을 만든다([`docs/operating-guide.md`](docs/operating-guide.md) 2절).

설치기 없이 스택만 세우려면 `./bootstrap`(기본 `ko-full`, 컨테이너 CPU 리랭커)이나 `./bootstrap --cpu-en`(영어 경량 프로파일)을 쓴다.<br>
`./bootstrap`도 `verify/verify-all.sh`가 0으로 끝날 때만 성공으로 처리된다.

## 데모: retain 한 번, recall 한 번

`bootstrap`은 `secrets/acl.json`이 없으면 데모 멤버 `alice`·`bob`의 토큰을 만들어 `secrets/demo-tokens.env`에 저장한다.<br>
아래는 `verify/gate3_roundtrip.sh`가 검증할 때마다 보내는 요청과 같은 형식의 요청이다.<br>
토큰이 `ps`에 보이지 않도록 헤더를 표준 입력으로 넘긴다.

```sh
set -a; . secrets/demo-tokens.env; set +a
GW=http://127.0.0.1:8888/v1/default/banks/personal-alice

# retain: 내용은 "[머리표] " 로 시작하고, 태그는 <prefix>:<slug> 형식이어야 한다
printf 'authorization: Bearer %s\n' "$HC_DEMO_TOKEN_ALICE" | curl -s -H @- \
  -H 'content-type: application/json' -X POST "$GW/memories" \
  -d '{"items":[{"content":"[데모] quarterly budget review moved to Thursday","tags":["project:hindsight-crew","kind:probe"]}]}'

# recall
printf 'authorization: Bearer %s\n' "$HC_DEMO_TOKEN_ALICE" | curl -s -H @- \
  -H 'content-type: application/json' -X POST "$GW/memories/recall" \
  -d '{"query":"when is the quarterly budget review"}'
```

같은 retain 요청을 `$HC_DEMO_TOKEN_BOB`으로 `personal-alice`에 보내면 게이트웨이가 거부한다.<br>
머리표나 태그 형식이 틀리면 422 `rule-violation`과 위반한 규칙의 이름이 돌아온다.

자동 기억 주입 훅이 보내는 요청은 다음과 같은 형식이다.<br>
트리거 표에 맞는 행이 없으면 빈 200 응답이 돌아오고,<br>
맞는 행이 있으면 훅이 작업 앞에 붙일 주입 블록이 돌아온다(`?format=json`이면 JSON).

```sh
printf 'authorization: Bearer %s\n' "$HC_DEMO_TOKEN_ALICE" | curl -s -H @- \
  -H 'content-type: application/json' -X POST "http://127.0.0.1:8888/context-triggers/personal-alice" \
  -d '{"text":"<지금 하려는 작업 문장>","tool":"<도구 이름>"}'
```

## 합격 게이트 (`verify/verify-all.sh`)

| 게이트 | 확인하는 것 |
|---|---|
| ① health | 게이트웨이 프런트가 200을 돌려주고 upstream에 접속된다(`upstream_ok`) |
| ② banks | personal·team 뱅크가 프로비저닝됐다 |
| ③ roundtrip | `sync_retain` 뒤 `recall`이 적재한 기억을 돌려준다 |
| ④ isolation + attribution | A 토큰이 B 뱅크를 못 읽고, team 적재가 해당 멤버로 귀속된다 |
| ⑤ adversarial | ACL 없는 토큰, 경로 traversal, `X-Bank-Id`·본문 `arguments.bank` 바꿔치기, 귀속 위조를 모두 거부한다 |
| ✦ contract-drift | 라이브 계약이 선언된 스펙에서 벗어나지 않았다 |
| ⑦ MCP enforcement | MCP 규칙 강제(거부, 자동 채움, fail-open 없음)가 실제로 동작한다 |
| ⑧ rerank provider | 선언된 프로파일과 실제 리랭커 경로가 일치한다(조용한 CPU 폴백 없음) |
| ⑨ LLM chain | 모든 LLM 슬롯이 선언된 provider·모델을 쓰고, 토큰 체인 구성과 CLI 버전 하한이 맞다 |
| ⑥ restore | 백업을 빈 스택에 복원한 뒤 적재·recall이 동작한다 |

표의 행 순서는 실제 실행 순서다.<br>
게이트 번호는 도입한 순서라서 실행 순서와 다르다.

## 설계 결정

- [`CONTEXT.md`](CONTEXT.md): 이 프로젝트의 용어집.<br>
  튜닝 채택 기준(Pareto 개선), 보류, A/A 잡음폭, cold 요청 같은 판정 용어를 정의한다.
- [`docs/adr/`](docs/adr/): 되돌리기 어려운 결정의 기록.
  - [0001 태그 정규화는 선언된 어휘로 칸을 해석한다](docs/adr/0001-tag-normalization-resolves-declared-vocabulary.md)
  - [0002 공개판은 운영 리포에서 단방향으로 내보낸다](docs/adr/0002-public-edition-is-a-one-way-export.md)
- [`docs/THREAT-MODEL.md`](docs/THREAT-MODEL.md): 위협 모델과 업그레이드 경로(mTLS, 뱅크별 키, 레이트 리밋).
- [`docs/operating-guide.md`](docs/operating-guide.md): 운영자가 이 시스템을 실제로 쓰는 방식(뱅크 구조, 정책 설계, retain 형식, recall·reflect, 배포 습관).

무인 데모는 운영 환경에서 검증한 v1.1.0 다이제스트(`@sha256:50311d02...`)로 고정된 이미지를 쓴다.<br>
운영 환경에서도 `.env.local`의 `HC_HINDSIGHT_IMAGE`를 다이제스트로 고정한다(`.env.example` 참고).<br>
임베딩 모델이 벡터 차원을 결정하므로, 데이터를 적재한 뒤 프로파일을 바꾸면 전체를 다시 색인해야 한다.

## 실험 근거

recall 품질·속도 튜닝은 채택 기준을 먼저 정한 뒤 재생 하네스로 판정했다.<br>
판정 결과(채택, 불채택, 보류, 롤백)는 [`docs/benchmarks.md`](docs/benchmarks.md) 한 장에 모았다.<br>
각 행에는 비교 조건, 표본 수, 당시 코드·upstream 버전, 한계, 근거 보고서 링크가 있다.

하네스 사용법과 판정 규칙은 [`bench/README.md`](bench/README.md)에 있다.<br>
하네스는 운영자 도구다.<br>
운영 환경의 실사용 기록을 전제로 하며 외부에서 재현할 수 있는 합성 예제는 아직 없다.

## 측정값 (당시 기준)

아래 수치는 측정 시점의 값이다.<br>
현행 upstream은 Hindsight v1.x이고, 이 수치는 그 뒤로 다시 측정하지 않았다.

- **`ko-full-mac-mps` (2026-10-08 측정, 당시 hindsight 이미지 `0.9.1`):** Apple M5 Pro 24GiB, colima 6 vCPU / 10GiB.<br>
  맥 전체 실사용 약 10.5GiB(colima VM 약 6.5GiB + MPS 리랭커 사이드카 약 4.0GiB).<br>
  디스크 약 17GB(hindsight 이미지 5.93GB, 모델 캐시 볼륨 7.08GB, 호스트 리랭커 모델 약 2.3GB 등).
- **`ko-full` 라이브 검증 (당시 Hindsight `v0.8.3`):** Colima 6 vCPU / 10GiB, `bge-m3` + `bge-reranker-v2-m3`, 오프라인(`LLM=none`).<br>
  당시 게이트 구성 전부 GREEN, 한국어 의미 recall 확인.
- **`cpu-en` (2026-06-26 측정, 이후 재측정 없음):** `bge-small-en-v1.5` + `ms-marco-MiniLM`.<br>
  런타임 약 0.85GB RAM, 디스크 약 7GB, 2GB Docker VM에서 구동.<br>
  게이트 ⑦⑧⑨가 생기기 전의 구성으로만 검증했다.

## 프로파일

| 프로파일 | 임베딩 / 리랭커 | 용도 |
|---|---|---|
| `ko-full-mac-mps` | `bge-m3`(1024차원) / `bge-reranker-v2-m3`, 맥 MPS 사이드카 | `./install`이 세우는 구성 |
| `ko-full` | `bge-m3`(1024차원) / `bge-reranker-v2-m3`, 컨테이너 CPU | 사이드카 없이, recall이 느림 |
| `cpu-en` | `bge-small-en-v1.5` / `ms-marco-MiniLM` | 영어, 제약 호스트, CI 러너 |
| `gpu` | TEI로 서빙 | 옵트인, CUDA GPU 필요, 미검증 |

## 구조

```
gateway/         본문 인지·기본 거부 MCP/REST 정책 게이트웨이
verify/          verify-all.sh + 게이트 스크립트 + lib
scripts/         bootstrap·install 헬퍼, 백업·복원, 정책·태그 도구
profiles/        ko-full / ko-full-mac-mps / cpu-en / gpu
rerank-server/   맥 MPS 리랭커 사이드카
bench/           recall 재생 하네스(운영자 도구)
tests/           게이트웨이·하네스 단위 테스트
docs/            ADR, 위협 모델, 실험 판정 요약
```

## 커밋 이력 읽는 법

공개판은 이력이 없는 단일 커밋 스냅샷이다.<br>
운영판의 HEAD 트리를 커밋 하나로 내보내며,<br>
운영판의 작업 이력은 비공개 운영 리포에만 있다.<br>
그 커밋 메시지의 `Source-HEAD:`는 원본 운영 HEAD의 sha이고,<br>
`Exported-From:`은 내보내기에 사용한 스냅샷 커밋의 sha다(둘 다 공개판에서는 열 수 없다).

코드·문서에 있는 `#NNN`은 비공개 트래커 번호다.<br>
공개판에서는 그 이슈·PR을 열어 볼 수 없으므로,<br>
결정 근거는 [`docs/adr/`](docs/adr/)에 남긴다.

## 라이선스

MIT. [`LICENSE`](LICENSE) 참조.
