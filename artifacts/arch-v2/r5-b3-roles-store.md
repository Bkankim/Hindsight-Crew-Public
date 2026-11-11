# R5-B3 S4 역할 판정기 - 사이드 저장소 백필 실측 (issue #376)

상위: #360 "결정됨"의 "S4·S3 구현". 짝 티켓: #377(S3, 같은 사이드 저장소 계약으로 읽는다).

## 1. 무엇을 만들었나

- `gateway/item_roles.py`: 순수 함수 모듈(네트워크·LLM 없음, 표준 라이브러리와 `item_keys`만 import).
  - 문장 분리·키 언급 찾기·병합(`split_sentences`·`memory_mentions`·`merge_roles`)은 #372 `bench/r5_roles.py`와 같은 동작이다.
  - 게이트웨이는 bench를 import할 수 없어 코드를 옮겨 썼다.
  - `tests/test_item_roles.py`가 bench 모듈을 import해 같은 고정 입력 묶음에서 결과가 같은지 대조하고, 상수 회귀도 둔다.
  - 사이드 저장소 스키마 v1 생성·버전 확인·공개(`publish`: 같은 디렉터리 새 파일 -> fsync -> `os.replace`, 저널 DELETE).
- `scripts/item_roles_sidecar.py`: 호스트 사이드카 CLI(`backfill`·`poll`·`status`).
  - upstream은 `scripts/bank-config.py`의 `resolve_target`/`upstream`(docker exec, 사본 가드)으로 GET만 한다.
  - 사본이 아닌 대상은 `--allow-prod`가 있어야 읽는다. 이 티켓에서는 운영 대상으로 실행하지 않았다.
  - LLM은 `claude -p --model claude-haiku-5-5 ...`이고 토큰은 자식 env에만 넣는다.
  - 프롬프트는 #372 `ROLE_PROMPT`와 같은 문안(sha256 `3baf354a9232636d9ebd2088ffd8c0ac81bc457d7aac23d424b8a6c2fd070b74`), 40개 묶음이다.
- `scripts/launchd/__PREFIX__.hindsight-item-roles-sidecar.plist.example`: 15분마다 `poll` 1회 예시. 설치하지 않았다.
- `.gitignore`: `policies/item-roles.sqlite*` 한 줄.

### 설계 판단

- `backfill`은 저장소 파일이 없을 때만 돈다(실수로 다시 전액을 쓰지 않게). 멈추면 거기까지 공개하고 나머지는 `poll`이 잇는다.
- `poll`의 재판정 조건은 `text_sha256` 변경과 함께, 본문이 같아도 키 집합이 바뀐 경우(태그 변경)를 넣었다.
  - 저장 역할의 키 집합과 지금 추출한 키 집합이 다르면 그 기억만 다시 판정한다.
- 다시 판정할 기억(본문·키 집합이 바뀜)의 옛 행은 판정 전에 뺀다. 판정이 실패하면 그 기억은 행 없는 미판정으로 공개되고 다음 `poll`이 다시 본다(fail-open, final-review 반영).
- 저장소 meta의 model·prompt_sha256·aliases_sha256이 지금 값과 다르면 `poll`은 거부한다(서로 다른 설정의 판정이 섞이지 않게).
- `aliases_sha256` = 소문자 정규화한 별칭 표의 정렬 JSON(`sort_keys`, `ensure_ascii=False`, 구분자 `,`·`:`) UTF-8 sha256(`item_roles.aliases_sha256`).
- 오류·시간 초과 재시도 1회와 빠진 항목 재시도 1회는 따로 센다(묶음당 최대 3호출, final-review 반영). 오류 재시도까지 오류면 그 묶음에 언급이 있는 기억 전부가 미판정이다.
  - 기억의 언급이 두 묶음에 걸쳐 있으면 어느 한쪽만 실패해도 그 기억은 미판정이다.
  - 빠진 항목 재시도 뒤에도 빠진 항목은 ref다.
