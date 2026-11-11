# 운영 가이드: 운영자는 이 시스템을 이렇게 쓴다

이 문서는 운영자 한 사람이 이 기억 시스템을 실제로 어떻게 쓰는지 한 장에 모은다.
뱅크·프로젝트 이름은 전부 데모 이름이다(`personal-demo`, `project:demo-app`).
주장마다 리포 안의 근거 문서·코드 경로를 달았으니,
그 파일을 열어 직접 확인할 수 있다.

용어(운영판·공개판)는 [`CONTEXT.md`](../CONTEXT.md)와 [ADR 0002](adr/0002-public-edition-is-a-one-way-export.md)를 따른다.
설치는 [`README.md`](../README.md)의 설치 절이 정본이고,
이 문서는 설치 뒤의 쓰임새만 다룬다.

## 1. 뱅크 구조: 개인 뱅크 하나와 `project:` 태그

**원칙: 운영자는 개인 뱅크 하나에 모든 기억을 담고, 프로젝트는 `project:` 태그로 나눈다.**

- 토큰 하나는 개인 뱅크 하나와 팀 뱅크 목록을 가진다.
  근거: [`gateway/acl.py`](../gateway/acl.py)의 `Principal`(`identity`, `personal`, `teams`).
- 개인 뱅크 템플릿은 `project` 접두를 태그 칸으로 선언하고,
  기록마다 소속 프로젝트를 태그로 달라고 안내한다.
  근거: [`policies/templates/personal-bank.json`](../policies/templates/personal-bank.json)의 `tag_prefixes`와 `instructions_text`("Tag with the project it belongs to").
- 요약(멘탈 모델)도 `project:<x>` 또는 `topic:<x>` 태그 하나 단위로 만든다.
  그래서 프로젝트를 태그로 나눠 두면 뱅크를 늘리지 않고도 프로젝트별 요약을 얻는다.
  근거: [`docs/operations.md`](operations.md) 6절(모델 규격: 단일 태그, `tags_match=all_strict`).

**전용 뱅크는 뱅크 단위로만 걸 수 있는 정책이 필요한 큰 프로젝트에만 만든다.**
뱅크를 따로 두어야만 얻는 것은 셋이다.

| 뱅크 단위로만 걸리는 것 | 근거 |
|---|---|
| 태그 스키마(필수 접두, 닫힌 어휘, 별칭) | [`gateway/bank_policies.py`](../gateway/bank_policies.py)의 `BankPolicy`, 정책 파일의 `banks.<뱅크>` 항목 |
| 연산 차단(`blocked_operations`) | [`gateway/bank_policies.py`](../gateway/bank_policies.py)의 `blocked_operations`, [`gateway/policy.py`](../gateway/policy.py)의 `blocked_operation_reason` |
| 접근 제어(어느 토큰이 이 뱅크를 여는가) | [`gateway/acl.py`](../gateway/acl.py)의 `allowed_banks` |

이 셋 중 어느 것도 필요 없으면 태그로 충분하다.
뱅크를 하나 늘리면 정책 항목과 ACL 항목이 함께 늘고,
정책을 바꿀 때마다 백업·재기동 절차를 밟아야 한다([`docs/operations.md`](operations.md) 3절).

여러 사람이 함께 볼 기억은 개인 뱅크가 아니라 팀 뱅크(데모 `team-eng`)에 둔다.
팀 뱅크 적재는 게이트웨이가 쓴 사람으로 귀속을 찍는다.
근거: [`gateway/app.py`](../gateway/app.py) 모듈 설명 4단계(team-retain attribution),
[`verify/gate4_isolation.sh`](../verify/gate4_isolation.sh).

## 2. 정책 설계: 공용 정책, 개인 뱅크 템플릿, 로컬 오버레이

정책은 세 층으로 나뉜다.

| 층 | 파일 | 커밋 여부 | 담는 것 |
|---|---|---|---|
| 공용 정책 | [`policies/bank-policies.json`](../policies/bank-policies.json) | 추적 | `defaults`와 데모·검증용 뱅크(`personal-alice`, `team-eng`) |
| 개인 뱅크 템플릿 | [`policies/templates/personal-bank.json`](../policies/templates/personal-bank.json) | 추적 | `__OWNER__` 자리표시자가 든 개인 뱅크 정책 |
| 로컬 오버레이 | `policies/local/bank-policies.json`, `policies/local/context-triggers.json` | gitignore | 소유자 뱅크(템플릿을 채운 것)와 그 트리거 |

- `defaults`는 느슨하고,
  엄격한 규칙은 뱅크가 직접 선언해야 켜진다.
  근거: [`gateway/bank_policies.py`](../gateway/bank_policies.py)의 `BankPolicy` 설명("Loose defaults; strict banks opt in").
