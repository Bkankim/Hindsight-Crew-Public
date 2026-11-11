# 런 2 A arm: main 고정 빌드 + 범위 (iv') 이전·완전 재통합 2회 (issue #324)

상위: #318. main 고정 빌드 `hc-upstream-main:b801b694d060`에서 범위 (iv') `[["project:*"],["topic:demo-style*"],["topic:demo-brand*"]]`을 뱅크 설정과 문서 PATCH 로 옮기고 완전 재통합을 2회(반복마다 D10 새 복원) 했다. 반복마다 세트 A·B 합친 세트로 (a)(b)(c)를 채점하고, arm 백업을 v1.1.0 bench 복제본에 올려 데이터 효과 벤치를 1 run 돌렸다.
이 문서에는 id·판정·해시·수치만 쓴다(정답표 항목 이름, 질의·응답 원문, 기억 본문, 뱅크 실명 없음, 공개 전환 가드 #4). 원문은 리포 밖 `~/.local/share/hindsight-bench/`(0700)에만 있다. 판정(합격·개선·효과 없음·판정 불가)은 #326 이 `judge-arms`로 낸다. 이 문서는 값만 적는다.

## 요약

- **두 반복 모두 완료**(1회차 첫 시도는 3g OOM 으로 "중단", 1절). 같은 이미지·같은 설정·같은 하네스 커밋(`4da9189`)·같은 `mem_limit` 5g 로 D10 새 복원에서 각각 시작했다.
- **재통합 비용**: 1회차 2시간 19분 39초·생성형 통합 654건, 2회차 2시간 27분 06초·652건. 1회 상한(4시간, 2,000건)과 70% 기준(2시간 48분, 1,400건) 모두 안이다. 관찰 4,924 -> 15,323 / 15,379, 범위 1,290 -> 309 / 309. 통합 실패·취소는 늘지 않았다.
- **판정 값(합친 세트 끝난 항목 open, 분모 30)**: (a) 0.00 / 1.00(유효 run 3개씩, 시도 3회씩), (b) full 2 / 4, (c) 4 / 4, (b) delta 2 / 2(보고만). invalid 0, 대조군 0 run 0. 기준선 main(#319)은 (a) 2.00, (b) 2.50, (c) 5 다(5절).
- **벤치(데이터 효과)**: 두 반복 모두 `arm_compare` FAIL(exit 1). 속도·p95 는 ok(0.919 / 0.923, 0.953 / 0.942)이고 토큰이 +10.03% / +8.34% 로 허용 0 을 넘었다(cost regressed). 두 run 모두 valid.
- 상한 사용: 이 티켓이 완전 재통합 3회(중단 1 + 완료 2), 누적 5시간 24분 48초를 썼다(6절).

## 0. 공통 조건

- 워크트리 HEAD `4da9189`(origin/main, #319·#320·#321·#323 머지 포함). 두 반복 모두 같은 HEAD 다(반복 사이에 바꾸지 않음).
- 이미지: `docker image inspect -f '{{.Id}}' hc-upstream-main:b801b694d060` -> `sha256:7707479b02f9b2008a3e1cb7fda308bc579b95732cf7cc25630daaf3c4124c02`(기대값과 같음), 라벨 `null`(`com.docker.compose.project=hc-next`·`hc.next.commit` 없음). 기동 뒤 `docker inspect -f '{{.Config.Image}} {{.Image}}' hc-next-hindsight-1`도 같은 태그·ID. 끝난 뒤에도 이미지 ID 그대로(지우거나 다시 빌드하지 않음).
- 기동 명령(워크트리): `HC_NEXT_IMAGE=hc-upstream-main:b801b694d060 HC_NEXT_MEM_LIMIT=5g HC_NEXT_BACKUP_SHA256=8a4900ea... bash scripts/next-stack.sh up --llm opus --backup <운영 체크아웃>/backups/keep-arch-v2-20261027-043001`. `HC_NEXT_MEM_LIMIT=5g`는 1절 OOM 뒤 코디네이터 결정이다(중단 시도는 기본 3g).
- 마이그레이션(hindsight 로그): `... f2a9c4e7b1d3 -> c7e2f9a4d1b8`, head `c7e2f9a4d1b8`.
- 적용 설정(워크트리 gitignore 오버레이 `templates/bank-config/local/<bank>.json` = 운영 오버레이 복사 + arm 키, 사적 값 없음, 원문 그대로):
  `{"version": "1", "bank": {"memory_defense": {"enabled": false, "rules": []}, "default_observation_scopes": [["project:*"], ["topic:demo-style*"], ["topic:demo-brand*"]]}}`.
  `next_env` 뒤 `python3 -B scripts/bank-config.py apply --bank <bank>` -> `patched: default_observation_scopes`, 다시 읽어 `same default_observation_scopes`·`same memory_defense`, exit 0(세 번 모두). `templates/bank-config/base.json`은 바꾸지 않았다.
- 문서 이전: `python3 -B scripts/scope-migrate.py migrate --bank <bank> --scopes '[["project:*"],["topic:demo-style*"],["topic:demo-brand*"]]' --apply`(기본 배치 50, 53배치). 런 1 #300 의 표본 3개 사전 PATCH 는 하지 않았다(코디네이터 결정).
- 재통합 시작 = 첫 PATCH 직전 시각이다. 설정 적용은 세 번 모두 통합을 시작하지 않았다(적용 20초 뒤 `pending_consolidation` 0, 적용 시각부터 `llm-requests` 0·통합 0).
  스펙 #318 상한 정의("처치 시작 = 설정 적용·backfill·문서 PATCH 중 가장 이른 것")로 세면 설정 적용 시각부터라 반복마다 약 3분 30초(-pre 페이지 생성 구간)가 더해진다. 아래 표에 둘 다 적는다.
- drain: `scripts/scope-migrate.py drain --bank <bank> --since <재통합 시작> --max-llm 2000 --timeout 14400 --interval 60 --timeseries <리포 밖>`(exit 1 이 일시 불일치 fail-closed 일 때 같은 `--since`로 다시 부르는 리포 밖 포장 스크립트. 재시도는 1회차 0회, 2회차 2회, 3절), 이어서 `python3 -B -m bench.arch_v2_regression stats --wait <남은 초> --wait-pages`.
- 채점 입력 sha256 대조(채점 전): 세트 A 고정 세트 `3fbe114b588af11e154b343639b8d5dcd9efe4b3c49343fb91559afbdd9d904d`, 세트 B 정답표 `6c50a56b42cf67b31b5d9592bb0ea4a63ac78e1fa891edcedcce6591e0566e6b`, 세트 B 고정 세트 `6798f3eb07f921baf00bf3c03b61224e34b4854fd89f355ce0ffc3c5803451a3`, 키 표 `3575b1c02e5baaca34c2e54f9dbb57ba87b8fe460ed636e0f1eef9d3d604138a`. 모두 #323 산출물 값과 같다.
- 채점 명령(`next_env` 뒤, `A2=(--answer-key <세트 A> --answer-key <세트 B> --evidence <r1-fixed-set> --evidence <r2-setb-fixed-set> --key-split <r2-key-split>)`, `query_timestamp=2026-10-28T14:59:59Z` 고정):
  처치 전 `score "${A2[@]}" --paths b --page-suffix -pre` -> 처치·drain·`--wait-pages` -> (b) delta `score "${A2[@]}" --paths b --read-pages --page-suffix -pre` -> (a) README 반복(유효 3개 또는 시도 6회) -> (b) full `score "${A2[@]}" --paths b` -> (c) `score "${A2[@]}" --paths c`.
- 분모: 합친 세트 끝난 항목 30(세트 A 15, 세트 B 15), 대조군 6(공유 C01~C03 + BC01~BC03). 키 표(sha256 위와 같음)로 세면 끝난 항목은 키 있음 16·키 없음 14이고 미정(null)이 없다(#319 표의 "미정 K02" 열이 이 표에는 없다).
- 메모리 감시(코디네이터 지시): 재통합 동안 60초마다 상태, 10분마다 `docker stats --no-stream`을 리포 밖에 기록. 운영 hindsight 6GiB 초과나 사본 OOM 이면 자동 down 하도록 걸었다(두 반복 모두 발동 없음).

## 1. 중단 시도: 3g OOM (1회차 첫 시도)

- 10:50:11 KST up(기본 `mem_limit` 3g, `docker-compose.next.yml` 40행 `HC_NEXT_MEM_LIMIT:-3g`), `restore:` sha256 D10 값. 설정 적용 10:50:51, -pre 생성 10:51:20~10:53:49(점수 디렉터리 `score-20261031-105120`, 판정 입력에 쓰지 않는다), 재통합 시작(첫 PATCH 직전) **10:53:55**, PATCH 2,633개 10:58:10 끝.
- 11:29:53 KST hindsight 컨테이너 종료: `docker inspect` State `exited exit=137 oom=true`, `HostConfig.Memory` 3221225472. 마지막 로그는 consolidation claude-code LLM 호출 여러 개가 60초대로 걸린 직후 `hindsight-api Killed`. drain 감시는 `docker exec ... is not running`으로 exit 1(상한 아님).
- 그때까지 비용(마지막 poll 11:29:44): 생성형 통합 **200건**, `llm-requests` 598(임베딩 398), 관찰 4,771, `pending_consolidation` 1,751, 통합 실패·취소 0. 비용 끝 = down 확인 **11:31:58**(처치 시작부터 38분 3초, 설정 적용부터 41분 7초).
- 처리(코디네이터 결정): 이 시도는 "중단", 완전 재통합 1회(예비)로 센다. 채점·arm 백업 없음. D10 새 복원으로 `HC_NEXT_MEM_LIMIT=5g`를 붙여 1회차를 다시 했고, 2회차도 같은 5g 다.
- 근거: 런 1 #300(v1.1.0)은 같은 3g 로 같은 처치를 통과했다. 5g 재시도의 사본 hindsight 메모리는 10분 표본 최대 약 2.75GiB 였다(10분 표본이라 순간 최고치는 잡지 못한다. 3g OOM 의 순간 사용량은 재지 못했다). 기준선 main(#319)은 재통합이 없어 3g 에서 문제가 없었다. 채점 조건(이미지·D10·하네스)은 같고 컨테이너 메모리 상한만 다르다.

## 2. 1회차 (5g, 완료)

| 단계 | 시각(KST) | 비고 |
|---|---|---|
| up | 11:32:17 ~ 11:32:43 | exit 0, `pg_verifybackup ok`, `docker inspect` `HostConfig.Memory` 5368709120(5GiB), `docker stats` 1.218GiB / 5GiB |
| 기동 직후 기준선 | 11:32:50 | documents 2,633, 관찰 4,924, 범위 1,290, `pending_consolidation` 0, operations completed 6,230·failed 40(batch_retain 18, retain 18, refresh_mental_model 4, 통합 0)·cancelled 0 |
| 설정 적용 | 11:32:53 | 20초 뒤 `pending_consolidation` 0, 적용 시각부터 `llm-requests` 0 |
| -pre 페이지 생성 | 11:33:22 ~ 11:36:26 | 7개(project_hint 합집합 6 + 전체 1) |
| 문서 PATCH | 11:36:26 ~ 11:41:02 | **재통합 시작 11:36:26(02:36:26Z)**, 2,633개 53배치, migrate exit 0 |
| drain | 11:41 ~ 13:56:05 | 재시도 0회, exit 0. **drain 완료 13:56:05(04:56:05Z)** |
| `stats --wait-pages` | 13:56:18 ~ 13:56:41 | exit 0, `pages_stale` [], `refresh_live` 0, `done_polls` 2 |
| 채점 | 13:56:54 ~ 14:08:50 | delta -> (a) 3회 -> (b) full -> (c) |
| arm 백업 | 14:09:07 ~ 14:09:19 | 워크트리 `backups/`에 썼고 운영 `backups/`는 그대로(확인), 곧바로 리포 밖으로 옮김 |
| down | 14:10:33 ~ 14:10:35 | `status` exit 3 |
| 벤치 | 14:10:38 ~ 14:34:27 | run `r2-a-1-20261031-141123` |
| 벤치 정리 | 14:34:27 ~ 14:34:37 | 확인 4개 빈 출력 |

- `restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4`
- PATCH 문서 수 **2,633**(중단 시도 때 dry-run `targets: 2633 documents in 53 batches of up to 50`, 이번 마지막 줄 `batch 53/53: 33 patched, 2633/2633 done`).
- drain 마지막 줄: `drain: drained after 8100.3s, 2067 LLM requests since 2026-10-31T02:36:26Z (generative 704, generative consolidation 654, local embedding 1335), 15323 observations in 309 scopes`, exit 0.

재통합 비용(처치 시작 02:36:26Z ~ drain 완료 04:56:05Z, 재통합 창의 `llm-requests` 전수 분류):

| 항목 | 값 |
|---|---|
| 소요 | **2시간 19분 39초**(설정 적용부터 세면 2시간 23분 12초) |
| `llm-requests` 전체 | 2,067(임베딩 1,335, 리랭크 28, 생성형 704) |
| 생성형 통합(`consolidation` 650 + `consolidation_dedup` 4) | **654** |
| 생성형 페이지 갱신(-pre 7개 delta, `refresh_mental_model` 42 + `mental_model_delta_ops` 8) | 50 |
| 토큰(입력/출력) | 통합 9,752,618 / 2,171,064, 페이지 갱신 2,267,449 / 55,336 |
| 관찰 수 | 4,924 -> **15,323** |
| 관찰 범위 수(태그 집합 수) | 1,290 -> **309** |
| operations 실패·취소 | 기준선 failed 40·cancelled 0(통합 0) -> drain 뒤 failed 40·cancelled 0(통합 0). 늘지 않아 완료 |
| 메모리(10분 표본 최대) | 사본 hindsight 2.75GiB / 5GiB, 운영 hindsight 1.40GiB |

drain 시계열 발췌(UTC, 범위 수가 찍힌 poll):

| at | 경과(초) | 관찰 | pending_consolidation | 생성형 통합 | 범위 |
|---|---|---|---|---|---|
| 02:41:05 | 0 | 743 | 2,356 | 42 | 186 |
| 03:06:23 | 1,518 | 3,792 | 1,889 | 170 | 239 |
| 03:31:56 | 3,051 | 6,290 | 1,502 | 267 | 254 |
| 03:57:40 | 4,596 | 9,312 | 1,026 | 395 | 281 |
| 04:23:33 | 6,148 | 12,039 | 557 | 508 | 293 |
| 04:49:48 | 7,723 | 15,125 | 32 | 646 | 309 |

### 2.1 채점 (1회차)

경로·run 별 출력(#326 judge-arms manifest 입력, 디렉터리는 `~/.local/share/hindsight-bench/arch-v2/` 아래. "pre(생성)"은 처치 전 `-pre` 페이지 생성 run 이라 판정 입력이 아니다):

| 경로·run | 출력 디렉터리 | summary.json sha256 | detail.json sha256 |
|---|---|---|---|
| pre(생성) | `score-20261031-113322` | `698abc7310b5edc39a357fdde40191f781d10b845f8e843f60957d32913d3b1a` | `a02a4f9df3adfd73127c6556992c916fc3cdafe1077b34f79424b71be2813a89` |
| b delta | `score-20261031-135654` | `41c61bbbb7ab32b25b6849e966e79bcc3ed8da2b9f70d5d17ae1708862a1b592` | `e825fc2cf895f7299e3c0956ee3853ba0600b970fe20818f7ede61a62806982d` |
| a #1 | `score-20261031-135725` | `5812e195c071415fe7bce08fdd3262477ec9be36ba85458a4e8163e10f34f554` | `88b34fdf0aa49fb4295d970653b3db46b6c6bffb7c1ce97d2b7269d5976ded4c` |
| a #2 | `score-20261031-135938` | `c0e7324051c03e8f0c22723a4c93e153f66ae54872a7ace57333f8cdc5ebb667` | `7014f88ff0490d61bc4b5d8b9f0881aaa17eb51514d1860539f91c71eb5d0c04` |
| a #3 | `score-20261031-140111` | `319def419dbdfd765a541cb858e338670c50b27a60543ba8f0e1d4e36b3a8e39` | `fedee91b19cf994ca2ca939268920838a5fe0c99e76e6d6a11accc7e3431139a` |
| b full | `score-20261031-140302` | `81a21ceb00220b4bb161c023f2c68ab50c3a1f0ada5321dc4e763e6203d9b6bc` | `948bbc6e26b16244846d2944ffd67ee58b3d9782b4319007276c9e8263c7c37a` |
| c | `score-20261031-140619` | `81ba229cf102dcad0271f01692013f771e4a0b461c627a8535bd4d3487cf3707` | `8fd12bf1bdcbd5d50ee07d7f7b1137452b30507e2f409105c218156ca05c8526` |

| 경로·run | 끝난 open 합친 (30) | 세트 A | 세트 B | 키 있음 | 키 없음 | 대조군 open 합친 (6) | 세트 A 대조군 (3) | 세트 B 대조군 (6) | invalid | (a) 상태 |
|---|---|---|---|---|---|---|---|---|---|---|
| pre(생성) | 2 | 1 | 1 | 2 | 0 | 3 | 3 | 3 | 0 | - |
| b delta | 2 | 2 | 0 | 2 | 0 | 3 | 3 | 3 | 0 | - |
| a #1 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 0 | valid |
| a #2 | 0 | 0 | 0 | 0 | 0 | 3 | 3 | 3 | 0 | valid |
| a #3 | 0 | 0 | 0 | 0 | 0 | 3 | 3 | 3 | 0 | valid |
| b full | 2 | 1 | 1 | 2 | 0 | 3 | 3 | 3 | 0 | - |
| c | 4 | 3 | 1 | 1 | 3 | 2 | 2 | 2 | 0 | - |

| id | pre(생성) | b delta | a #1 | a #2 | a #3 | b full | c |
|---|---|---|---|---|---|---|---|
| K01 | open | open | not-open | not-open | not-open | open | not-open |
| K02 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K04 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K05 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| K06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K08 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| K09 | not-open | open | not-open | not-open | not-open | not-open | not-open |
| K10 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K12 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K15 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K16 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| B02 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B04 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B05 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B12 | open | not-open | not-open | not-open | not-open | open | not-open |
| B13 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B15 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B16 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| B17 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B18 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B19 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| C01 | open | open | open | open | open | open | not-open |
| C02 | open | open | not-open | open | open | open | open |
| C03 | open | open | not-open | open | open | open | open |
| BC01 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC02 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |

| 경로·run | 질의 수 | 소요(초) | llm_calls(metrics) |
|---|---|---|---|
| pre(생성) | 7 | 184.6 | 127 |
| b delta | 7 | 30.8 | 0 |
| a #1 | 1 | 133.2 | 24 |
| a #2 | 1 | 92.4 | 12 |
| a #3 | 1 | 106.8 | 19 |
| b full | 7 | 196.3 | 151 |
| c | 36 | 150.9 | 71 |

- (a): **시도 3회, 유효 run 3개(#1·#2·#3), 대조군 0 run 0개**, invalid 0. 끝난 항목 open 0, 0, 0 -> 판정 값 0.00.
- (b) delta = `-pre` 페이지가 재통합 뒤 delta 로 갱신된 결과(`--read-pages`, POST·LLM 0회). (b) full = 처치 뒤 새 이름(접미사 없음) 첫 생성. 두 결과 모두 `deleted_evidence` 0.
- (c): 끝난 항목 open 4, 대조군 open 2.
- 새 대조군 BC01~BC03 은 1회차 어느 경로에서도 open 이 아니었다.

### 2.2 arm 백업과 벤치 (1회차)

- 경로 `~/.local/share/hindsight-bench/arm-backups/r2-a-1-20261031-140907/`(0700), `pg0.tar.gz` sha256 `0262cd54911f7e2d6b8d85421080f0a8edc0bb732fd38f82fad90d1f5c492d52`.
- `LINEAGE`(0600, `schema_version` 2): `arm` `r2-a-1`, `source_backup` D10, `settings` = 오버레이 두 키, `directives` [], `document_patches` 2633, `reconsolidation` {started_at 02:36:26Z, drained_at 04:56:05Z, llm_requests 2067, 생성형 704, 생성형 통합 654, 설정 적용 시각, mem_limit 5g}, `pages` {-pre, ""}, `image` {`hc-upstream-main:b801b694d060`, `sha256:7707479b...4c02`}, `invalidations` null, `expected_bank_overrides` = 백업 직전 overrides(`default_observation_scopes`·`memory_defense`). **LINEAGE sha256 `a91bf9e7c70d0711e14af81641a8ed32e08f3bb36368a4d5e6a06191fc9a00ac`**.
- 벤치(`bench/README.md` "1단계 arm 벤치" 명령, ref `270242e`, `--split dev --limit 40 --seed 13 --rounds 3 --clock-from-set s1`):
  - `up` s1 `backup_sha256` `8a4900ea...613f4`(D10), s2 `backup_sha256` `0262cd54...2d52`(= LINEAGE `backup.pg0_sha256`). `verify` s1·s2 exit 0, `pass: true`.
  - runner exit 0. `arm_compare` **exit 1, `overall: FAIL`**(valid true, invalid_reasons []):
    speed `ok`(기하평균 비 0.91948, CI95 [0.88682, 0.95208], 40쿼리 120쌍, 오염 0), p95 `ok`(0.95257, CI95 [0.91642, 0.97148]), **cost `regressed`**(토큰 base 295,475 -> cand 325,122, **+10.03%**, 늘어난 쿼리 36·줄어든 쿼리 4, 쿼리별 차 p50 +799).
  - 결과가 바뀐 쿼리 비율 40/40 = 1.0(보고만, 판정에 쓰지 않음). 세트 시계 s1 2026-10-26T19:30:00Z, s2 2026-10-31T04:54:00Z(`clocks_differ` true, s1 시계로 통일).

## 3. 2회차 (5g, 완료)

| 단계 | 시각(KST) | 비고 |
|---|---|---|
| up | 14:34:48 ~ 14:35:10 | exit 0, `HostConfig.Memory` 5368709120, `docker stats` 1.191GiB / 5GiB |
| 기동 직후 기준선 | 14:35:13 | 1회차와 같다(documents 2,633, 관찰 4,924, 범위 1,290, failed 40·cancelled 0, 통합 실패 0) |
| 설정 적용 | 14:35:15 | 20초 뒤 `pending_consolidation` 0, `llm-requests` 0 |
| -pre 페이지 생성 | 14:35:44 ~ 14:38:25 | 7개 |
| 문서 PATCH | 14:38:25 ~ 14:42:40 | **재통합 시작 14:38:25(05:38:25Z)**, 2,633개 53배치, migrate exit 0 |
| drain | 14:42 ~ 17:05:31 | exit 1 두 번(아래) 뒤 같은 `--since`로 다시, 마지막 exit 0. **drain 완료 17:05:31(08:05:31Z)** |
| `stats --wait-pages` | 17:05:39 ~ 17:06:02 | exit 0, `pages_stale` [], `refresh_live` 0 |
| 채점 | 17:06:15 ~ 17:18:12 | delta -> (a) 3회 -> (b) full -> (c) |
| arm 백업 | 17:18:26 | 운영 `backups/` 그대로(확인), 리포 밖으로 옮김 |
| down | 17:19:51 ~ 17:19:53 | `status` exit 3 |
| 벤치 | 17:19:59 ~ 17:48:43 | run `r2-a-2-20261031-172048` |
| 벤치 정리 | 17:48:43 ~ 17:48:48 | 확인 4개 빈 출력 |

- `restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4`
- PATCH 문서 수 **2,633**(마지막 줄 `batch 53/53: 33 patched, 2633/2633 done`).
- drain exit 1 두 번: `drain: llm-requests listed 1251 distinct rows but reports a total of 1252`(07:07:35Z), `... listed 1335 distinct rows but reports a total of 1336`(07:17:04Z). 상한(exit 2·3)이 아니고 감시 쪽 실패라 서버 통합은 계속 돌았다. 같은 `--since 2026-10-31T05:38:25Z`로 다시 불렀다(첫 번은 손으로, 둘째는 포장 스크립트). 코디네이터가 알려 준 "다섯 상태 합 일시 불일치"와 다른 경로의 같은 종류 fail-closed 다(8절).
- drain 마지막 줄: `drain: drained after 2901.5s, 2061 LLM requests since 2026-10-31T05:38:25Z (generative 702, generative consolidation 652, local embedding 1330), 15379 observations in 309 scopes`, exit 0(`drained after`는 마지막 재시작부터 센 값. 창 집계는 `--since`부터라 전체 값이다).

재통합 비용(05:38:25Z ~ 08:05:31Z):

| 항목 | 값 |
|---|---|
| 소요 | **2시간 27분 06초**(설정 적용부터 2시간 30분 16초) |
| `llm-requests` 전체 | 2,061(임베딩 1,330, 리랭크 29, 생성형 702) |
| 생성형 통합(`consolidation` 647 + `consolidation_dedup` 5) | **652** |
| 생성형 페이지 갱신(`refresh_mental_model` 43 + `mental_model_delta_ops` 7) | 50 |
| 토큰(입력/출력) | 통합 9,719,383 / 2,183,390, 페이지 갱신 2,472,671 / 46,750 |
| 관찰 수 | 4,924 -> **15,379** |
| 관찰 범위 수 | 1,290 -> **309** |
| operations 실패·취소 | 40·0(통합 0) -> 40·0(통합 0). 완료 |
| 메모리(10분 표본 최대) | 사본 hindsight 2.61GiB / 5GiB, 운영 hindsight 1.40GiB |

drain 시계열 발췌(UTC, 07:22 이후 경과는 재시작부터):

| at | 경과(초) | 관찰 | pending_consolidation | 생성형 통합 | 범위 |
|---|---|---|---|---|---|
| 05:42:44 | 0 | 844 | 2,342 | 40 | 192 |
| 06:07:58 | 1,515 | 3,793 | 1,899 | 166 | 238 |
| 06:33:29 | 3,046 | 6,468 | 1,478 | 267 | 252 |
| 06:59:17 | 4,593 | 9,213 | 1,047 | 383 | 280 |
| 07:22:21 | 312 | 11,329 | 670 | 471 | 289 |
| 07:48:35 | 1,885 | 14,082 | 215 | 589 | 302 |

### 3.1 채점 (2회차)

| 경로·run | 출력 디렉터리 | summary.json sha256 | detail.json sha256 |
|---|---|---|---|
| pre(생성) | `score-20261031-143544` | `5a70611d480c5ece5cae6d5158dbf8129edd37217f24b2e440d07f3132732a45` | `024d215b9ac84b12735241c729e49627b7312e96bdb611adafd70fc3cef36bc7` |
| b delta | `score-20261031-170615` | `3e4c98877f694784da5af73b93eec63cfaed9983c7abca83b7fffd46ed019612` | `43bc10cdfafae0aab84374e132dc09309908ac4f2b2bc9f0a52e5aefa2a4203d` |
| a #1 | `score-20261031-170703` | `7c1faca6b3d3874d16c2531f53e8981918ef8624752e60467c75e0efe24f98c5` | `0b591084f9388472c489030ac3f4f312aa766b36dc8aeba27652b257f6451f2d` |
| a #2 | `score-20261031-170839` | `a1ddf3d23412d54e16ac8152b77f53b659f8ad3f04a2a89ce0d029762fd4bf82` | `efc9587095c3857e72d5d0db441a901ca3fe43ccb100f8e4b0486b6fb2cbffec` |
| a #3 | `score-20261031-171112` | `046cf6f30bdf7b7edbd1298ff9d933b188b8b1dc11d7726317f8f6a4da950f37` | `03143277755256755cc307987899e4d86fca6e83eb03217c278c4d5ab668006c` |
| b full | `score-20261031-171251` | `ff55c8f088a0d2b4033f71135f94859eee1759fa4431d822e120b81aeda0e97b` | `c1cb1e95729a4033d8deee89dad14c61cea54bd84bc25fdf285ea2cffc9275f0` |
| c | `score-20261031-171540` | `51a5871d6f2e697a9bd10b8edcb5dd20bf2967d7423cdd42cdd0b8afe0eadc05` | `44088a065ed4d06398ad0669d2db23a991860685c5a33b2153ee989f3c9ae9ac` |

| 경로·run | 끝난 open 합친 (30) | 세트 A | 세트 B | 키 있음 | 키 없음 | 대조군 open 합친 (6) | 세트 A 대조군 (3) | 세트 B 대조군 (6) | invalid | (a) 상태 |
|---|---|---|---|---|---|---|---|---|---|---|
| pre(생성) | 3 | 2 | 1 | 3 | 0 | 3 | 3 | 3 | 0 | - |
| b delta | 2 | 1 | 1 | 2 | 0 | 3 | 3 | 3 | 0 | - |
| a #1 | 2 | 0 | 2 | 2 | 0 | 4 | 3 | 4 | 0 | valid |
| a #2 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 0 | valid |
| a #3 | 1 | 1 | 0 | 0 | 1 | 1 | 1 | 1 | 0 | valid |
| b full | 4 | 2 | 2 | 3 | 1 | 3 | 3 | 3 | 0 | - |
| c | 4 | 3 | 1 | 2 | 2 | 2 | 2 | 2 | 0 | - |

| id | pre(생성) | b delta | a #1 | a #2 | a #3 | b full | c |
|---|---|---|---|---|---|---|---|
| K01 | open | open | not-open | not-open | not-open | open | open |
| K02 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K04 | not-open | not-open | not-open | not-open | not-open | open | not-open |
| K05 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| K06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K08 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K09 | open | not-open | not-open | not-open | not-open | not-open | not-open |
| K10 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K12 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K15 | not-open | not-open | not-open | not-open | open | not-open | not-open |
| K16 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| B02 | not-open | not-open | open | not-open | not-open | not-open | not-open |
| B03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B04 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B05 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B06 | not-open | not-open | not-open | not-open | not-open | open | not-open |
| B07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B12 | open | open | open | not-open | not-open | open | not-open |
| B13 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B15 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B16 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| B17 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B18 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B19 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| C01 | open | open | open | open | open | open | not-open |
| C02 | open | open | open | not-open | not-open | open | open |
| C03 | open | open | open | not-open | not-open | open | open |
| BC01 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC02 | not-open | not-open | open | not-open | not-open | not-open | not-open |
| BC03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |

| 경로·run | 질의 수 | 소요(초) | llm_calls(metrics) |
|---|---|---|---|
| pre(생성) | 7 | 160.4 | 133 |
| b delta | 7 | 47.9 | 0 |
| a #1 | 1 | 95.1 | 12 |
| a #2 | 1 | 153.1 | 17 |
| a #3 | 1 | 98.5 | 13 |
| b full | 7 | 169.5 | 133 |
| c | 36 | 151.8 | 71 |

- (a): **시도 3회, 유효 run 3개, 대조군 0 run 0개**, invalid 0. 끝난 항목 open 2, 0, 1 -> 판정 값 1.00. #1 에서 새 대조군 BC02 가 처음 open 이었다(나머지 경로·run 에서 BC01~BC03 은 open 아님).
- (b) delta 2·full 4, 두 결과 모두 `deleted_evidence` 0. (c) 4, 대조군 2.

### 3.2 arm 백업과 벤치 (2회차)

- 경로 `~/.local/share/hindsight-bench/arm-backups/r2-a-2-20261031-171826/`(0700), `pg0.tar.gz` sha256 `444e9984107688c082c94318ac5d668e2472441a33bea1fa9fd73e9d2bde9e17`.
- `LINEAGE`(0600, `schema_version` 2, 1회차와 같은 모양, `arm` `r2-a-2`, `reconsolidation` {05:38:25Z, 08:05:31Z, 2061, 702, 652}, `expected_bank_overrides`는 1회차와 같은 값). **LINEAGE sha256 `8fabfc29fc3e8de84d8af51a156030c732e8be9a193a224bd7b15d2dea4c0577`**.
- 벤치(1회차와 같은 명령·ref `270242e`):
  - `up` s1 `backup_sha256` `8a4900ea...613f4`(D10), s2 `backup_sha256` `444e9984...9e17`(= LINEAGE `backup.pg0_sha256`). `verify` s1·s2 exit 0, `pass: true`.
  - runner exit 0. `arm_compare` **exit 1, `overall: FAIL`**(valid true, invalid_reasons []):
    speed `ok`(0.92265, CI95 [0.88861, 0.95886], 40쿼리 120쌍, 오염 6쌍 = 0.05), p95 `ok`(0.94218, CI95 [0.89083, 0.96655], 114쌍), **cost `regressed`**(토큰 base 295,475 -> cand 320,127, **+8.34%**, 늘어난 쿼리 34·줄어든 쿼리 6, 쿼리별 차 p50 +605).
  - 결과가 바뀐 쿼리 비율 40/40 = 1.0(보고만). 세트 시계 s1 2026-10-26T19:30:00Z, s2 2026-10-31T08:03:00Z(s1 시계로 통일).

## 4. 재통합 사이 흔들림 (1회차 vs 2회차)

같은 설정·이미지·하네스 커밋으로 D10 에서 두 번 재통합한 차이다(스펙 #318 W 의 "arm 두 반복 값의 차").

| 항목 | 1회차 | 2회차 | 차 |
|---|---|---|---|
| (a) 판정 값(유효 run 3개 평균) | 0.00 (0, 0, 0) | 1.00 (2, 0, 1) | 1.00 |
| (a) 대조군 open 평균 | 2.33 | 2.00 | 0.33 |
| (b) full 끝난 open | 2 | 4 | 2 |
| (b) delta 끝난 open | 2 | 2 | 0 |
| (c) 끝난 open | 4 | 4 | 0 |
| (b) full / (c) 대조군 open | 3 / 2 | 3 / 2 | 0 / 0 |
| 세트 A: (a) 평균 / (b) full / (c) | 0.00 / 1 / 3 | 0.33 / 2 / 3 | 0.33 / 1 / 0 |
| 세트 B: (a) 평균 / (b) full / (c) | 0.00 / 1 / 1 | 0.67 / 2 / 1 | 0.67 / 1 / 0 |
| 키 있음: (a) 평균 / (b) full / (c) | 0.00 / 2 / 1 | 0.67 / 3 / 2 | 0.67 / 1 / 1 |
| 키 없음: (a) 평균 / (b) full / (c) | 0.00 / 0 / 3 | 0.33 / 1 / 2 | 0.33 / 1 / 1 |
| 관찰 수(drain 뒤) | 15,323 | 15,379 | 56 |
| 관찰 범위 수(drain 뒤) | 309 | 309 | 0 |
| 생성형 통합 | 654 | 652 | 2 |
| 재통합 소요 | 2:19:39 | 2:27:06 | 7분 27초 |
| 벤치 cand 토큰 합계(base 295,475 같음) | 325,122 (+10.03%) | 320,127 (+8.34%) | 4,995 |
| 벤치 속도 / p95 비 | 0.91948 / 0.95257 | 0.92265 / 0.94218 | 0.003 / 0.010 |

항목 단위(끝난 항목과 대조군 중 open 인 id):

| 경로 | 1회차 | 2회차 |
|---|---|---|
| (a) #1·#2·#3 | C01 / C01 C02 C03 / C01 C02 C03 | B02 B12 BC02 C01 C02 C03 / C01 / C01 K15 |
| (b) delta | C01 C02 C03 K01 K09 | B12 C01 C02 C03 K01 |
| (b) full | B12 C01 C02 C03 K01 | B06 B12 C01 C02 C03 K01 K04 |
| (c) | B16 C02 C03 K05 K08 K16 | B16 C02 C03 K01 K05 K16 |

- (c)는 결정적 경로인데도 항목이 둘 바뀌었다(K08 -> K01). 재통합이 다시 만든 관찰 내용이 반복마다 달라 recall 결과가 달라진 것이다. 끝난 open 수는 같다.
- 범위 수는 두 반복 모두 309 로 같고, 관찰 수는 56(0.4%) 다르다. 런 1 #300(v1.1.0, 같은 범위)의 재통합 뒤 관찰 3,406·범위 309 와 비교하면 범위 수는 같고 관찰 수는 약 4.5배다(main 통합이 관찰을 훨씬 많이 만든다. 판정과 무관한 기록).

## 5. 기준선 main(#319) 대비 변화 (값만, 판정은 #326 judge-arms)

`judge-arms`(질의 0회)를 리포 밖 manifest(`baseline-main` = #319 산출물 summary 8개, `A` = 중단 1 + 위 두 반복)로 돌려 값을 검산했다. 판정 줄은 #326 이 정본으로 다시 낸다.

| 경로 | A 1회차 | A 2회차 | 기준선 main | W | 대조군 open A(반복별) / 기준선 |
|---|---|---|---|---|---|
| a | 0.00 | 1.00 | 2.00 | 3 | 2.33 / 2.00 / 2.00 |
| b-full | 2.00 | 4.00 | 2.50 | 2 | 3.00 / 3.00 / 3.00 |
| c | 4.00 | 4.00 | 5.00 | 0 | 2.00 / 2.00 / 2.00 |
| b-delta(보고만) | 2.00 | 2.00 | - | - | 3.00 / 3.00 |

- 검산 출력 첫 줄: `## A vs baseline-main: 효과 없음 [no-effect]`, 판정 불가 경로 없음, (a) 대조군 0 run 수 0 / 0, 개선 막힘 False. 판정은 #326 이 낸다.
- 값의 변화만 적는다: (a)는 두 반복 모두 기준선보다 낮지만 W(3, 기준선 유효 run 1~4 범위)를 넘지 않는다. (b) full 은 1회차 기준선 아래, 2회차 위다. (c)는 두 반복 모두 기준선보다 1 낮고 W 는 0 이다(c 의 기준선 반복 차는 0 으로 정의).
- 대조군 open 은 경로마다 기준선과 같거나 많다.

## 6. 상한 사용과 1회차 상한 판정

| 시도 | 처치 시작(첫 PATCH 직전, KST) | 끝(KST) | 소요 | 생성형 통합 | 상태 |
|---|---|---|---|---|---|
| 1회차 첫 시도(3g) | 10:53:55 | 11:31:58(down 확인) | 38분 03초 | 200(OOM 직전 poll) | 중단(OOM) |
| 1회차(5g) | 11:36:26 | 13:56:05(drain 완료) | 2시간 19분 39초 | 654 | 완료 |
| 2회차(5g) | 14:38:25 | 17:05:31(drain 완료) | 2시간 27분 06초 | 652 | 완료 |
| 합계 | | | **5시간 24분 48초** | 1,506 | 완전 재통합 3회 |

- 스펙 정의(설정 적용 시각부터)로 세면 41분 07초 + 2시간 23분 12초 + 2시간 30분 16초 = 5시간 34분 35초다.
- 상한 계산 기준: 스펙 #318 상한 절은 1회를 "처치 시작(설정 적용·backfill·문서 PATCH 중 가장 이른 것)부터 drain 완료까지"로 정의하므로, 누적 사용과 남은 예산은 스펙 정의 값(5시간 34분 35초)으로 센다. 위 표의 첫 PATCH 기준 값(5시간 24분 48초)은 `drain --since` 기준과 맞춘 참고값이다(final-review 20261031-180526 Codex P2 반영, 코디네이터 정정).
- 남은 예산(코디네이터 기록 기준 시작 5회·0시간): 중단 시도를 예비 1회로 센 코디네이터 결정에 따라 완전 재통합 2회(C 2) 남음, 누적 9시간 25분 25초 남음(15시간 - 5시간 34분 35초).
- 처치 시작 전 확인: 1회차(재시도) 전 남은 4회·사용 38분, 2회차 전 남은 3회·사용 약 2시간 58분. 둘 다 1회 상한 4시간 여유가 있었다.
- drain 직전 상한 확인: 두 반복 모두 PATCH 직후 곧바로 drain 을 걸었고, 첫 poll 이 처치 시작부터의 경과·생성형 통합을 찍었다(1회차 02:41:05Z 4분 39초·42건, 2회차 05:42:44Z 4분 19초·40건). 중단 시도는 drain 직전 별도 확인(처치 시작부터 4분 19초·36건). drain exit 2·3 없음. 완료된 두 반복은 스펙의 "drain 직전 상한 확인"을 따로 하지 않고 첫 poll로 대신했으므로 이 절차는 부분 이행이다. 첫 poll 값이 1회 상한(4시간·2,000건)과 멀어 결과에는 영향이 없다(final-review 20261031-180526 Spec (a)).
- **1회차 상한 판정**: 2시간 19분 39초 < 2시간 48분, 654건 < 1,400건으로 70% 기준을 넘지 않았다(스펙 정의 시작 기준 2시간 23분 12초도 2시간 48분 미만). status `#324 1회차 결과`(14:09 KST)로 알리고 벤치 뒤 2회차로 바로 갔다(ask 없음). C 반복 수는 코디네이터가 정한다.
- 기준선 페이지 생성·(a) 반복은 상한 밖이다. 반복마다 `metrics.json` `llm_calls`(경로 구간의 `llm-requests` 전체, 임베딩·리랭크 포함): -pre 생성 127 / 133, (a) 3회 55 / 42, (b) full 151 / 133, (c) 71 / 71.

## 7. 사본·복제본 구간과 정리

canary-exclude 형식(KST, up 부터 down·정리 확인 + 300초):

```
2026-10-31 10:50:11 2026-10-31 11:36:58  # #324 A 중단 시도(3g OOM) hc-next D10 사본(up 10:50:11 ~ down 확인 11:31:58 +300s)
2026-10-31 11:32:17 2026-10-31 14:15:35  # #324 A r1 hc-next D10 사본(up 11:32:17 ~ down 확인 14:10:35 +300s)
2026-10-31 14:10:38 2026-10-31 14:39:37  # #324 A r1 bench 복제본(up 14:10:38 ~ 정리 확인 14:34:37 +300s)
2026-10-31 14:34:48 2026-10-31 17:24:53  # #324 A r2 hc-next D10 사본(up 14:34:48 ~ down 확인 17:19:53 +300s)
2026-10-31 17:19:59 2026-10-31 17:53:48  # #324 A r2 bench 복제본(up 17:19:59 ~ 정리 확인 17:48:48 +300s)
```

- 끝 확인(17:48:59 KST): `bash scripts/next-stack.sh status` -> `hc-next: no resources` exit 3. `docker ps -aq --filter label=hc-bench=1`, `docker volume ls -q --filter label=hc-bench=1`, `docker network ls -q --filter name=hc-bench`, `lsof -nP -iTCP:18780-18799 -sTCP:LISTEN` 모두 빈 출력. status `#324 사본 반납` 17:49 KST.
- 워크트리 심링크(`secrets`·`audit`·`bench/.data`, 운영 체크아웃 읽기 전용)는 첫 up 직후 달았고(`audit`이 심링크인지 `test -L`로 확인) 끝에 지웠다. 오버레이는 gitignore 로 안 보인다.
- 운영 스택·운영 뱅크에는 쓰지 않았다. main 이미지는 지우거나 다시 빌드하지 않았다(끝에 ID 같음). `scripts/backup.sh`는 두 번 모두 워크트리 `backups/`에 썼고 운영 `backups/`에는 새 디렉터리가 생기지 않았다(전후 목록 대조).

## 8. 범위 밖 발견

- `scripts/scope-migrate.py` `list_paged`(151-167행): 통합이 도는 동안 `llm-requests` 창을 읽으면 `total`보다 고유 행이 1개 적은 경우가 3회 재읽기 안에 풀리지 않아 drain 이 exit 1 로 끝난다(2회차에서 2회, 증거: `drain: llm-requests listed 1251 distinct rows but reports a total of 1252`, `... 1335 ... 1336`). 다섯 상태 합 검사(#300 rework)와 같은 종류의 일시 불일치이고, 같은 `--since`로 다시 부르면 이어진다. 처방 후보: 불일치를 그 poll 의 경고로 남기고 다음 poll 로 넘기기(약 5줄, 파일 1개). 이 티켓 소유가 아니라 고치지 않았다.
- `docker-compose.next.yml` 40행 `mem_limit: ${HC_NEXT_MEM_LIMIT:-3g}`: main 고정 빌드의 완전 재통합이 3g 에서 OOM 으로 죽었다(1절). #325(C, 같은 main 이미지 완전 재통합)도 같은 위험이 있어 같은 5g 를 써야 조건이 맞는다. 기본값 변경 여부는 코디네이터 판단(1줄).
- `bench/README.md` "1단계 arm 벤치" 절 명령은 `PROD="$(cd ... && pwd)"` 형태라 워커 훅에 걸린다(공통 규칙대로 `PROD="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"`로 바꿔 썼다). 문서 1줄.
