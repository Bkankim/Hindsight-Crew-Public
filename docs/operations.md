# 운영 습관: 배포·검증·정책·멘탈 모델

운영자가 이 시스템을 배포하고 고칠 때 지키는 습관을 일반화해 모았다.
운영판의 운영자 런북에서 설치마다 같은 부분만 옮겼고,
호스트 경로·LaunchAgent 이름·운영 뱅크 이름·실측 일시는 담지 않는다.
이름은 전부 데모 이름이다(`personal-demo`, `project:demo-app`).
쓰임새 전체는 [`docs/operating-guide.md`](operating-guide.md)가 정본이고,
이 문서는 그 가이드가 근거로 가리키는 절차를 담는다.

명령의 `<gateway 컨테이너>`·`<hindsight 컨테이너>`는 `docker compose ps`가 보여 주는 이름으로 바꾼다.

## 1. 배포 전: 롤백 좌표를 먼저 적는다

배포 전에 세 값을 적는다.

| 좌표 | 명령 |
|---|---|
| 게이트웨이 코드 커밋 | `git rev-parse --short HEAD` |
| 실행 중 upstream 이미지 digest | `docker image inspect --format '{{index .RepoDigests 0}}' "$(docker inspect --format '{{.Image}}' <hindsight 컨테이너>)"` |
| 정책 파일 백업 | `mkdir -p backups && cp policies/local/bank-policies.json backups/bank-policies.$(date +%Y%m%d-%H%M%S).json` |

- `.env.local`은 gitignore라 이전 digest가 어디에도 자동으로 남지 않는다.
  적지 않으면 롤백할 값이 사라진다.
- digest는 로컬 `:latest` 태그가 아니라 실행 중 컨테이너의 이미지 Id에서 얻는다.
  컨테이너를 만든 뒤 `:latest`를 다시 pull했으면 둘이 다르다.
- 새 체크아웃은 코드 기본값이 곧 핀이다.
  근거: [`docker-compose.yml`](../docker-compose.yml)의 `image: ${HC_HINDSIGHT_IMAGE:-...@sha256:...}`,
  [`scripts/seed-demo.sh`](../scripts/seed-demo.sh)(새 `.env.local`에 같은 digest를 적는다).
- 유닛 테스트와 [`verify/verify-all.sh`](../verify/verify-all.sh)가 GREEN인지 확인한다.
  RED가 하나라도 있으면 배포를 멈춘다.
  CI는 유닛 테스트만 돌리고 verify-all은 돌리지 않는다(근거: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)).

## 2. 배포: pull과 build를 잇고, 컨테이너 코드를 커밋과 대조한다

게이트웨이는 로컬 빌드라 이미지에 커밋이 기록되지 않는다(근거: [`docker-compose.yml`](../docker-compose.yml)의 `build: ./gateway`).
pull만 하고 재빌드를 잇지 않으면 새 코드가 들어간 것처럼 보여도 옛 코드가 돈다.

```sh
docker compose --env-file .env.local up -d --build --no-deps gateway
for f in $(git ls-files gateway | grep '\.py$'); do
  [ "$(docker exec <gateway 컨테이너> sha256sum /app/$f | cut -c1-64)" = "$(git show HEAD:$f | shasum -a 256 | cut -c1-64)" ] || echo "DIFF $f"
done
```

- 대조 출력이 비어야 한다.
  이미지 생성 시각으로 판단하지 않는다.
- upstream `hindsight`는 건드리지 않는다(`--no-deps`).
  `.env.local`은 두 서비스의 공용 env 파일이라, 키를 바꾼 뒤 `--no-deps` 없이 `up -d gateway`를 하면 의존 서비스 `hindsight`까지 재생성될 수 있다(근거: [`docker-compose.yml`](../docker-compose.yml) gateway 서비스 주석, [`scripts/next-stack.sh`](../scripts/next-stack.sh)의 `--no-deps`).
  재생성하면 임베딩·리랭커 모델을 다시 올린다.
- `/healthz`는 200만 보지 않고 `upstream_ok: true`까지 본다.
  `ok`는 게이트웨이 자신의 상태라 upstream이 죽어도 true다(근거: [`gateway/app.py`](../gateway/app.py)의 `/healthz` 처리, `upstream_ok`).