- 새 설치는 템플릿의 `__OWNER__`를 소유자 이름으로 바꿔 `policies/local/bank-policies.json`으로 저장한다.
  데모 소유자 `demo`라면 뱅크는 `personal-demo`가 되고 `user` 닫힌 어휘는 `demo` 하나다.
  `./install --owner <slug>`를 쓰면 설치기가 이 렌더를 대신 한다(같은 이름 뱅크가 오버레이에 이미 있으면 그대로 둔다).
  근거: [`docs/operations.md`](operations.md) 4절 "새 설치",
  [`scripts/install/hc_install.py`](../scripts/install/hc_install.py)의 `policy_overlay`.
- 로더는 추적 파일에 로컬 오버레이를 병합한다.
  오버레이는 뱅크를 더하거나 바꿀 뿐이고,
  `defaults`는 추적 파일 것이 그대로 남는다.
  오버레이가 없으면 추적 파일만 쓰고,
  있는데 깨져 있으면 기동이 실패한다.
  경로는 `HC_BANK_POLICIES_LOCAL`로 덮을 수 있다.
  근거: [`gateway/bank_policies.py`](../gateway/bank_policies.py) 모듈 설명과 `with_local`·`from_files`.
- 오버레이를 커밋하지 않는 이유는 뱅크 설명에 사적 맥락이 들어가기 때문이다.
  근거: [`.gitignore`](../.gitignore)의 `policies/local/` 줄과 그 위 주석.
  그래서 공개판에 실리는 정책 파일은 공용 정책과 템플릿뿐이다([ADR 0002](adr/0002-public-edition-is-a-one-way-export.md)).
- 맥락 트리거도 같은 구조다:
  추적 [`policies/context-triggers.json`](../policies/context-triggers.json)에 `policies/local/context-triggers.json`이 병합된다.
  트리거는 다음 요청에서 다시 읽히고,
  정책은 게이트웨이를 재기동해야 반영된다.
  근거: [`docs/operations.md`](operations.md) 4절,
  [`gateway/context_triggers.py`](../gateway/context_triggers.py) 모듈 설명.

## 3. retain 형식과 강제

개인 뱅크 템플릿이 선언하는 형식은 이렇다([`policies/templates/personal-bank.json`](../policies/templates/personal-bank.json)).

| 규칙 | 선언 | 예 |
|---|---|---|
| 접두 라벨 | `content_prefix_pattern` `^\[[^\]]{1,12}\] ` | `[결정] 데모 앱 캐시를 끈다 - 재현 실패 원인` |
| 태그 꼴 | `tag_prefixes`의 접두 + `:` + 소문자 `[a-z0-9-]` 슬러그 | `project:demo-app` |
| 필수 접두 | `tag_required_prefixes: ["kind"]` | `kind:` 태그가 하나 이상 있어야 한다 |
| 닫힌 어휘 | `tag_closed_prefixes: ["kind", "user"]` + `tag_vocabulary` | `kind:decision`은 되고 목록 밖 `kind:` 값은 안 된다 |

클라이언트에 주는 안내문은 [`templates/CLAUDE.memory.md`](../templates/CLAUDE.memory.md)와 MCP `initialize` 안내다.
`initialize` 안내는 게이트웨이가 뱅크의 선언 규칙으로 만든 문구에,
정책의 `instructions_text`를 운영자 문구로 덧붙인 것이다.
근거: [`gateway/rules.py`](../gateway/rules.py)의 `instructions_for`.
하지만 안내는 권고일 뿐이고,
강제는 게이트웨이 한 곳에서 한다.
근거: [`gateway/rules.py`](../gateway/rules.py) 모듈 설명("The gateway is the only hard enforcement point for retain FORMAT rules").

**거부.**
규칙을 어긴 retain은 upstream으로 보내지 않고 규칙 id와 함께 돌려준다.

- 규칙 id: `RULE:tags-required`, `RULE:tags-grammar`, `RULE:tags-vocabulary`, `RULE:tags-missing-prefix`, `RULE:content-prefix`, `RULE:strategy-unknown` 등.
  근거: [`gateway/rules.py`](../gateway/rules.py)의 `RULE_*` 상수.
- MCP에서는 HTTP 200에 `isError:true`와 `RULE:<id>`가 오고,
  REST에서는 HTTP 422 `{"error":"rule-violation","rule":"RULE:<id>",...}`가 온다.
  권한 거부(403)와 일부러 다른 꼴이다.
  근거: [`gateway/app.py`](../gateway/app.py) 모듈 설명의 거부 본문 세 종류.
