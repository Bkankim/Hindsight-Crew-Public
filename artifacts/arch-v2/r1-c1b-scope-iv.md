# R1-c1b 범위 arm (iv') - `[["project:*"], ["topic:demo-style*"], ["topic:demo-brand*"]]` 재통합 실측 (issue #300, 상위 #296)

운영자 E4 결정(2026-10-30)의 채택 후보 (iv')를 D10 사본에서 재통합까지 돌려 잰 결과다. 비교 기준은 `artifacts/arch-v2/r1-a-baseline.md`(#297, 재통합하지 않은 D10)와 `artifacts/arch-v2/r1-b-scope.md`(#298, (i) `[["project:*"]]`)다.
이 파일에는 항목 id·판정·해시·집계만 쓴다(정답표 항목 이름, 질의·응답 원문, 개인 경로 없음). 원문은 리포 밖 `~/.local/share/hindsight-bench/arch-v2/`(0700)에만 있다.

## 요약

- 재통합 1회: 2026-10-30 04:31:26Z(표본 3개 사전 PATCH) ~ 06:58:12Z(drain 완료), **2시간 26분 46초**. 문서 2,633개 PATCH, 생성형 통합 호출 **650건**(생성형 전체 733건, `llm-requests` 전체 2,131건). 상한(4시간, 생성형 2,000건)과 arm 2 전 확인선(2시간 40분, 생성형 900건) 모두 안이다.
- 관찰 4,924 -> 3,406, 범위 1,290 -> 309. project 범위 132개(관찰 2,820), `topic:demo-style` 범위 1개(관찰 35), `topic:demo-brand` 범위 1개(관찰 21), project 없는 combined 범위 175개(관찰 530).
- **topic 모델 2개의 관찰이 되살아났다**: 그 태그를 가진 관찰 60·51(D10) -> 1·0((i)) -> **35·21**((iv')). recall 관찰 48·51 -> 1·0 -> 29·21.
  `kind:` + project 필터 관찰 recall 은 (i)과 같이 0·0 이다(설계상 예상).
- 사전 확인 (a) 접두어 충돌 없음(두 태그만 걸림). (b) 범위 동작: 대조 2개는 project 범위만, 양성 1개는 사전 PATCH 직후 기존 combined 관찰에 과도기 흡수됐고(코디네이터 판단 A: 진행), 전수 PATCH 뒤에는 project 범위 관찰 1개 + `topic:demo-style` 범위 관찰 1개로 기대와 같다.
- 회귀 세 경로 모두 **불합격**(invalid 0). 끝난 항목 open: (a) 3회 반복 3·2·1/15(합 6/45, 대조군 9/9), (b) delta 2/15·full 3/15(대조군 3/3), (c) 2/15(대조군 2/3, 판정이 D10 기준선과 같다).
- operations 실패·취소: 기준선 실패 40(통합 0)·취소 0 -> drain 뒤 실패 40(통합 0)·취소 0. 통합 실패·취소가 늘지 않아 이 arm 은 완료다.
- 벤치(동시 두 세트, 한 run): **FAIL**(유효 run). 속도 비 1.0308(문턱 3.0%를 0.08%p 넘음, CI [0.9936, 1.0536]), 섞은 p95 비 1.0400(문턱 6.0% 안), 토큰 295,475 -> 283,083(-4.2%). (i)은 1.0285 PASS 였다(8절).

## 1. 사본과 복원원

- up 전: `bash scripts/next-stack.sh status` -> `hc-next: no resources`, exit 3. `docker ps -aq --filter label=hc-bench=1` -> 0줄.
- up(2026-10-30 13:26:21~13:26:48 KST): `HC_NEXT_BACKUP_SHA256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4 HC_NEXT_BACKUP_DIR=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001 HC_NEXT_REQUIRED_BANK=personal-demo bash scripts/next-stack.sh up --llm opus` exit 0.
- 복원 줄 원문(홈 경로만 바꿈):

```
restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4
pg_verifybackup ok
up ok: commit=69d7999140af276691bffd15e1236fdfe4a29a66 gateway=http://127.0.0.1:18888 ui=http://127.0.0.1:9998 llm=opus
```

- sha256 이 D10 값과 같다. 복원 직후 stats: documents 2,633, observations 4,924(world 1,916, experience 719), pending_consolidation 0. 재통합 전 probe(6절)가 #298·#299 재통합 전 값과 모두 같다(관찰 4,924, 범위 1,290, 원문 2,635, project 없는 원문 239).
- down: `bash scripts/next-stack.sh down` -> `down ok`, exit 0(16:10:22), 직후 `status` exit 3, `docker ps -aq --filter name=hc-next`·`docker volume ls -q --filter name=hc-next` 0줄(16:10:22).

### operations 실패·취소 기준선과 drain 뒤

- 방법: `GET .../operations` 전체를 나열해 (operation_type, status)로 센다(읽기만, 리포 밖 스크래치 스크립트).

| 시점 | 전체 | 실패 | 취소 | 통합(consolidation*) 실패 | 통합 취소 |
|---|---|---|---|---|---|
| 기준선(up 직후, 13:27 KST) | 6,270 | 40 (batch_retain 18, retain 18, refresh_mental_model 4) | 0 | 0 | 0 |
| drain·채점 뒤(16:09 KST) | 6,314 | 40 (같음) | 0 | 0 | 0 |

- 통합 실패·취소가 늘지 않았다. 그 밖의 operation 의 새 실패·취소도 없다(refresh_mental_model completed 405 -> 422).

## 2. 적용 설정

- 워크트리 gitignore 오버레이 `templates/bank-config/local/personal-demo.json` = 운영 오버레이(`memory_defense`) 복사 + arm 키(사적 값 없음, 원문 그대로):

```json
{"version": "1", "bank": {"memory_defense": {"enabled": false, "rules": []}, "default_observation_scopes": [["project:*"], ["topic:demo-style*"], ["topic:demo-brand*"]]}}
```

- `HC_TARGET=next python3 -B scripts/bank-config.py diff --bank personal-demo` -> `change default_observation_scopes: null -> [["project:*"], ["topic:demo-style*"], ["topic:demo-brand*"]]`, `same memory_defense`, exit 3.
  `apply`(04:27:45Z) -> `patched: default_observation_scopes`, 다시 읽어 `same default_observation_scopes`·`same memory_defense`, exit 0. 적용 직후 pending_consolidation 0(설정 적용만으로 통합이 시작되지 않는다).
- 적용 뒤 뱅크 override 전체(LINEAGE `expected_bank_overrides`): `{"default_observation_scopes": [["project:*"], ["topic:demo-style*"], ["topic:demo-brand*"]], "memory_defense": {"enabled": false, "rules": []}}`.
- 지시문 0개, 미션 null.

## 3. 사전 확인

### (a) 접두어 충돌

- 방법: upstream tags API `GET /v1/default/banks/personal-demo/tags?limit=500`(읽기만, 페이지 1개에 전체 444개)에서 `topic:demo-style`·`topic:demo-brand`으로 시작하는 태그를 모두 골랐다. 설정 적용 전(13:27 KST)과 직후 두 번 같은 결과.
- 결과: `[{"tag": "topic:demo-style", "count": 87}, {"tag": "topic:demo-brand", "count": 64}]`. 두 태그 말고 걸린 태그가 없다(멈추지 않음). count 는 관찰을 포함한 기억 수다.
  원문 기억 기준(원문 2,635개 나열)으로는 `topic:demo-style` 27개(project 없음 1), `topic:demo-brand` 13개(project 없음 0)로 #299 값과 같다.

### (b) 표본 3개 범위 동작

- 표본 고르기(읽기만): 문서 2,633개 중 project 태그가 정확히 하나이고 memory unit 1개인 문서를 `created_at` 내림차순으로 정렬해 역할별 첫 문서를 골랐다(후보 수 양성 20, 대조-다른 topic 1,572, 대조-topic 없음 529).
  대조 하나는 다른 `topic:` 태그를 가진 문서로 골라 glob 이 무관한 topic 태그에 붙지 않는지도 함께 봤다.

| 역할 | 문서 id | memory unit id | project 태그 | `topic:demo-style` | `topic:demo-brand` | 다른 topic |
|---|---|---|---|---|---|---|
| 양성 | `ff76b043-ad73-4da0-9f98-9c45b3ce8b33` | `31cd9b4b-a22c-41ed-bec9-357ac2ccffaf` | 1 (`project:demo-blog`) | 있음 | 없음 | 없음 |
| 대조 1 | `0889e591-9e67-4ecb-8669-68b2c6cd2771` | `af18ada7-4485-4e94-b6de-956dcfc374dc` | 1 (`project:demoproj14`) | 없음 | 없음 | 1개 |
| 대조 2 | `581ef3e3-a219-44cf-8160-dc33a24b658e` | `59815630-3dfa-43d7-a435-2694a900a37e` | 1 (`project:demoproj13`) | 없음 | 없음 | 없음 |

- 사전 PATCH: 위 순서로 upstream 직결 `PATCH /v1/default/banks/personal-demo/documents/<id>` `{"observation_scopes": [["project:*"], ["topic:demo-style*"], ["topic:demo-brand*"]]}`(04:31:26.61Z~04:31:26.80Z, 3건 모두 `success: true`, 다시 읽은 `observation_scopes`가 보낸 값과 같다).
- drain: `scripts/scope-migrate.py drain --bank personal-demo --since 2026-10-30T04:31:26Z --interval 15 --timeout 1800 --max-llm 2000` -> `drain: drained after 61.6s, 70 LLM requests since 2026-10-30T04:31:26Z (generative 31, generative consolidation 4, local embedding 25), 4923 observations in 1291 scopes`.
  첫 두 실행은 `the five operation states add up to 6279 but the operations total is 6282 (fail-closed)`로 멈췄다. 통합 직후 `-pre` 페이지 refresh 작업 3개가 생긴 순간 stats 의 상태 합이 operations 목록 total 보다 늦게 따라왔다(바로 뒤 stats 합 6,282 = total). 같은 `--since`로 다시 돌려 위 줄을 얻었다(11절 범위 밖 발견).
- 범위 확인: `GET .../memories/list?type=observation&time_field=updated_at&start_date=2026-10-30T04:31:26Z`로 PATCH 뒤 바뀐 관찰 5개를 나열하고, 각 관찰을 `GET .../memories/{id}`로 읽어 `source_memory_ids`와 태그를 대조했다. 5개 모두 표본 기억이 출처다.

| 관찰 id | 출처 표본 | 출처 수 | 관찰 태그 |
|---|---|---|---|
| `66bad9d0` | 대조 2 | 1 | `project:demoproj13` |
| `0f7b11ca`, `66c9bbf2`, `919d667b` | 대조 1 | 1 | `project:demoproj14` |
| `8f587896` | 양성 | 3 | `topic:demo-style`, `project:demo-blog`, `kind:completion`, `project:demoproj14`, `user:demo` |

- 판정: 대조 2개의 관찰은 project 범위만이다(topic 범위·combined 범위 없음). 다른 topic 태그(대조 1)에 glob 범위가 생기지 않았다.
- 양성은 **과도기 흡수**다(코디네이터 판단 A: 잘못된 범위 신호 아님, 진행). `8f587896`은 새로 생긴 범위 관찰이 아니라 2026-10-18 에 통합된 기존 combined 관찰이다.
  출처 3개 중 2개(`de0fb7f7`, `9a3af03c`)는 아직 PATCH 되지 않은 combined 문서(`observation_scopes` null)의 기억이고, 나머지 1개가 양성 기억이다. 태그는 그 combined 출처의 태그 묶음 그대로다(양성 기억의 `kind:decision`은 붙지 않았다).
  - 코드 근거(upstream v1.1.0 이미지 `/app/api/hindsight_api/engine/consolidation/consolidator.py`): 통합의 관련 관찰 recall 은 범위 태그가 있으면 `tags_match = "all_strict"`다(324행). 범위 패스마다 `obs_tags_override`를 관찰 범위 태그로 쓴다(2486행~). all_strict 는 상위집합 일치라 `project:demo-blog`·`topic:demo-style`를 모두 가진 combined 관찰이 두 범위 패스 모두에서 후보가 된다.
  - 이 관찰은 전수 PATCH 때 나머지 출처 문서가 이전되면 지워지고 공동 출처가 다시 큐에 들어간다(`engine/memories/pg/documents.py` 575행~: 이동한 기억이 출처인 관찰 삭제 + 공동 출처 `consolidated_at = NULL`).
  - 양성의 순수 project 범위·`topic:demo-style` 범위 관찰은 사전 PATCH 단계에서 **생기지 않았다**(기록만, 스펙대로 계속).
- 전수 PATCH·drain 뒤 사후 확인(코디네이터 조건 [2], 07:00 KST, 관찰 3,406개 중 표본 태그와 겹치는 577개를 읽어 `source_memory_ids` 대조):
  - `8f587896`은 **지워졌다**(관찰 목록에 없음). 다시 만들어진 양성 기억의 관찰은 `d40f7509`(`topic:demo-style`, 출처 1)과 `71f0d7e5`(`project:demo-blog`, 출처 1) 두 개다. project 범위 + `topic:demo-style` 범위로 기대와 같다.
  - 대조 관찰 4개는 그대로 project 태그만이다. 표본 기억을 출처로 가진 관찰 중 combined 모양(project 2개 이상, 또는 project + topic + kind 묶음)은 **남지 않았다**.

## 4. 문서 PATCH

- `scripts/scope-migrate.py migrate --bank personal-demo --scopes '[["project:*"], ["topic:demo-style*"], ["topic:demo-brand*"]]'`(dry-run) -> `targets: 2633 documents in 53 batches of up to 50`, PATCH 0.
- `... --apply --batch-size 200 --batch-sleep 0`(04:35:39Z~04:39:23Z) -> 14 배치, `2633/2633 done`, exit 0. **PATCH 대상 문서 2,633개**(필터 없음, 사전 PATCH 3개 포함 - 같은 값 재전송).

## 5. 재통합 비용

- 구간: 사전 PATCH **04:31:26Z** ~ drain 완료 **06:58:12Z**(KST 13:31:26~15:58:12), **2시간 26분 46초**. 재통합 시작은 사전 PATCH 시각이다(설정 적용은 통합을 시작하지 않았다).
- drain 감시: `scripts/scope-migrate.py drain --bank personal-demo --since 2026-10-30T04:31:26Z --interval 60 --timeout 14340 --max-llm 2000 --timeseries <스크래치>/drain1.jsonl`(재시도 포장 스크립트, 이 구간에는 재시도 없음). 137 poll, 매 poll 다섯 상태 합 = operations total.
  마지막 줄: `drain: drained after 8314.5s, 2131 LLM requests since 2026-10-30T04:31:26Z (generative 733, generative consolidation 650, local embedding 1353), 3406 observations in 309 scopes`, exit 0.

| 구분 (`llm-requests`, operation / scope / provider) | 건수 |
|---|---|
| consolidation / consolidation / claude-code | 649 |
| consolidation_dedup / consolidation_dedup / claude-code | 1 |
| consolidation / embedding_query, embedding_document / local | 649 + 649 |
| refresh_mental_model(`-pre` 페이지 delta 갱신) / reflect, reflect_tool_call / claude-code | 11 + 59 |
| mental_model_delta_ops / delta_ops, retraction_ops / claude-code | 10 + 3 |
| refresh_mental_model / rerank(tei), embedding_query·embedding_document(local) | 45 + 45 + 10 |
| **합계** | **2,131**(생성형 733, 그중 통합 650) |

- 토큰(claude-code, 같은 구간, 페이지 갱신 포함): 입력 10,066,516, 출력 2,216,676, 캐시 3,511,426.
- 시계열(drain poll 발췌, UTC, 범위 수는 `--scope-every 5` 행):

| 시각 | 관찰 | pending_consolidation | 생성형 통합 | 전체 llm-requests | 범위 수 |
|---|---|---|---|---|---|
| 04:39:37 | 593 | 2,369 | 45 | 194 | 188 |
| 04:54:44 | 1,023 | 2,075 | 131 | 452 | 228 |
| 05:14:56 | 1,434 | 1,769 | 210 | 689 | 247 |
| 05:40:23 | 1,911 | 1,362 | 303 | 964 | 259 |
| 06:00:52 | 2,396 | 975 | 412 | 1,291 | 285 |
| 06:21:17 | 2,802 | 569 | 506 | 1,577 | 294 |
| 06:41:44 | 3,192 | 222 | 592 | 1,833 | 304 |
| 06:58:12 | 3,406 | 0 | 650 | 2,131 | 309 |

- 처리 속도: 원문 약 17.9개/분(15.7~18.2), 생성형 통합 1건당 원문 약 4.0개. (i)(#298)의 약 20개/분·618건과 비슷하다. (iv')의 추가 범위(topic 2개, 원문 40개)는 생성형 통합을 32건(+5.2%) 더 썼다(#299 추정 640~650건과 맞다).
- 관찰·범위 전후(관찰 전체를 `GET .../memories/list?type=observation`으로 나열해 태그 묶음을 센 값, #298 과 같은 방식):

| | 재통합 전(D10) | (i) 재통합 뒤(#298) | **(iv') 재통합 뒤** |
|---|---|---|---|
| 관찰 | 4,924 | 3,373 | **3,406** |
| 관찰 범위(서로 다른 태그 묶음) | 1,290 | 307 | **309** |
| 관찰 1개인 범위 | 505 | 79 | **81** |
| project 범위 / 관찰 | - | 131 / 2,842 | **132 / 2,820** |
| `topic:demo-style` 범위 / 관찰 | - | - | **1 / 35** |
| `topic:demo-brand` 범위 / 관찰 | - | - | **1 / 21** |
| project 없는 combined 범위 / 관찰 | 176 / 530 | 176 / 531 | **175 / 530** |
| `kind:` 태그가 있는 범위 / 관찰 | 1,290 / 4,924 | 176 / 531 | **175 / 530** |
| project 없는 관찰 | 530 | 531 | **586**(combined 530 + topic 56) |

  (i) 열의 project 범위 수·관찰은 #298 표의 범위 307·관찰 3,373 에서 combined 176·531 을 뺀 값이다.
- combined 관찰은 (i)의 531 에서 530 으로 1개 줄었다. 이슈 본문 예상(두 topic 태그를 가진 project 없는 원문만큼 준다)과 같은 방향이고, 그런 원문은 `topic:demo-style` 1개뿐이다(`topic:demo-brand`은 0개). 수만 기록한다.

## 6. 회귀 세 경로 (#297 고정 세트 18건)

- 고정 세트: `~/.local/share/hindsight-bench/arch-v2/r1-fixed-set.json`, sha256 `3fbe114b588af11e154b343639b8d5dcd9efe4b3c49343fb91559afbdd9d904d`(#297 값과 같다). evidence 는 다시 만들지 않았다.
- 순서(bench/README.md "(b) 페이지 이름·읽기 전용 모드와 delta 갱신"):
  1. `score --paths b --page-suffix -pre`(13:27:51~13:31:15 KST, 재통합 전 `-pre` 페이지 7개, exit 0)
  2. 사전 PATCH·전수 PATCH·drain(3~5절)
  3. `stats --wait 3600 --wait-pages`(15:58:28~15:58:51) -> 06:58:47Z 연속 2회 `pages_stale: []`, `refresh_live: 0`, exit 0
  4. `score --paths b --read-pages --page-suffix -pre`(15:58:56~15:59:09, POST 0회, LLM 0회) = **delta**
  5. `score`(15:59:11~16:05:47, 접미사 없는 새 페이지 7개 + reflect + recall) = **full** 과 (a)(c) 1회째
  6. `score --paths a` 두 번 더(16:05:47~16:07:51, 16:07:51~16:09:32) = (a) 2·3회째
- 리포 밖 출력: `score-20261030-132751/`(pre), `score-20261030-155856/`(delta), `score-20261030-155912/`(full·a 1회·c), `score-20261030-160547/`(a 2회), `score-20261030-160751/`(a 3회).

| id | (a) 기준선 | (a) (i) | (a) arm 1회 | (a) arm 2회 | (a) arm 3회 | (b) 기준선 | (b) (i) delta | (b) (i) full | (b) pre | (b) delta | (b) full | (c) 기준선 | (c) (i) | (c) arm |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| K01 | not-open | open | open | open | open | open | open | not-open | open | open | open | open | not-open | open |
| K02 | not-open | not-open | open | not-open | not-open | not-open | open | not-open | open | open | not-open | not-open | not-open | not-open |
| K03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K04 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | open | not-open | not-open | not-open | not-open | not-open |
| K05 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | open | open | open |
| K06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K08 | not-open | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | open | not-open |
| K09 | not-open | not-open | not-open | not-open | not-open | open | not-open | open | not-open | not-open | open | not-open | open | not-open |
| K10 | not-open | not-open | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | open | not-open | not-open | not-open |
| K11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K12 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K14 | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K15 | not-open | not-open | not-open | open | not-open | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K16 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| C01 | open | open | open | open | open | open | open | open | open | open | open | not-open | not-open | not-open |
| C02 | not-open | open | open | open | open | open | open | open | open | open | open | open | open | open |
| C03 | not-open | open | open | open | open | open | open | open | open | open | open | open | open | open |

| 경로 | 끝난 항목 open (기준선 -> (i) -> (iv')) | 대조군 open (기준선 -> (i) -> (iv')) | invalid | deleted_evidence | 질의·소요·LLM | 판정 |
|---|---|---|---|---|---|---|
| (a) reflect 나열, 3회 | 1/15 -> 2/15 -> 3·2·1/15 (합 6/45, K01 3회, K02·K10·K15 각 1회) | 1/3 -> 3/3 -> 3/3 x 3 | 0 | - | 회당 1회, 124.8·123.7·100.9초, 6·6·5건 | 불합격 |
| (b) delta(`-pre` 갱신본) | 3/15 -> 2/15 -> 2/15 (K01, K02) | 3/3 -> 3/3 -> 3/3 | 0 | 0 | 7회, 12.4초, 0건(읽기만) | 불합격 |
| (b) full(재통합 뒤 새 페이지) | 3/15 -> 1/15 -> 3/15 (K01, K09, K10) | 3/3 -> 3/3 -> 3/3 | 0 | 0 | 7회, 187.8초, 130건 | 불합격 |
| (c) recall 상태 | 2/15 -> 3/15 -> 2/15 (K01, K05) | 2/3 -> 2/3 -> 2/3 | 0 | - | 18회, 82.5초, 35건 | 불합격 |

- 합격 조건(끝난 항목 open 0, invalid 0, 대조군 3/3)을 세 경로 모두 못 채운다. invalid 가 0 이라 "판정 불가"는 없다.
- (b) pre(재통합 전 첫 생성)는 3/15 (K01, K02, K04)였고 delta 갱신 뒤 2/15 (K01, K02)다. (b) 첫 생성은 같은 D10 에서 0~3/15 를 오간다(`r1-c1-o2.md` 7절, 이 arm 의 pre 3/15 를 더해도 범위 안). 그래서 (i) full 1/15 와 (iv') full 3/15 의 차이를 범위 설정 효과로 읽지 않는다.
- (c)는 판정이 D10 기준선과 같다(K01, K05). (i)에서 open 이던 K08·K09 가 not-open, (i)에서 not-open 이던 K01 이 open 이다. (c)는 같은 사본·같은 설정에서 결정적이지만(`r1-c3-read.md` 3절), 재통합 두 번 사이의 흔들림은 잰 적이 없다. 차이는 범위 설정 차이와 재통합 LLM 비결정성이 섞여 있다.
- (a)는 3회 모두 대조군 3/3 을 잡았다. K01 이 3회 모두 open 이다. 1회 응답 흔들림은 `r1-c3-read.md` 3절(base 6 run 끝난 항목 open 0~3)과 같은 폭이다.
- 해석 주의(스펙 #296): 기준선은 재통합하지 않은 D10 이라, 기준선과의 차이는 설정 효과와 재통합 자체(v1.1.0, Opus) 효과가 섞여 있다.

## 7. 태그 필터 recall·topic 모델 영향

- 방법: #298·#299 와 같은 probe(같은 질의문·태그·`tags_match: all_strict`·`budget: mid`·`max_tokens: 8192`·`query_timestamp=2026-10-28T14:59:59Z`, upstream REST `POST .../memories/recall`, `types: ["observation"]`, 읽기만, #299 워커가 남긴 probe 스크립트 그대로)를 재통합 전(13:27 KST)과 delta 채점 뒤(16:00 KST)에 보냈다.
  "뱅크 일치"는 관찰 전체 나열에서 그 태그를 모두 가진 관찰 수다. topic 모델 1·2 의 태그는 각각 `topic:demo-style`, `topic:demo-brand` 하나다.

| 질의 | D10(재통합 전) recall / 뱅크 일치 | (i) 완료 뒤(#298) | **(iv') 완료 뒤** |
|---|---|---|---|
| `kind:completion` + `project:hindsight-crew` | 41 / 260 | 0 / 0 | **0 / 0** |
| `kind:decision` + `project:hindsight-crew` | 34 / 151 | 0 / 0 | **0 / 0** |
| topic 모델 1(`topic:demo-style`) | 48 / 60 | 1 / 1 | **29 / 35** |
| topic 모델 2(`topic:demo-brand`) | 51 / 51 | 0 / 0 | **21 / 21** |

- topic 모델 2개: 그 태그 범위의 관찰이 되살아났다(35·21개). D10 의 60·51 보다 적다. D10 은 combined 범위 관찰이 기억의 태그를 모두 물려받아 같은 원문이 여러 묶음 관찰로 나뉘었고, (iv')는 그 태그 원문 27·13개를 한 범위로 모아 통합한다.
- `kind:` 필터 관찰 recall: (i)과 같이 0·0 이다(범위 설계상 예상 - project·topic 범위 관찰은 `kind:` 태그를 물려받지 않는다). 고치지 않는다(게이트웨이 쪽 대응은 #304 범위).
- project 없는 combined 관찰: 530(D10) -> 531((i)) -> 530((iv')), 5절.

## 8. 벤치 (동시 두 세트, 한 run)

- 명령: `bench/README.md` "1단계 arm 벤치" 절 그대로(리포 밖 스크래치 스크립트로 묶어 실행). 벤치 사이드카 `r1`(18781) 공유, s1 = D10, s2 = arm 백업(`--expected-overrides <LINEAGE>`),
  팔 base(s1, 18891)·cand(s2, 18892) 모두 `gw-up --ref 270242e --local-policies <운영 체크아웃>/policies/local`.
  up 전 `next-stack.sh status` exit 3, `docker ps -aq --filter label=hc-bench=1` 0줄. 워크트리에 운영 체크아웃 `bench/.data`·`audit`·`secrets` 심링크를 달았다가 끝나고 지웠다.
- 세트: s1 `backup_sha256` = `8a4900ea913d...`(D10), s2 = `90443dfa7a06...`(LINEAGE `backup.pg0_sha256`과 같다). 이미지 둘 다 `sha256:50311d02...`(v1.1.0).
  세트 시계 s1 `2026-10-26T19:30:00Z`, s2 `2026-10-30T06:54:00Z` -> `--clock-from-set s1`(`bench_clock_common.clocks_differ: true`).
- 팔: commit `270242e288ac20e48644ccf28bc03c79fb426996`, 두 팔 이미지 같음(`sha256:1ee18bf72b0a...`, 이번에 로컬 빌드. ref 는 #297·#298 과 같다).
  정책 sha256: `bank-policies.json` `0587ecbb315e...`, `context-triggers.json` `8ed1ebaa4e27...`(#297 과 같다).
- verify(run 직전): s1 `pass: true`(기대 override 없음, `bank_overrides_equal: true`), s2 `pass: true`(`expected_overrides` = LINEAGE, `bank_overrides_equal: true`, `bank_config_diff_unexplained: []`).
- arm_compare: `--expect-sha base=<D10> --expect-sha cand=90443dfa... --speed-threshold 0.03 --p95-threshold 0.06`, exit 1(FAIL).

| run | 시작~끝 (KST) | 속도 기하평균 비 [95% CI] | 섞은 p95 비 [95% CI] | 토큰 base / cand | 결과 변경 | 오염 쌍 | arm_compare |
|---|---|---|---|---|---|---|---|
| r1-c1b-1 | 16:11:34~16:34:27 | **1.0308** [0.9936, 1.0536] | 1.0400 [1.0200, 1.0538] | 295,475 / 283,083 | 40/40 | 3/120 | **FAIL** (valid, speed regressed) |

- 판정: 속도 점추정 1.0308 이 문턱 1.03 을 0.08%p 넘어 `speed: regressed`, 전체 FAIL 이다. 섞은 p95 는 4.0%(문턱 6.0% 안), 토큰은 -4.2%(쿼리 36개 감소, 4개 증가, 쿼리별 증가 최대 +456, p95 +23)로 ok.
- 해석: (i)(#298)은 같은 방식으로 1.0285(PASS, 문턱 경계)였다. 두 범위 arm 모두 cand 가 약 3% 느리고, A/A 흔들림(0.983~1.007, `r1-a-baseline.md` 7절) 바깥이다. 1 run 의 CI 는 1 을 포함한다.
  (iv')와 (i)의 차이(1.0308 대 1.0285)는 run 사이 흔들림(A/A sd 0.0098)보다 작아 두 범위 설정의 속도 차이로 읽지 않는다. 문턱 판정은 정본대로 FAIL 로 적고, 재측정 여부는 코디네이터 판단이다(스펙상 1 run).
- 결과 변경 40/40 은 설계상 예상된 값이다(관찰이 바뀌는 arm). p99 는 내지 않았다(참고용).
- 정리(16:34:30): `docker ps -aq --filter label=hc-bench=1`·`docker volume ls -q --filter label=hc-bench=1`·`docker network ls -q --filter name=hc-bench`·`lsof -nP -iTCP:18780-18799 -sTCP:LISTEN` 모두 0줄, `next-stack.sh status` exit 3.
- 리포 밖 출력: `~/.local/share/hindsight-bench/arch-v2/r1-c1b-bench/`(up·gw·verify·runner·compare), 원본 run `<운영 체크아웃>/bench/.data/runs/r1-c1b-1`.

## 9. arm 백업

- `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"; bash scripts/backup.sh` -> `backups/20261030-161002/pg0.tar.gz`(16:10:02 KST, exit 0).
  워크트리에서 실행해 워크트리 `backups/`에 쓰였다(운영 체크아웃 `backups/`에는 생기지 않았다 - 전후 `ls`로 확인). 곧바로 리포 밖으로 옮기고 워크트리 `backups/`를 지웠다.
- 위치: `~/.local/share/hindsight-bench/arm-backups/r1-c1b-iv-20261030-161002/`(0700). `pg0.tar.gz` sha256 **`90443dfa7a0660559c08c245014a73231786b897fac5f3dd5449e1e509d47a58`**, `LINEAGE`(0600, schema_version 1, arm `r1-c1b-iv`).
- LINEAGE 요지: source_backup = D10(sha256 `8a4900ea...`), settings 2절, directives [], document_patches 2633(사전 PATCH 3 포함), reconsolidation 04:31:26Z ~ 06:58:12Z(llm_requests 2131, 생성형 733, 통합 650),
  pages `-pre`/``(full), expected_bank_overrides 2절. 이 백업에는 `-pre` 페이지 7개와 full 페이지 7개가 들어 있다.

## 10. 부하 구간 (canary-exclude 형식)

```
2026-10-30 13:26:21 2026-10-30 16:15:22  # #300 R1-c1b hc-next D10 사본(up 13:26:21 ~ down 확인 16:10:22 +300s): -pre 페이지·사전 PATCH 3·문서 PATCH 2633·(iv') 재통합(Opus)·delta 갱신·score 세 경로·(a) 반복·arm 백업
2026-10-30 16:10:48 2026-10-30 16:39:30  # #300 R1-c1b 벤치 복제본 s1 D10 / s2 arm 백업 + 벤치 사이드카(up 16:10:48 ~ down 확인 16:34:30 +300s): run r1-c1b-1
```

## 11. 범위 밖 발견

- `scripts/scope-migrate.py` drain 의 다섯 상태 합 검사(`poll`, 194~211행, 고치기 전 181~197행)는 stats `operations_by_status`와 operations 목록 `total`을 따로 읽는다. 통합 직후 페이지 refresh 작업이 생기는 순간 stats 쪽이 늦어 합이 모자라 fail-closed(exit 1)로 끝난다(이 arm 사전 PATCH drain 에서 2회, 같은 `--since`로 다시 돌리면 통과). 코디네이터 재작업(rework #300-1)으로 이 PR 에서 고쳤다(df012ed: 합계만 어긋나면 같은 poll 안에서 2초 간격으로 최대 3회 다시 읽고, 3회 모두 어긋나면 같은 문구로 fail-closed). 이 arm 측정은 고치기 전 도구와 재시도 포장 스크립트로 했다.