- 배포 뒤 verify-all을 다시 돌린다.

## 3. 정책 갱신: 재기동만, 먼저 테스트

`policies/`는 읽기 전용 바인드 마운트라 정책 JSON만 바꿨다면 재빌드 없이 게이트웨이 재기동으로 반영된다.
게이트웨이 코드는 이미지에 구워지므로 코드 변경은 `--build`가 필요하다.
근거: [`docker-compose.yml`](../docker-compose.yml)의 `./policies:/app/policies:ro`,
[`gateway/bank_policies.py`](../gateway/bank_policies.py) 모듈 설명(Update procedure).

1. 현재 파일을 백업한다(1절).
2. 로더 규칙을 바꿨다면 유닛 테스트를 돌린다: `python -m pytest tests/test_bank_policies.py tests/test_policy_overlay.py`.
   이 테스트는 리포의 fixture로 로더를 검사할 뿐이라 편집한 로컬 정책 파일은 보지 않는다.
3. 편집한 파일은 재기동 전에 게이트웨이가 기동 때 쓰는 로더로 직접 읽어 본다(추적 파일 + `policies/local/bank-policies.json` 오버레이, 깨졌으면 예외로 끝난다):
   `uv run --no-project --python 3.11 --with-requirements gateway/requirements.txt python -c "from gateway.bank_policies import BankPolicies; BankPolicies.from_files('policies/bank-policies.json'); print('POLICIES_OK')"`.
   근거: [`gateway/bank_policies.py`](../gateway/bank_policies.py)의 `from_files`(게이트웨이 기동 경로 `load`가 부르는 함수).
4. `docker compose --env-file .env.local restart gateway`.
5. 기동 로그에 의도치 않은 `defaults-only` 경고가 없는지 본다.
6. verify-all GREEN.
7. 롤백은 백업 복원 뒤 같은 재기동이다.

파일이 없으면 기동은 하되 `defaults-only` 경고를 남기고,
파일이 있는데 깨졌으면 기동이 실패한다.
이 비대칭은 의도다.
근거: [`gateway/bank_policies.py`](../gateway/bank_policies.py)의 `load`(파일 없음은 `defaults-only` 경고, 있는 파일은 엄격 검증).

## 4. 로컬 오버레이와 새 설치

- 로더는 추적 파일 `policies/bank-policies.json`에 `policies/local/bank-policies.json`(gitignore)을 병합한다.
  로컬 파일은 `banks` 키만 갖고,
  뱅크마다 추가하거나 같은 이름의 추적 뱅크를 통째로 대체한다(추적 `defaults`는 상속).
  근거: [`gateway/bank_policies.py`](../gateway/bank_policies.py) 모듈 설명(Local overlay),
  [`tests/test_policy_overlay.py`](../tests/test_policy_overlay.py).
- 맥락 트리거는 추적 행 뒤에 로컬 행을 잇고,
  두 파일 중 하나만 바뀌어도 다음 요청에서 다시 읽는다(핫 리로드).
  정책은 재기동해야 반영된다.
  근거: [`gateway/context_triggers.py`](../gateway/context_triggers.py) 모듈 설명.
- 새 설치는 [`policies/templates/personal-bank.json`](../policies/templates/personal-bank.json)의 `__OWNER__`를 소유자 이름으로 바꿔 `policies/local/bank-policies.json`으로 저장한다.
  소유자 `demo`라면 뱅크는 `personal-demo`다.
  `./install --owner <slug>`가 이 렌더를 대신 한다.
  근거: [`scripts/install/hc_install.py`](../scripts/install/hc_install.py)의 `policy_overlay`.

## 5. 태그 별칭 더하기

- 별칭은 뱅크 정책의 `tag_aliases`(옛 이름 -> 정식 이름)다.
  게이트웨이는 retain 때 기계적 정규화 뒤,
  문법 검사 전에 한 번(1홉) 적용한다.
  그래서 키는 정규화 뒤의 꼴로 적는다(접두 없는 맨단어 `demo`는 `topic:demo`로 들어온다).
  근거: [`gateway/rules.py`](../gateway/rules.py)의 태그 평가(별칭 적용 부분).