- 거부 메시지는 모델이 스스로 고치도록 무엇이 틀렸는지와 유효한 예시를 함께 담는다.
  근거: [`gateway/rules.py`](../gateway/rules.py) 모듈 설명(REJECTION).

**자동 교정.**
고칠 수 있는 값은 거부하지 않고 고쳐서 보낸 뒤,
무엇을 고쳤는지 응답에 알린다.

- 태그는 먼저 기계적으로 정규화(접두 누락, 대문자, 공백)하고,
  그 결과에 뱅크의 `tag_aliases`를 한 번(1홉) 적용해 옛 이름을 정식 이름으로 바꾼다.
  템플릿 예: `kind:done` -> `kind:completion`.
  근거: [`docs/operations.md`](operations.md) 5절,
  [`gateway/rules.py`](../gateway/rules.py)의 별칭 적용 부분.
- 접두 없는 맨단어를 선언된 어휘의 칸으로 해석하는 조건은 [ADR 0001](adr/0001-tag-normalization-resolves-declared-vocabulary.md)에 있다.
- 시각이 빠진 retain은 현재 시각을 채운다(`timestamp_autofill`).
  근거: [`gateway/rules.py`](../gateway/rules.py) 모듈 설명(CORRECTION).
- 별칭을 더할 때는 값이 다시 키가 되면 안 되고(비이행성),
  로더가 기동 때 검사해 어기면 크게 실패한다.
  근거: [`docs/operations.md`](operations.md) 5절,
  [`gateway/bank_policies.py`](../gateway/bank_policies.py)의 `_as_tag_aliases`.

## 4. recall, reflect, 멘탈 모델을 언제 쓰나

| 필요한 것 | 쓰는 것 | 근거 |
|---|---|---|
| 사실 조회, 지난 기록 확인 | recall | [`templates/CLAUDE.memory.md`](../templates/CLAUDE.memory.md)의 "recall first" |
| 한 프로젝트의 현재 규칙·결정 요약 적재 | 멘탈 모델(get) | [`docs/operations.md`](operations.md) 6절 |
| 판단, 충돌 판정, 패턴 질문 | reflect | 비용은 [`docs/operations.md`](operations.md) 6절, 사용 범위는 운영자 관행(아래) |

- **recall**은 이름이 붙은 프로젝트·시스템을 건드리기 전이나,
  "지난번", "이어서"처럼 앞선 작업을 가리킬 때 먼저 부른다.
  `max_tokens`는 비워 두거나 4096 이상으로 보낸다.
  예산이 첫 결과보다 작으면 upstream이 잘라 주지 않고 빈 결과를 돌려주기 때문이다.
  근거: [`templates/CLAUDE.memory.md`](../templates/CLAUDE.memory.md),
  [`gateway/bank_policies.py`](../gateway/bank_policies.py)의 `recall_max_tokens_floor` 주석,
  [`gateway/rules.py`](../gateway/rules.py)의 `RULE:recall-max-tokens-floor`.
- **멘탈 모델**은 태그 하나(`project:demo-app` 등)의 현재 유효한 규칙·결정을 요약해 둔 것이다.
  세션을 시작할 때 읽어 들이는 부팅 팩이자,
  reflect의 1차 캐시다(모델이 있는 태그의 reflect는 모델 하나로 끝나고 원본 재검색을 건너뛴다).
  갱신은 예약 작업 하나만 하고 서버 자동 갱신은 끈다.
  그래서 세션이 따로 refresh하지 않는다.
  근거: [`docs/operations.md`](operations.md) 6절,
  [`scripts/model-refresh.sh`](../scripts/model-refresh.sh).
- **reflect**는 LLM이 기억을 모아 추론하는 경로라 비싸다.
  멘탈 모델이 없으면 원본 기억을 다시 검색해 LLM 입력으로 넣는다.
  같은 태그를 멘탈 모델로 바꾸기 전의 reflect는 바꾼 뒤보다 input 토큰이 훨씬 많았다.
  호출도 길어서 게이트웨이의 reflect 변환은 실패해도 호출을 막지 않고 통과시키게(fail-open) 짰다.
  근거: [`docs/operations.md`](operations.md) 6절(1차 캐시와 전환 전후 input 토큰 실측),
  [`gateway/reflect_based_on_default.py`](../gateway/reflect_based_on_default.py) 모듈 설명.
  그래서 운영자는 reflect를 판단·충돌 판정·패턴 질문에만 쓰고,
  사실 조회는 recall로 한다.
  이 사용 범위는 리포 코드가 강제하는 규칙이 아니라 운영자 관행이다.