- 지출 상한은 이번 실행 누계 + 호출당 예상(응답한 호출의 평균, 오류·시간 초과 호출은 빼고, 없으면 0.02달러)으로 매 호출 전에 본다(final-review 반영).
- 호출기의 예상 밖 예외도 그 묶음의 오류로 처리한다(재시도 규칙 그대로). 기록에는 예외 타입 이름만 남긴다(final-review 반영).
- meta에 선택 키 `bank`(대상 뱅크 id)를 쓴다. poll은 meta `bank`가 지금 `--bank`와 다르면 거부하고, 없는 옛 저장소는 이번 공개 때 채운다(final-review 반영, 계약 v1 필수 키·스키마는 그대로).

## 2. 실측 (hc-next D10 사본, 1회)

- 사본: `restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4`(D10 일치), 기준 커밋 `9657807`.
- 별칭 표: 런 5 실험 표 `r2-aliases.json`(파일 sha256 `21d1b75bae4c66f23f28061d6f1e9fae35ea4bfa931e57b63875aa6de606cdf6`, 99쌍).
  - 사본 상태 디렉터리 정책 `<state>/policies/local/bank-policies.json`의 대상 뱅크 `item_repo_aliases`에 넣었다(정책 경로, `--aliases-file`은 쓰지 않았다).
  - `gateway.bank_policies`로 병합해 읽은 표가 r2 표와 같음을 확인했다. meta `aliases_sha256` = `821e7076049f6b91e7da9ab3d3ce1cf75515b90574af6381a16d386e3870fa90`.
  - #372도 정책 표 없이 r2 표만 썼으므로 키 추출 입력이 같다.
- 명령: `item_roles_sidecar.py backfill --db <state>/policies/item-roles.sqlite --bank <bank> --policies <state>/policies/bank-policies.json --max-usd 2`
- 재통합 0회, 무효화 0회, upstream 쓰기 0(GET만), 사본 게이트웨이 재시작 없음.

### 2.1 판정 수·역할 분포

| 항목 | 값 |
|---|---|
| memories/list 나열 (world + experience) | 2635 |
| 키가 있는 기억 = 판정 기억 | 545 |
| LLM 판정 언급(문장 언급) | 1761 |
| item 태그만 있는 기억 | 0 |
| 저장 (기억, 키) 수(`merge_roles` 뒤) | 1562 |
| 미판정 기억 | 0 |
| 그중 dev 구간(mentioned_at <= 2026-10-13 KST 끝) 기억 / (기억, 키) | 430 / 1247 |
| 그 뒤 구간 기억 / (기억, 키) (수만, 본문·역할 미열람) | 115 / 315 |
| mentioned_at 없는 기억 | 0 |

dev 구간 (기억, 키) 역할 분포: done 481, open 447, ref 319. state는 545건 전부 `valid`다.

봉인 구간을 포함한 사본 뱅크 전체를 판정했다(R5-D 봉인 테스트에서도 저장소가 필요하다).
판정은 고정 프롬프트라 라벨을 보지 않는다.
2026-10-13 KST 끝 뒤 기억은 본문·역할을 열어 보거나 분석하지 않았고 수만 적는다.

### 2.2 #372 dev 430기억 LLM 역할과의 일치율

(기억, 키) 단위로 비교했다. 양쪽 다 `merge_roles` 뒤다. #372 쪽은 R5-A3 `roles-llm.json`(같은 모델·같은 프롬프트, 2026-11-02 01시대 1회)이다.

| 항목 | 값 |
|---|---|
| #372 기억 / 저장소에 있는 #372 기억 | 430 / 430 |
| (기억, 키) #372 / 저장소(같은 기억) / 공통 | 1247 / 1247 / 1247 |
| 한쪽에만 있는 쌍 | 0 / 0 |
| 일치 | 1147 / 1247 = **92.0%** |

키 집합은 완전히 같았다(언급 찾기의 동치를 사본 데이터에서도 확인).
남은 8.0%는 같은 모델·같은 프롬프트의 실행 간 차이다.