- 더하는 순서:
  1. 로컬 정책 파일의 그 뱅크 `tag_aliases`를 편집한다.
  2. 게이트웨이를 재기동한다(3절).
  3. 로더가 기동 때 별칭의 태그 문법과 비이행성(값이 다시 키가 되면 안 된다)을 검사해 어기면 크게 실패하므로 기동 로그를 본다.
  근거: [`gateway/bank_policies.py`](../gateway/bank_policies.py)의 `_as_tag_aliases`.

## 6. 멘탈 모델: 규격, 갱신, 승격

- 규격: 태그 `project:<x>` 또는 `topic:<x>` 하나, `tags_match=all_strict`, `refresh_after_consolidation=false`.
  서버 자동 갱신(`refresh_cron`)은 비워 두고 예약 작업 하나만 갱신한다(이중 발화 금지).
  서버 `refresh_cron`으로 옮긴 설치에서는 이 예약 작업을 내리고 [`docs/model-refresh-cron.md`](model-refresh-cron.md)를 따른다.
  그때는 아래 `model-refresh.sh` 직접 실행도 쓰지 않는다(둘을 함께 켜지 않는다).
  근거: [`scripts/model-refresh.sh`](../scripts/model-refresh.sh) 머리 주석,
  [`docs/model-refresh-cron.md`](model-refresh-cron.md)(서버 cron 쪽 사실).
- 갱신 드라이버는 모델 태그의 기억 수가 지난 갱신 뒤 늘지 않았으면 건너뛰고,
  48시간마다 전체 갱신을 한다.
  대량 결정 직후 즉시 갱신이 필요하면 `bash scripts/model-refresh.sh --force`.
  근거: [`scripts/model-refresh.sh`](../scripts/model-refresh.sh),
  [`tests/test_model_refresh_clear_mode.py`](../tests/test_model_refresh_clear_mode.py).
- 모델이 있는 태그의 reflect는 근거가 모델 한 건으로 끝나고 원본 재검색을 건너뛴다(reflect 1차 캐시).
  운영판 실측에서 한 태그를 모델로 바꾼 뒤 reflect input 토큰이 약 5분의 1로 줄었다.
- 승격 기준은 "이 태그로 반복될 reflect 질문을 한 문장으로 쓸 수 있는가"다.
  모델 없는 태그에서 발화한 reflect가 1순위 후보이고,
  주간 감사가 알리면 사람이 승인해 만든다(자동 생성·삭제 없음).
  근거: [`scripts/model_audit.py`](../scripts/model_audit.py) 머리 설명,
  [`gateway/reflect_demand.py`](../gateway/reflect_demand.py)(수요 계측, 메타데이터만).

## 7. 진단 출력은 필드만 본다

`docker inspect`, `launchctl print`, 렌더된 compose 설정의 전체 출력에는 토큰·키 값이 평문으로 섞일 수 있다.

- `docker inspect`는 `--format`으로 필드 하나만 꺼낸다(1절 명령).
- `launchctl print`는 항상 필터를 붙인다: `launchctl print gui/$(id -u)/<레이블> | grep -E "state =|last exit code|Hour"`.
- 값 자체가 나오는 명령(`launchctl getenv`)은 `| wc -c`나 해시로만 확인한다.
- `docker compose config`는 원문 대신 확인할 키만 `grep`으로 고른다.

## 8. 임베딩 프로필은 첫 기동 때 고정된다

임베딩 모델이 곧 벡터 차원이라 프로필은 데이터를 넣기 전에 한 번 고른다.
나중에 바꾸면(예: 영어 경량 <-> 한국어) 전체 재색인이나 데이터 삭제가 필요하다.
근거: [`scripts/profile-detect.sh`](../scripts/profile-detect.sh) 머리 주석.

- 고른 프로필은 `.env.local`의 `HC_PROFILE`에 적힌다(근거: [`scripts/seed-demo.sh`](../scripts/seed-demo.sh)).
- 그 뒤 `bootstrap`에 다른 프로필을 주면 무시하고 `.env.local` 값을 쓴다(근거: [`bootstrap`](../bootstrap)의 `locked by .env.local` 분기).
- 바꾸려면 플래그가 아니라 데이터를 비우고 `.env.local`을 다시 만드는 재시드가 필요하다.
  1절대로 백업을 먼저 받는다.
