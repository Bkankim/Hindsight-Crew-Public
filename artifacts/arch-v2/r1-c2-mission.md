# R1-c2 미션 arm - 범위 (iv') + O1 관찰 미션 재통합 실측 (issue #300, 상위 #296)

범위 (iv') `[["project:*"], ["topic:demo-style*"], ["topic:demo-brand*"]]`에 O1 관찰 미션 출발 후보(`observations_mission`)를 함께 적용하고 D10 사본에서 재통합까지 돌려 잰 결과다.
비교 기준은 같은 날 같은 순서로 잰 범위 단독 arm `artifacts/arch-v2/r1-c1b-scope-iv.md`(이하 arm 1)다. O3(`consolidation_strategies`)는 넣지 않았다.
이 파일에는 항목 id·판정·해시·집계만 쓴다(정답표 항목 이름, 질의·응답 원문, 개인 경로 없음). O1 문안은 스펙 #296 "결정됨"의 공개 가능한 출발 후보라 원문을 적는다. 그 밖의 원문은 리포 밖 `~/.local/share/hindsight-bench/arch-v2/`(0700)에만 있다.

## 요약

- 재통합 1회: 2026-10-30 07:39:43Z(표본 3개 사전 PATCH) ~ 10:07:39Z(drain 완료), **2시간 27분 56초**. 문서 2,633개 PATCH, 생성형 통합 호출 **650건**(생성형 전체 732건, `llm-requests` 전체 2,122건). arm 1(2시간 26분 46초, 650건)과 비용이 같다. 상한(4시간, 생성형 2,000건) 안이다.
- 관찰 3,406(arm 1) -> **3,173**(-233, -6.8%), 범위 309 -> 306. 줄어든 것은 project 범위 관찰(2,820 -> 2,585, -8.3%)이다. topic 범위 관찰 35·21 -> 34·24, combined 530 -> 530.
- 회귀 세 경로 모두 **불합격**(invalid 0). arm 1 대비: (a) 끝난 항목 open 합 6/45 -> 3/75, 대조군 9/9 -> 7/15(5회 중 2회 대조군 0), (b) delta 2/15 -> 1/15, full 3/15 -> 2/15(full 대조군 3/3 -> 2/3), (c) 2/15 -> 1/15(K01 해소).
- **미션 효과 판정: 1회 측정으로 판정 불가.** 모든 경로의 차이가 인용한 흔들림 폭 안이거나((a)(b)), 비교할 흔들림 폭이 없다((c), 재통합 간 흔들림 미측정). 관찰 수 -6.8%와 (a) 대조군 감도 저하는 "시사"로만 적는다.
- **채택 미션 권고: 이 run 으로 채택 결정 없음**(6절). 출발 후보 O1 은 문안 sha256 `93722a7b...`, 415자다.
- 미션 변경만으로는 기존 관찰이 다시 만들어지지 않는다(upstream v1.1.0 코드·문서, 7절). 그래서 arm 1 백업 위에서 미션만 바꾸지 않고 D10에서 다시 출발했다.
- operations 실패·취소: 기준선 실패 40(통합 0)·취소 0 -> drain 뒤 실패 40(통합 0)·취소 0. 이 arm 은 완료다.
- 벤치(동시 두 세트 s1 = arm 1, s2 = arm 2, 한 run): **FAIL**(유효 run). 속도 비 0.9961, 섞은 p95 비 1.0021 은 ok, 토큰 283,085 -> 284,327(+0.44%)로 비용 허용 0 을 넘었다(8절).

## 1. 사본과 복원원

- up 전: `bash scripts/next-stack.sh status` exit 3(arm 1 사본 down 뒤), `docker ps -aq --filter label=hc-bench=1` 0줄(arm 1 벤치 정리 뒤).
- up(2026-10-30 16:35:14~16:35:41 KST): `HC_NEXT_BACKUP_SHA256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4 HC_NEXT_BACKUP_DIR=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001 HC_NEXT_REQUIRED_BANK=personal-demo bash scripts/next-stack.sh up --llm opus` exit 0.
- 복원 줄 원문(홈 경로만 바꿈):

```
restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4
pg_verifybackup ok
up ok: commit=69d7999140af276691bffd15e1236fdfe4a29a66 gateway=http://127.0.0.1:18888 ui=http://127.0.0.1:9998 llm=opus
```

- sha256 이 D10 값과 같다. 재통합 전 probe 가 arm 1 재통합 전 값과 모두 같다(관찰 4,924, 범위 1,290, 원문 2,635, project 없는 원문 239, 태그 필터 recall 41·34·48·51).
- down: `bash scripts/next-stack.sh down` -> `down ok`(19:27:33), 직후 `status` exit 3, `docker ps -aq --filter name=hc-next`·`docker volume ls -q --filter name=hc-next` 0줄.

### operations 실패·취소 기준선과 drain 뒤

| 시점 | 전체 | 실패 | 취소 | 통합(consolidation*) 실패 | 통합 취소 |
|---|---|---|---|---|---|
| 기준선(up 직후, 16:36 KST) | 6,270 | 40 (batch_retain 18, retain 18, refresh_mental_model 4) | 0 | 0 | 0 |
| drain·채점 뒤(19:21 KST) | 6,313 | 40 (같음) | 0 | 0 | 0 |

- 통합 실패·취소가 늘지 않았다. 그 밖의 operation 의 새 실패·취소도 없다.

## 2. 적용 설정

- 워크트리 gitignore 오버레이 `templates/bank-config/local/personal-demo.json` = 운영 오버레이(`memory_defense`) 복사 + arm 키:

```json
{"version": "1", "bank": {"memory_defense": {"enabled": false, "rules": []}, "default_observation_scopes": [["project:*"], ["topic:demo-style*"], ["topic:demo-brand*"]], "observations_mission": "<O1 원문, 아래>"}}
```

- O1 원문(스펙 #296 "결정됨"과 바이트 단위로 같다, 415자, sha256 `93722a7bee848386d3a4e43574830aac200aa94aa63710b78fa43daaacb3afa9`):

> Observations are the current, reconciled state of the owner's projects: decisions in force, rules, system configurations, work items and their status, and known pitfalls. When a newer fact changes a state (completed, reverted, superseded, retired, fixed), update the observation to the new state and keep one line of what it replaced. Keep durable preferences and recurring patterns. Ignore one-off process chatter.

- `HC_TARGET=next python3 -B scripts/bank-config.py diff --bank personal-demo` -> `change default_observation_scopes: null -> [...]`, `same memory_defense`, `change observations_mission: null -> "Observations are the current, ..."`, exit 3.
  `apply`(07:36:00Z) -> `patched: default_observation_scopes, observations_mission`, 다시 읽어 `same` 셋, exit 0.
- **적용 직후 통합 시작 여부**: 20초 뒤 stats `pending_consolidation` 0, `pending_operations` 0, 적용 시각 이후 `llm-requests` 0건. 미션·범위 적용만으로 통합이 시작되지 않았다. 그래서 재통합 시작은 arm 1 과 같이 사전 PATCH 시각이다.
- 적용 뒤 뱅크 override 전체(LINEAGE `expected_bank_overrides`): `default_observation_scopes` 위 값, `memory_defense` `{"enabled": false, "rules": []}`, `observations_mission` O1 원문. 지시문 0개.

## 3. 사전 PATCH 와 범위 동작

- arm 1 과 같은 표본 3개(양성 `ff76b043`, 대조 1 `0889e591`, 대조 2 `581ef3e3`, `r1-c1b-scope-iv.md` 3절 표)를 같은 순서로 upstream 직결 PATCH 했다(07:39:43.52Z~07:39:43.68Z, 3건 `success: true`).
  그 전에 `-pre` 페이지 7개를 만들었다(16:36:32~16:39:43 KST, `score --paths b --page-suffix -pre`, exit 0).
- drain: `drain --since 2026-10-30T07:39:43Z --interval 60 --timeout 1800 --max-llm 2000` -> `drained after 121.4s, 69 LLM requests ... (generative 30, generative consolidation 4, local embedding 25), 4923 observations in 1291 scopes`.
- 범위 확인(`time_field=updated_at`, arm 1 과 같은 방법): 관찰 5개. 대조 1 -> `project:demoproj14` 3개, 대조 2 -> `project:demoproj13` 1개(모두 출처 1), 양성 -> 기존 combined 관찰 `8f587896`(출처 3)에 흡수. arm 1 과 같은 모양이다(과도기 흡수, arm 1 보고서 3절).
- 전수 PATCH·drain 뒤 사후 확인(19:08 KST): `8f587896`은 지워졌다. 대조 관찰 4개는 project 태그만이다. 양성 기억의 관찰은 `503062aa`(`topic:demo-style`, 출처 2)와 `c7c88280`(**`project:demoproj14`, `project:demo-blog`**, 출처 2)다.
  arm 1 은 양성의 project 관찰이 `project:demo-blog` 하나였다. 이번에는 project 태그 2개인 범위의 관찰에 합쳐졌다(10절 범위 밖 발견).

## 4. 문서 PATCH 와 재통합 비용

- `scripts/scope-migrate.py migrate --bank personal-demo --scopes '[["project:*"], ["topic:demo-style*"], ["topic:demo-brand*"]]' --apply --batch-size 200 --batch-sleep 0`(07:41:46Z~07:45:37Z) -> 14 배치, `2633/2633 done`, exit 0. PATCH 대상 2,633개(사전 PATCH 3 포함).
- 구간: 사전 PATCH **07:39:43Z** ~ drain 완료 **10:07:39Z**(KST 16:39:43~19:07:39), **2시간 27분 56초**.
- drain: `drain --since 2026-10-30T07:39:43Z --interval 60 --timeout 14340 --max-llm 2000 --timeseries <스크래치>/drain2.jsonl`, 140 poll, 재시도 없음.
  마지막 줄: `drain: drained after 8521.4s, 2122 LLM requests since 2026-10-30T07:39:43Z (generative 732, generative consolidation 650, local embedding 1347), 3173 observations in 306 scopes`, exit 0.

| 구분 (`llm-requests`, operation / scope / provider) | arm 1 | **arm 2** |
|---|---|---|
| consolidation / consolidation / claude-code | 649 | **649** |
| consolidation_dedup / claude-code | 1 | **1** |
| consolidation / embedding_query + embedding_document / local | 649 + 649 | **649 + 645** |
| refresh_mental_model / reflect + reflect_tool_call / claude-code | 11 + 59 | **13 + 56** |
| mental_model_delta_ops / delta_ops + retraction_ops / claude-code | 10 + 3 | **10 + 3** |
| refresh_mental_model / rerank(tei) + embedding(local) | 45 + 55 | **43 + 53** |
| **합계** (생성형, 통합) | 2,131 (733, 650) | **2,122 (732, 650)** |

- 토큰(claude-code, 페이지 갱신 포함): 입력 10,008,290, 출력 2,213,651, 캐시 3,489,644(arm 1 10,066,516 / 2,216,676 / 3,511,426).
- 시계열(drain poll 발췌, UTC):

| 시각 | 관찰 | pending_consolidation | 생성형 통합 | 전체 llm-requests | 범위 수 |
|---|---|---|---|---|---|
| 07:45:38 | 611 | 2,339 | 32 | 155 | 192 |
| 08:00:44 | 996 | 2,042 | 125 | 430 | 223 |
| 08:20:59 | 1,416 | 1,723 | 213 | 692 | 244 |
| 08:41:18 | 1,781 | 1,385 | 295 | 935 | 255 |
| 09:01:43 | 2,200 | 1,013 | 398 | 1,244 | 279 |
| 09:22:09 | 2,479 | 708 | 468 | 1,459 | 286 |
| 09:42:47 | 2,838 | 359 | 556 | 1,719 | 296 |
| 10:07:39 | 3,173 | 0 | 650 | 2,122 | 306 |

- 처리 속도: 원문 약 17.8개/분(arm 1 약 17.9개/분, 둘 다 원문 2,633개 / 전체 구간). 미션이 통합 호출 수·토큰을 바꾸지 않았다(문안 415자가 프롬프트에 더해지지만 입력 토큰 차이 -0.6%).
- 관찰·범위 전후(관찰 전체 나열, arm 1 과 같은 방식):

| | D10 | arm 1 (iv') | **arm 2 (iv') + O1** | arm 2 - arm 1 |
|---|---|---|---|---|
| 관찰 | 4,924 | 3,406 | **3,173** | -233 (-6.8%) |
| 관찰 범위 | 1,290 | 309 | **306** | -3 |
| 관찰 1개인 범위 | 505 | 81 | **81** | 0 |
| project 범위 / 관찰 | - | 132 / 2,820 | **129 / 2,585** | -3 / -235 (-8.3%) |
| `topic:demo-style` 범위 / 관찰 | - | 1 / 35 | **1 / 34** | 0 / -1 |
| `topic:demo-brand` 범위 / 관찰 | - | 1 / 21 | **1 / 24** | 0 / +3 |
| project 없는 combined 범위 / 관찰 | 176 / 530 | 175 / 530 | **175 / 530** | 0 / 0 |
| project 없는 관찰 | 530 | 586 | **588** | +2 |

### 태그 필터 recall·topic 모델 (arm 1 과 같은 probe)

- 같은 질의문·태그·`tags_match: all_strict`·`budget: mid`·`max_tokens: 8192`·`query_timestamp=2026-10-28T14:59:59Z`, upstream REST recall(`types: ["observation"]`, 읽기만). 재통합 전 16:36 KST, delta 채점 뒤 19:08 KST.

| 질의 | D10 recall / 뱅크 일치 | (i) (#298) | arm 1 (iv') | **arm 2 (iv') + O1** |
|---|---|---|---|---|
| `kind:completion` + `project:hindsight-crew` | 41 / 260 | 0 / 0 | 0 / 0 | **0 / 0** |
| `kind:decision` + `project:hindsight-crew` | 34 / 151 | 0 / 0 | 0 / 0 | **0 / 0** |
| topic 모델 1(`topic:demo-style`) | 48 / 60 | 1 / 1 | 29 / 35 | **34 / 34** |
| topic 모델 2(`topic:demo-brand`) | 51 / 51 | 0 / 0 | 21 / 21 | **24 / 24** |

- topic 모델 2개의 관찰 가시성은 arm 1 과 같은 수준이다(미션이 이를 바꾸지 않는다). `kind:` 필터 관찰 recall 은 범위 설계상 0 그대로다.

## 5. 회귀 세 경로 (#297 고정 세트 18건)

- 고정 세트 sha256 `3fbe114b588af11e154b343639b8d5dcd9efe4b3c49343fb91559afbdd9d904d`. evidence 는 다시 만들지 않았다. 순서는 arm 1 과 같다:
  `stats --wait 3600 --wait-pages`(19:07:48~19:08:11, 10:08:07Z 연속 2회 `pages_stale: []`·`refresh_live: 0`, exit 0) -> delta(19:08:16~19:08:30) -> full·(a) 1회·(c)(19:08:32~19:16:48) -> (a) 2·3회(19:16:48~19:21:03).
  (a) 2회째가 대조군 0/3(감도 상실)이라 사본이 떠 있는 동안 2회를 더 쟀다(19:21:48~19:25:49). arm 1 은 3회다(사본이 내려가 더 잴 수 없었다).
- 리포 밖 출력: `score-20261030-163632/`(pre), `score-20261030-190816/`(delta), `score-20261030-190832/`(full·a 1회·c), `score-20261030-191648/`·`191914`·`192148`·`192349`(a 2~5회).

| 경로 | arm 1 끝난 항목 open / 대조군 | **arm 2 끝난 항목 open / 대조군** | invalid | deleted_evidence | 질의·소요·LLM(arm 2) | 판정 |
|---|---|---|---|---|---|---|
| (a) reflect 나열 | 3회: (3,3) (2,3) (1,3), 합 6/45, 대조군 9/9 | 5회: (2,3) (0,0) (1,3) (0,1) (0,0), 합 **3/75**, 대조군 **7/15** | 0 | - | 회당 1회, 145.8·146.0·108.5·121.0·153.3초, 10·6·5·5·6건 | 불합격 |
| (b) delta(`-pre` 갱신본) | 2/15 (K01, K02), 3/3 | **1/15 (K01)**, 3/3 | 0 | 0 | 7회, 13.3초, 0건 | 불합격 |
| (b) full | 3/15 (K01, K09, K10), 3/3 | **2/15 (K01, K02)**, **2/3**(C03 놓침) | 0 | 0 | 7회, 268.3초, 135건 | 불합격 |
| (c) recall 상태 | 2/15 (K01, K05), 2/3 | **1/15 (K05)**, 2/3 | 0 | - | 18회, 81.4초, 35건 | 불합격 |

- (b) pre(재통합 전 첫 생성, 같은 D10): arm 1 3/15 (K01, K02, K04), arm 2 2/15 (K01, K04), 둘 다 대조군 3/3. 설정이 같은 D10 상태에서 만든 페이지가 1건 다르다(첫 생성 흔들림).
- (a) OPEN 절 줄 수: arm 1 100·136·89, arm 2 101·67·95·101·89. 대조군 0 인 run 은 67·89줄이다. 목록 길이는 크게 줄지 않았는데 대조군 항목이 빠졌다.

| id | (a) arm1 1 | (a) arm1 2 | (a) arm1 3 | (a) arm2 1 | (a) arm2 2 | (a) arm2 3 | (a) arm2 4 | (a) arm2 5 |
|---|---|---|---|---|---|---|---|---|
| K01 | open | open | open | open | not-open | open | not-open | not-open |
| K02 | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K10 | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K15 | not-open | open | not-open | open | not-open | not-open | not-open | not-open |
| C01 | open | open | open | open | not-open | open | open | not-open |
| C02 | open | open | open | open | not-open | open | not-open | not-open |
| C03 | open | open | open | open | not-open | open | not-open | not-open |
| 나머지 K 11개 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |

| id | (b) arm1 pre | (b) arm1 delta | (b) arm1 full | (b) arm2 pre | (b) arm2 delta | (b) arm2 full | (c) arm1 | (c) arm2 |
|---|---|---|---|---|---|---|---|---|
| K01 | open | open | open | open | open | open | open | not-open |
| K02 | open | open | not-open | not-open | not-open | open | not-open | not-open |
| K04 | open | not-open | not-open | open | not-open | not-open | not-open | not-open |
| K05 | not-open | not-open | not-open | not-open | not-open | not-open | open | open |
| K09 | not-open | not-open | open | not-open | not-open | not-open | not-open | not-open |
| K10 | not-open | not-open | open | not-open | not-open | not-open | not-open | not-open |
| C01 | open | open | open | open | open | open | not-open | not-open |
| C02 | open | open | open | open | open | open | open | open |
| C03 | open | open | open | open | open | not-open | open | open |
| 나머지 K 9개 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |

### 미션 효과 판정 (흔들림 폭과 비교)

비교 기준은 arm 1 이다. 재통합 두 번 사이의 흔들림은 잰 적이 없으므로, 폭을 넘는 차이도 "시사"로만 쓴다.

| 경로 | arm 1 -> arm 2 | 인용한 흔들림 폭 | 판정 |
|---|---|---|---|
| (a) | run 별 끝난 항목 open 1~3 -> 0~2, 대조군 3/3 x 3 -> 0~3 | 같은 사본·설정의 reflect 1회 흔들림: `r1-c3-read.md` 3절 base 6 run 끝난 항목 open 0~3, 대조군 0~3(대조군 0 인 run 1회 포함) | 폭 안 -> **1회 측정으로 판정 불가**. 대조군 0 run 이 5회 중 2회라 감도 저하를 시사한다(목록 축소일 수 있다) |
| (b) delta | 2/15 -> 1/15 | (b) 첫 생성 0~3/15(`r1-c1-o2.md` 7절), 이 티켓 pre 2~3/15 | 폭 안 -> **판정 불가** |
| (b) full | 3/15 -> 2/15, 대조군 3/3 -> 2/3 | 같음 | 폭 안 -> **판정 불가** |
| (c) | 2/15 -> 1/15 (K01 해소) | 같은 사본·설정에서는 결정적(`r1-c3-read.md` 3절: base 의 (c)가 #298 과 판정·토큰이 같다). **재통합 사이의 (c) 반복 결과는 r1-c3-read.md 에 없다** | 비교할 흔들림 폭 없음 -> **1회 측정으로 판정 불가**. 참고: 범위만 다른 (i)과 (iv') 사이에서 (c)는 3건이 바뀌었다(`r1-c1b-scope-iv.md` 6절) |
| 관찰 수 | 3,406 -> 3,173 (-6.8%) | 재통합 사이 관찰 수 흔들림 미측정 | 시사만: 미션이 project 범위 관찰을 줄였을 수 있다 |
| 벤치 토큰 | 283,085 -> 284,327 (+0.44%) | 재통합 사이 흔들림 미측정(같은 세트 A/A 는 토큰 차이 0, `r1-a-baseline.md` 7절) | 시사만 |

- 결론: 미션 효과로 판정한 경로는 없다. 두 arm 은 같은 D10 에서 같은 순서로 처리했지만 재통합 LLM 의 비결정성이 남는다.

## 6. 채택 미션 권고

- **이 run 으로 채택 결정 없음.**
- 사유:
  1. 회귀 세 경로 모두 arm 1 과의 차이가 인용한 흔들림 폭 안이거나 비교할 폭이 없다(5절). 미션이 "이미 끝난 일이 열린 일로 나온다"를 줄였다는 근거가 없다.
  2. 감도 신호가 나쁜 쪽이다: (a) 대조군 9/9 -> 7/15(대조군 0 run 2/5), (b) full 대조군 3/3 -> 2/3. 끝난 항목 open 감소가 상태 판단 개선이 아니라 목록 축소일 가능성을 가를 수 없다.
  3. 재통합 비용은 같다(생성형 통합 650 = 650, 시간 +1분). 벤치는 속도 차이 없음, 토큰 +0.44% 로 비용 축 FAIL(8절, 재통합 간 흔들림 미측정이라 시사만).
- 출발 후보(재측정할 때 쓸 값): O1 원문 2절, 415자, sha256 `93722a7bee848386d3a4e43574830aac200aa94aa63710b78fa43daaacb3afa9`.
- 판정을 가르려면: 같은 설정의 재통합을 한 번 더 해 재통합 간 흔들림((c) 판정, 관찰 수, (a) 대조군 감도)을 재야 한다. 1단계 재통합 상한(4회)은 이 티켓으로 모두 썼다(#298 1, #299 1, #300 2). 결정은 코디네이터·#304 몫이다.

## 7. 미션 변경만으로 관찰이 다시 만들어지는가 (upstream 근거)

- **다시 만들어지지 않는다.** 미션은 그 뒤에 통합되는 기억에만 적용된다.
- 코드(upstream v1.1.0 이미지 `/app/api/hindsight_api`, 읽기만):
  - `PATCH /v1/default/banks/{bank_id}/config`(`api/http.py` 9918행 `api_update_bank_config`) -> `engine/memory_engine.py` `update_bank_config`(14971행)·`_update_bank_config_authenticated`(15345행) -> `config_resolver.py` `_persist_bank_config`(665행~)는 `banks.config`에 override 를 합쳐 쓰고 캐시를 무효화할 뿐이다. 기억의 `consolidated_at`을 비우거나 통합 작업을 큐에 넣지 않는다.
  - 미션은 통합 시점에 읽힌다: `engine/consolidation/consolidator.py` 2575행(`mission=(config.observations_mission or "").strip()`), `engine/consolidation/prompts.py` 218행 `build_mission_section`.
  - 기존 관찰을 다시 만드는 경로는 따로 있다: 문서 범위 이동(`engine/memories/pg/documents.py` 575행~: 이동한 기억이 출처인 관찰 삭제 + `consolidated_at = NULL`), 관찰 전체 초기화(`clear_observations`, `engine/memories/pg/engine_curation.py` 168행 `clear_observations_and_requeue`), 기억별 관찰 삭제.
- 문서(upstream `hindsight-docs/versioned_docs/version-1.1/developer/observations.mdx`, `gh api`로 읽기만): "Observations Mission" 절은 미션이 무엇을 합성할지 정한다고만 쓰고 기존 관찰 재처리는 말하지 않는다. 처음부터 다시 만들려면 "Resetting All Observations"(모든 원문 기억의 통합 상태를 초기화해 다음 통합이 관찰을 처음부터 다시 만든다)를 따로 써야 한다고 적는다.
- 사본 실측: 2절 - 미션·범위 적용 20초 뒤 pending_consolidation 0, 적용 이후 `llm-requests` 0건.
- 그래서 이 arm 은 arm 1 백업 위에서 미션만 바꾸지 않았다. 같은 범위 값 재전송도 관찰을 다시 만들지 않는다(`r1-b-scope.md` 3절, observations.mdx "Changing the Scopes"). D10(combined)에서 출발해 범위 이동으로 전 문서를 다시 큐에 넣었다.

## 8. 벤치 (동시 두 세트, 한 run)

- 명령: `bench/README.md` "1단계 arm 벤치" 절 그대로. 이슈 본문대로 **양쪽 세트 모두** 각 LINEAGE 로 띄웠다: s1 = arm 1 백업(`up --set s1 --backup <arm 1>/pg0.tar.gz --expected-overrides <arm 1>/LINEAGE`), s2 = arm 2 백업(`--expected-overrides <arm 2>/LINEAGE`).
  벤치 사이드카 `r1`(18781) 공유, 팔 base(s1, 18891)·cand(s2, 18892) 모두 `gw-up --ref 270242e --local-policies <운영 체크아웃>/policies/local`. up 전 `next-stack.sh status` exit 3, `docker ps -aq --filter label=hc-bench=1` 0줄.
- 세트: s1 `backup_sha256` = `90443dfa7a06...`(arm 1 LINEAGE 와 같다), s2 = `2c3d04f9ff56...`(arm 2 LINEAGE 와 같다). 이미지 둘 다 `sha256:50311d02...`(v1.1.0).
  세트 시계 s1 `2026-10-30T06:54:00Z`, s2 `2026-10-30T10:04:00Z` -> `--clock-from-set s1`(arm 1 시계, `clocks_differ: true`).
- 팔: commit `270242e288ac20e48644ccf28bc03c79fb426996`, 두 팔 이미지 같음(`sha256:c10689dbc517...`, 로컬 빌드). 정책 sha256 은 arm 1 과 같다.
- verify(run 직전): s1 `pass: true`(`expected_overrides` = arm 1 LINEAGE, `bank_overrides_equal: true`, `bank_config_diff_unexplained: []`), s2 `pass: true`(arm 2 LINEAGE, 같음).
- arm_compare: `--expect-sha base=90443dfa... --expect-sha cand=2c3d04f9... --speed-threshold 0.03 --p95-threshold 0.06`, exit 1(FAIL).

| run | 시작~끝 (KST) | 속도 기하평균 비 [95% CI] | 섞은 p95 비 [95% CI] | 토큰 base / cand | 결과 변경 | 오염 쌍 | arm_compare |
|---|---|---|---|---|---|---|---|
| r1-c2-1 | 19:28:26~19:54:43 | 0.9961 [0.9910, 1.0013] | 1.0021 [0.9845, 1.0285] | 283,085 / **284,327** | 39/40 | 5/120 | **FAIL** (valid, cost regressed) |

- 판정: 속도·섞은 p95 는 ok(차이 없음, A/A 흔들림 안). **비용(토큰)이 +1,242(+0.44%)로 허용 0 을 넘어 FAIL** 이다(쿼리 17개 증가, 22개 감소, 쿼리별 증가 최대 +626, p95 +406).
- 해석: recall 은 미션을 읽지 않는다. 그래서 토큰 차이는 관찰 내용 차이(미션으로 다시 만든 관찰의 글 길이·구성)에서 온다. 관찰 수는 6.8% 줄었지만 dev 40 질의가 돌려받는 관찰 글은 조금 길어졌다.
  재통합 간 흔들림을 잰 적이 없어 이 +0.44% 가 미션 효과인지 재통합 비결정성인지 가를 수 없다(시사만). 문턱 판정은 정본대로 FAIL 로 적는다.
- 참고: base(arm 1 세트) 토큰 283,085 는 arm 1 벤치의 cand(같은 arm 1 세트) 283,083 과 2 다르다. 이번 run 은 공통 시계가 arm 1 세트 시계라(arm 1 벤치는 D10 시계) recall 시각 인자가 달라 생긴 차이로 본다(미검증).
- 결과 변경 39/40 은 설계상 예상된 값이다(관찰이 바뀌는 arm). p99 는 내지 않았다.
- 정리(19:54:45): `docker ps -aq --filter label=hc-bench=1`·`docker volume ls -q --filter label=hc-bench=1`·`docker network ls -q --filter name=hc-bench`·`lsof -nP -iTCP:18780-18799 -sTCP:LISTEN` 모두 0줄, `next-stack.sh status` exit 3.
- 리포 밖 출력: `~/.local/share/hindsight-bench/arch-v2/r1-c2-bench/`, 원본 run `<운영 체크아웃>/bench/.data/runs/r1-c2-1`.

## 9. arm 백업

- `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"; bash scripts/backup.sh` -> `backups/20261030-192709/pg0.tar.gz`(19:27:09 KST, exit 0). 워크트리 `backups/`에 쓰였다(운영 체크아웃 `backups/`에 새 디렉터리 없음 - `ls`로 확인). 곧바로 리포 밖으로 옮기고 워크트리 `backups/`를 지웠다.
- 위치: `~/.local/share/hindsight-bench/arm-backups/r1-c2-mission-20261030-192709/`(0700). `pg0.tar.gz` sha256 **`2c3d04f9ff565f977e7103c76e37c79504ddc7bbdf89ab61a578adda208154b6`**, `LINEAGE`(0600, schema_version 1, arm `r1-c2-mission`).
- LINEAGE 요지: source_backup = D10, settings = 범위 값 그대로 + `memory_defense` + `observations_mission` `{"sha256": "93722a7b...", "len": 415}`, directives [], document_patches 2633,
  reconsolidation 07:39:43Z ~ 10:07:39Z(llm_requests 2122, 생성형 732, 통합 650), pages `-pre`/``, expected_bank_overrides = 백업 직전 override 전체(미션 원문 포함). `-pre`·full 페이지 7개씩 들어 있다.

## 10. 부하 구간 (canary-exclude 형식)

```
2026-10-30 16:35:14 2026-10-30 19:32:33  # #300 R1-c2 hc-next D10 사본(up 16:35:14 ~ down 확인 19:27:33 +300s): -pre 페이지·사전 PATCH 3·문서 PATCH 2633·(iv')+O1 재통합(Opus)·delta 갱신·score 세 경로·(a) 5회·arm 백업
2026-10-30 19:27:41 2026-10-30 19:59:45  # #300 R1-c2 벤치 복제본 s1 arm 1 백업 / s2 arm 2 백업 + 벤치 사이드카(up 19:27:41 ~ down 확인 19:54:45 +300s): run r1-c2-1
```

## 11. 범위 밖 발견

- 양성 표본 기억의 project 관찰이 arm 2 에서는 project 태그 2개 범위(`project:demoproj14`+`project:demo-blog`, 출처 2)의 관찰에 합쳐졌다(arm 1 은 `project:demo-blog` 하나, 출처 1). 통합의 관련 관찰 recall 이 all_strict 상위집합 일치라(`consolidator.py` 324행), 한 project 범위 기억이 그 project 를 포함한 여러 project 범위 관찰에 붙을 수 있다.
  `kind:`·`topic:` combined 는 아니고 `project:*` 범위 안의 합쳐짐이다. 같은 설정에서도 재통합마다 달라질 수 있다(이 표본 1건만 확인, 뱅크 전체 빈도 미측정). 고치지 않는다. 확인 방법: project 범위 관찰 중 태그 2개 이상인 관찰의 출처 기억 project 태그 수 분포.
- `scripts/scope-migrate.py` drain 의 상태 합 검사 경쟁(arm 1 보고서 11절)은 이 arm 에서는 생기지 않았다(poll 간격 60초).