혼동(#372 -> 저장소):

| | done | open | ref |
|---|---|---|---|
| done | 456 | 3 | 19 |
| open | 4 | 421 | 30 |
| ref | 21 | 23 | 270 |

불일치 100건 중 93건이 ref와 비참조(done/open) 사이다. done과 open이 서로 뒤바뀐 것은 7건이다.

### 2.3 비용·시간

| 항목 | 값 |
|---|---|
| haiku 호출 | 45회(오류 0, 재시도 0, 빠진 항목 0) |
| 비용 | 0.163132달러(상한 2달러) |
| backfill 벽시계 | 2026-11-02 04:54:39 ~ 05:08:03 KST (13분 24초) |
| 호출 시간 합 | 793.8초(호출당 약 17.6초) |
| 직후 `poll` | 0호출, 변경 0, 파일 재공개 없음, `status` 미판정 0·aliases_match true |

### 2.4 저장소

- sha256 `dbd67cb741f7b673a313c3ee674ef78cc8ed254751807e259a0a0948e4d67f6d`.
- 사본 상태 디렉터리 `<state>/policies/item-roles.sqlite`(0644, S3 사본 확인용)와 리포 밖 `~/.local/share/hindsight-bench/arch-v2/r5-b3-item-roles.sqlite`(0600). 두 파일의 sha256이 같다.
- meta: `schema_version`=1, `model`=claude-haiku-5-5, `prompt_sha256`=3baf354a...(#372와 같음), `aliases_sha256`=821e7076..., `updated_at`=2026-11-01T20:08:03Z.
- 이 저장소에는 meta `bank`가 없다(final-review 수정 전에 만들었다). 다음 poll 공개 때 채워진다.
- final-review 수정 뒤에도 이 백필 결과(sha256 dbd67cb7...)는 바뀌지 않는다. 다시 백필하지 않았다.
  - 백필은 오류·재시도·빠진 항목이 0이라 재시도 분리, 옛 행 제거, 오류 호출 평균 제외, 예외 처리 경로를 타지 않았다.
  - 임시 DB 권한·임시 이름·기본 파일 권한·URI 이스케이프는 파일 처리만 바꾼다.
  - 별칭 정규화를 바꾼 뒤에도 r2 표의 aliases_sha256은 821e7076...로 같다(재계산 확인).

### 2.5 사본 구간

- hc-next D10 사본 up 2026-11-02 04:53:43 KST.
- 백필 뒤 사본은 #377 사본 확인이 이어 쓰도록 유지했고, 코디네이터 반납 지시 뒤 내렸다(down 확인 2026-11-02 05:14:49 KST, `next-stack.sh status` exit 3, hc-bench 자원 4종 빈 출력).
- down 이 사본 상태 디렉터리를 지우므로 `<state>/policies/item-roles.sqlite` 사본 파일도 함께 지워졌다. 리포 밖 파일(sha256 같음)은 남아 있다.
- canary-exclude: `2026-11-02 04:53:43 2026-11-02 05:19:49  # #376 hc-next D10 사본(S4 백필, #377 이어 씀, up 04:53:43 ~ down 확인 05:14:49 +300s)`

## 3. 한계

- 저장소 한 파일에는 한 뱅크의 역할만 들어간다(meta `bank`). 운영 다뱅크에서 S3는 #377-1의 `HC_ITEM_OPEN_TABLE_BANK`로 붙일 뱅크를 `<bank>` 하나로 한정한다.
- 지출 누계는 실행 한 번 안에서만 센다. 주기 poll에서 계속 실패하는 묶음은 실행마다 다시 과금될 수 있다(운영 창에서 상한·주기로 관리).

## 4. 검증

- `tests/test_item_roles.py` 43개, `tests/test_item_roles_sidecar.py` 43개(네트워크 0, 가짜 upstream·가짜 caller). final-review 반영 회귀 테스트 17개는 수정 전 커밋(82091d1)에서 모두 실패함을 확인했다.
- 토큰 비노출: 진짜 호출기 생성 경로를 서브프로세스로 돌리고, 가짜 토큰 명령(HOME 아래 `<토큰 명령>`)과 PATH의 가짜 `claude`·`docker`를 썼다.
  - 가짜 토큰·가짜 API 키가 stdout·stderr·자식 argv·sqlite·산출 디렉터리 파일에 없음을 확인한다.
- CI 동등 pytest(final-review 반영 뒤): Python 3.11 `8325 passed, 2 skipped` exit 0, Python 3.12 `8325 passed, 2 skipped` exit 0.
- 게이트웨이 동작·bench 불변: `git diff origin/main...HEAD -- gateway/app.py gateway/item_ledger.py gateway/item_status.py docker-compose.yml bench/` 빈 출력.