- 같은 태그로 reflect가 되풀이되면 그 태그를 멘탈 모델로 올린다.
  승격 기준은 "이 태그로 반복될 reflect 질문을 한 문장으로 쓸 수 있는가"이고,
  모델 없는 태그에서 발화한 reflect가 1순위 후보다.
  근거: [`docs/operations.md`](operations.md) 6절,
  [`gateway/reflect_demand.py`](../gateway/reflect_demand.py)(reflect 수요 계측).
  계측은 질문·맥락·답변·기억 본문을 남기지 않고,
  태그·모델 id·budget·isError·upstream 토큰 사용량 같은 메타데이터만 남긴다(모듈 설명).

## 5. 무엇을 남기고 무엇을 안 남기나

| 남긴다 | 템플릿 `kind` 예 |
|---|---|
| 검증된 완료 단위 | `kind:completion` |
| 근거 있는 결정 | `kind:decision` |
| 다시 찾기 비싼 함정·발견 | `kind:gotcha` |
| 운영자의 새 규칙·선호 | `kind:rule`, `kind:preference` |

**안 남긴다:**
중간 단계,
그리고 리포나 git에서 바로 다시 찾을 수 있는 것.
시크릿·토큰은 어떤 경우에도 남기지 않는다.

근거: [`templates/CLAUDE.memory.md`](../templates/CLAUDE.memory.md)의 retain 항목,
[`policies/templates/personal-bank.json`](../policies/templates/personal-bank.json)의 `instructions_text`("Retain verified work units, reasoned decisions, costly-to-rediscover findings, and new rules or preferences; skip intermediate steps and anything cheaply re-derivable from git or the repo")와 `tag_vocabulary.kind`.

데모 예:

```json
{"content": "[결정] 데모 앱 캐시를 끈다 - 재현 실패 원인", "tags": ["project:demo-app", "kind:decision"]}
```

## 6. 배포·검증 습관

- **롤백 좌표를 먼저 적는다.**
  게이트웨이 코드 커밋(`git rev-parse --short HEAD`),
  실행 중 컨테이너 기준 upstream 이미지 digest,
  정책 파일 백업 경로를 배포 전에 기록한다.
  `.env.local`은 gitignore라 이전 digest가 어디에도 자동으로 남지 않기 때문이다.
  근거: [`docs/operations.md`](operations.md) 1절(롤백 좌표 기록).
- **pull과 build를 한 번에 한다.**
  게이트웨이는 로컬 빌드라 이미지에 커밋이 기록되지 않는다.
  pull만 하고 재빌드를 잇지 않으면 새 코드가 들어간 것처럼 보여도 옛 코드가 돈다.
  pull 뒤에는 바로 `docker compose --env-file .env.local up -d --build --no-deps gateway`를 잇고,
  컨테이너 안 파일 해시를 커밋과 대조한다.
  정책 JSON만 바꿨다면 재기동으로 충분하지만,
  게이트웨이 코드는 이미지에 구워지므로 `--build`가 필요하다.
  근거: [`docs/operations.md`](operations.md) 2절(pull 뒤 재빌드, 컨테이너 코드 = 배포하려던 커밋 대조)과 3절(정책만 바꾸면 재기동),
  [`docker-compose.yml`](../docker-compose.yml)의 `build: ./gateway`.
- **`verify/verify-all.sh`를 배포 전후에 돌린다.**
  RED가 하나라도 있으면 배포를 멈춘다.
  CI는 이 검사를 돌리지 않으므로 로컬에서 사람이 GREEN을 확인해야 한다.
  근거: [`verify/verify-all.sh`](../verify/verify-all.sh),
  [`docs/operations.md`](operations.md) 1절·2절,
  [`README.md`](../README.md)의 합격 게이트 표.
- **정책을 바꾸면 재기동 전에 편집한 파일의 파싱·스키마를 확인한다.**
  스키마 위반이면 게이트웨이가 기동에 실패하기 때문이다.
  유닛 테스트는 리포 fixture로 로더 규칙만 보므로, 편집한 로컬 정책 파일은 게이트웨이 로더로 직접 읽어 본다.
  근거: [`docs/operations.md`](operations.md) 3절,
  [`tests/test_bank_policies.py`](../tests/test_bank_policies.py).
- **진단 출력은 필드만 본다.**
  `docker inspect`, `launchctl print`, 렌더된 compose 설정의 전체 출력에는 토큰·키 값이 평문으로 섞일 수 있다.
  그래서 `--format`으로 필드 하나만 꺼내거나,
  `launchctl print gui/$(id -u)/<레이블> | grep -E "state =|last exit code|Hour"`처럼 필터를 붙인다.
  값 자체가 나오는 명령(`launchctl getenv`)은 길이나 해시로만 확인한다.
  근거: [`docs/operations.md`](operations.md) 7절(`--format` 명령, 필터 필수, `launchctl getenv`는 길이·해시로만, 렌더 검사는 원문 대신 필드만).
