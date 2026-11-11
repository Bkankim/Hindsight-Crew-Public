# R1-b 범위 arm - `default_observation_scopes=[["project:*"]]` 재통합 실측 (issue #298, 상위 #296)

O2 후보 (i)을 D10 사본에서 재통합까지 돌려 잰 결과다. 비교 기준은 `artifacts/arch-v2/r1-a-baseline.md`(#297, 재통합하지 않은 D10)다.
이 파일에는 항목 id·판정·해시·집계만 쓴다(정답표 항목 이름, 질의·응답 원문, 개인 경로 없음). 원문은 리포 밖 `~/.local/share/hindsight-bench/arch-v2/`(0700)에만 있다.

## 요약

- 재통합 1회: 2026-10-29 19:46:39Z ~ 21:53:13Z(2시간 6분 34초), 문서 2,633개 PATCH, 생성형(claude-code) 호출 666건(통합 618건), 전체 `llm-requests` 1,957건. 상한(4시간, 생성형 2,000건) 안이다.
- 관찰 4,924 -> 3,373, 관찰 범위 1,290 -> 307(관찰 1개인 범위 505 -> 79). project 없는 범위 176개는 그대로 combined 다.
- 회귀 세 경로 모두 **불합격**(invalid 0). 끝난 항목 open: (a) 1 -> 2, (b) full 3 -> 1, (b) delta 2(재통합 전 같은 페이지 2와 같음), (c) 2 -> 3. 대조군 open: (a) 1/3 -> 3/3, (b) 3/3 유지, (c) 2/3 유지.
- 벤치(동시 두 세트, 한 run): **PASS**. 속도 비 1.0285(문턱 3.0%), 섞은 p95 비 1.0413(문턱 6.0%), 토큰 295,475 -> 284,530(-3.7%).
- 부작용: 범위가 `project:` 태그만 남겨, project 가 있는 관찰에서 `kind:`·`topic:` 태그가 사라졌다. `kind:` + project 필터 관찰 recall 은 41·34건 -> 0·0건, topic 단일 태그 모델 2개가 읽는 관찰은 60·51개 -> 1·0개다.
- #299 권고: (ii)를 잰다. topic 모델 가시성을 되살리는 후보는 (ii)뿐이다. 다만 비용이 (i)의 약 1.67배(생성형 약 1,100건, 약 3시간 30분)로 4시간 상한에 가깝다. (iii)은 추정만 하고, 추정상 효과가 project 없는 원문 239개(9.1%)에 그친다.

## 1. 사본과 복원원

- up(2026-10-30 04:42:19~04:42:45 KST): `HC_NEXT_BACKUP_SHA256=8a4900ea... HC_NEXT_BACKUP_DIR=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001 HC_NEXT_REQUIRED_BANK=personal-demo bash scripts/next-stack.sh up --llm opus` exit 0. up 전 `next-stack.sh status` exit 3, `docker ps -aq --filter label=hc-bench=1` 빈 출력.
- 복원 줄 원문(홈 경로만 바꿈):

```
restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4
pg_verifybackup ok
up ok: commit=39d4346c7c18e6b20fba10038bba9228ebdb9def gateway=http://127.0.0.1:18888 ui=http://127.0.0.1:9998 llm=opus
```

- sha256 이 D10 값과 같다. 복원 직후: documents 2,633, observations 4,924, 원문 2,635(world 1,916, experience 719), pending_consolidation 0.
- down: `bash scripts/next-stack.sh down` exit 0(07:01:34), 직후 `status` exit 3, `docker ps -aq --filter name=hc-next`·`docker volume ls -q --filter name=hc-next` 빈 출력(07:01:35).

## 2. 적용 설정

- 워크트리 gitignore 오버레이 `templates/bank-config/local/personal-demo.json` = 운영 오버레이 복사 + arm 키(사적 값 없음, 원문 그대로):

```json
{"version": "1", "bank": {"memory_defense": {"enabled": false, "rules": []}, "default_observation_scopes": [["project:*"]]}}
```

- `HC_TARGET=next python3 -B scripts/bank-config.py diff --bank personal-demo` -> `change default_observation_scopes: null -> [["project:*"]]`, `same memory_defense`, exit 3.
  `apply` -> `patched: default_observation_scopes`, 다시 읽어 `same` 둘, exit 0.
- 적용 뒤 뱅크 override 전체(LINEAGE `expected_bank_overrides`): `{"default_observation_scopes": [["project:*"]], "memory_defense": {"enabled": false, "rules": []}}`.
- 지시문 0개(이 arm 은 지시문을 쓰지 않는다).

## 3. 문서 PATCH

- `scripts/scope-migrate.py migrate --bank personal-demo --scopes '[["project:*"]]'`(dry-run) -> `targets: 2633 documents in 53 batches of up to 50`, PATCH 0.
- `... --apply --batch-size 200 --batch-sleep 0`(19:46:46Z~19:50:56Z) -> 14 배치, `2633/2633 done`, exit 0. **PATCH 대상 문서 2,633개**(필터 없음, 뱅크 문서 전부).
- 같은 값을 다시 보내면 upstream 은 아무것도 지우지 않고 큐에 넣지 않으므로(observations.mdx "Changing the Scopes") 중간에 끊긴 실행은 그대로 다시 돌리면 된다.

## 4. 재통합 비용

- 구간: `bank-config.py apply` 직전 **19:46:39Z** ~ drain 완료 **21:53:13Z**(KST 04:46:39~06:53:13), 2시간 6분 34초.
- drain 감시: `scripts/scope-migrate.py drain --since 2026-10-29T19:46:39Z --interval 60`. 매 poll 마다 다섯 상태 합 = operations total 을 확인했다(위반 0회, 마지막 `{"pending": 0, "processing": 0, "completed": 6269, "failed": 40, "cancelled": 0}` 합 6,309 = total 6,309). failed 40 은 D10 에 원래 있던 값이다.
  마지막 줄: `drain: drained after 6645.6s, 1957 LLM requests since 2026-10-29T19:46:39Z (consolidation 1851), 3373 observations`.
  첫 감시는 상한을 전체 요청 수로 세고 있어 20:02Z 에 멈추고 다시 띄웠다(아래 상한 해석). 두 시계열은 이어진다.
  그 뒤 rework #298-1 로 `drain --max-llm`이 생성형 통합 호출(`operation=consolidation`에서 provider `local`·`tei` 행을 뺀 수)만 세도록 고쳤다(옛 동작은 `--count-all`).
  시계열에는 전체·로컬 임베딩·리랭크·생성형·통합 전체·생성형 통합 수가 모두 남는다. 이 run 의 시계열은 고치기 전 도구로 남긴 것이라 전체·통합 수만 있고, 생성형 수는 아래 표의 별도 집계다.
- 이 run 시계열의 한계(rework #298-2): 이 run 의 drain 시계열에는 **범위 수가 없다**. 범위 수는 재통합 전후 값(아래 표, 1,290 -> 307)만 있다. 생성형 통합 수도 시계열에는 없고, drain 완료 시점 `llm-requests` 행 전체를 operation·provider 로 나눈 **별도 집계(618 = consolidation 617 + consolidation_dedup 1)** 로 냈다.
  도구는 그 뒤 고쳤다. drain 행이 `--scope-every` 회마다(기본 5, 마지막 poll 포함) 범위 수(관찰 전체의 서로 다른 태그 묶음 수, 아래 표와 같은 방식)를 세고, 생성형 통합 수를 provider 기준(`local`·`tei`가 아닌 행 중 operation 이름이 consolidation 으로 시작하는 것)으로 센다.
  같은 drain 완료 시점 집계를 고친 분류 함수에 넣으면 618, 생성형 666, 전체 1,957 로 위 값과 같다. 다시 재지 않은 까닭: 재통합 1회가 2시간 걸렸고, 1단계 재통합 누적 상한(총 4회, R1-c1·R1-c2 몫)을 이 보완 측정에 쓰지 않는다. #299 부터는 고친 도구로 시계열에 두 값이 함께 남는다.
- 상한 해석(코디네이터 결정, 20:0xZ): 2,000건은 **생성형(claude-code) 호출**에만 건다. 로컬 임베딩은 세지 않는다. 4시간 벽시계 상한은 그대로다.

| 구분 (`llm-requests`, operation / scope / provider) | 건수 |
|---|---|
| consolidation / consolidation / claude-code | 617 |
| consolidation_dedup / consolidation_dedup / claude-code | 1 |
| consolidation / embedding_query, embedding_document / local | 617 + 617 |
| refresh_mental_model(`-pre` 페이지 delta 갱신) / reflect, reflect_tool_call / claude-code | 9 + 32 |
| mental_model_delta_ops / claude-code | 7 |
| refresh_mental_model / rerank(tei), embedding(local) | 25 + 32 |
| **합계** | **1,957**(생성형 666, 그중 통합 618) |

- 토큰(claude-code, 같은 구간, 페이지 갱신 포함): 입력 9,261,207, 출력 2,070,967, 캐시 3,159,739.
- 시계열(drain poll 발췌, UTC):

| 시각 | 관찰 | pending_consolidation | 전체 llm-requests |
|---|---|---|---|
| 19:47:16 | 4,428 | 236 | 11 |
| 19:51:07 | 640 | 2,332 | 120 |
| 20:12:31 | 1,224 | 1,908 | 462 |
| 20:46:46 | 1,938 | 1,311 | 887 |
| 21:13:57 | 2,586 | 753 | 1,320 |
| 21:44:09 | 3,262 | 106 | 1,756 |
| 21:53:13 | 3,373 | 0 | 1,957 |

- 처리 속도: 원문 약 20개/분, 생성형 통합 호출 1건당 원문 약 4.3개. PATCH 직후 관찰이 4,924 -> 640 으로 지워졌다가 다시 만들어졌다(그동안 recall 의 관찰이 줄어든다 - upstream 문서와 같다).
- 관찰·범위 전후(관찰 전체를 `GET .../memories/list?type=observation`으로 나열해 태그 묶음을 센 값, 기준선 #296 "관찰 범위"와 같은 방식):

| | 재통합 전(D10) | 재통합 뒤 |
|---|---|---|
| 관찰 | 4,924 | 3,373 |
| 관찰 범위(서로 다른 태그 묶음) | 1,290 | 307 |
| 관찰 1개인 범위 | 505 | 79 |
| project 없는 범위 | 176 | 176 |
| `kind:` 태그가 있는 범위 / 관찰 | 1,290 / 4,924 | 176 / 531 |
| project 없는 관찰 | 530 | 531 |

  (#296 기준선 1,337·507·184 는 운영 2026-10-29 값이다. D10 은 그보다 앞선 스냅숏이라 작다.)

## 5. 회귀 세 경로 (#297 고정 세트 18건)

- 고정 세트: `~/.local/share/hindsight-bench/arch-v2/r1-fixed-set.json`, sha256 `3fbe114b588af11e154b343639b8d5dcd9efe4b3c49343fb91559afbdd9d904d`(#297 값과 같다). evidence 는 다시 만들지 않았다.
- 순서(bench/README.md "(b) 페이지 이름·읽기 전용 모드와 delta 갱신"):
  1. `score --paths b --page-suffix -pre`(04:43:23~04:46:27 KST, 재통합 전 `-pre` 페이지 7개 생성, exit 0)
  2. 설정 적용·PATCH·drain(4절)
  3. `stats --wait 3600 --wait-pages` -> 21:53:49Z 연속 2회 `pages_stale: []`, `refresh_live: 0`, exit 0. `-pre` 페이지 7개 모두 21:51:48Z~21:52:49Z 에 delta 로 갱신됐다(`last_refreshed_at`)
  4. `score --paths b --read-pages --page-suffix -pre`(06:53:59~06:54:10, POST 0회, LLM 0회) = **delta**
  5. `score`(06:54:27~07:00:45, 접미사 없는 새 페이지 7개 + reflect + recall) = **full** 과 (a)(c)
- 리포 밖 출력: `score-20261030-044323/`(pre), `score-20261030-065359/`(delta), `score-20261030-065427/`(full·a·c).

| id | (a) 기준선 | (a) arm | (b) 기준선 | (b) pre | (b) delta | (b) full | (c) 기준선 | (c) arm |
|---|---|---|---|---|---|---|---|---|
| K01 | not-open | open | open | open | open | not-open | open | not-open |
| K02 | not-open | not-open | not-open | open | open | not-open | not-open | not-open |
| K03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K04 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K05 | not-open | not-open | not-open | not-open | not-open | not-open | open | open |
| K06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K08 | not-open | open | not-open | not-open | not-open | not-open | not-open | open |
| K09 | not-open | not-open | open | not-open | not-open | open | not-open | open |
| K10 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K12 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K14 | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K15 | not-open | not-open | open | not-open | not-open | not-open | not-open | not-open |
| K16 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| C01 | open | open | open | open | open | open | not-open | not-open |
| C02 | not-open | open | open | open | open | open | open | open |
| C03 | not-open | open | open | open | open | open | open | open |

| 경로 | 끝난 항목 open (기준선 -> arm) | 대조군 open (기준선 -> arm) | invalid | deleted_evidence | 질의·소요·LLM | 판정 |
|---|---|---|---|---|---|---|
| (a) reflect 나열 | 1/15 (K14) -> 2/15 (K01, K08) | 1/3 -> 3/3 | 0 | - | 1회, 108.9초, 5건 | 불합격 |
| (b) delta(`-pre` 갱신본) | 3/15 -> 2/15 (K01, K02) | 3/3 -> 3/3 | 0 | 0 | 7회, 11.2초, 0건(읽기만) | 불합격 |
| (b) full(재통합 뒤 새 페이지) | 3/15 -> 1/15 (K09) | 3/3 -> 3/3 | 0 | 0 | 7회, 187.0초, 133건 | 불합격 |
| (c) recall 상태 | 2/15 (K01, K05) -> 3/15 (K05, K08, K09) | 2/3 -> 2/3 | 0 | - | 18회, 81.6초, 35건 | 불합격 |

- 합격 조건(끝난 항목 open 0, invalid 0, 대조군 3/3)을 세 경로 모두 못 채운다. invalid 가 0 이라 "판정 불가"는 없다.
- (b) delta 는 재통합 전 같은 페이지(pre: K01, K02)와 판정이 같다. 7개 모두 delta 갱신이 돌았지만 판정을 바꾸지 못했다. full 은 끝난 항목 open 이 3 -> 1 로 가장 좋아졌다.
- (a)는 reflect 1회 응답이라 흔들림이 크다(#297 3절: 같은 사본·같은 질의에서 3 -> 1). 1 -> 2, 대조군 1/3 -> 3/3 은 점 하나다. (c)의 K08·K09 open 은 재통합으로 그 항목의 최신 상태 기억이 바뀐 결과일 수 있다(원문은 리포 밖 detail.json).
- 해석 주의(스펙 #296): 기준선은 재통합하지 않은 D10 이라, 위 차이는 설정 효과와 재통합 자체(v1.1.0, Opus) 효과가 섞여 있다.

## 6. 태그 필터 recall·topic 영향 (스펙 #296 O2 대상 질의)

- 방법: 같은 사본에서 재통합 전(04:42 KST)과 drain 뒤(06:54 KST)에 같은 질의를 upstream REST `POST .../memories/recall`(`types: ["observation"]`, `tags_match: all_strict`, `budget: mid`, `max_tokens: 8192`, `query_timestamp=2026-10-28T14:59:59Z`)로 보냈다.
  질의문은 둘 다 같은 일반 상태 질의다. "뱅크 일치"는 관찰 전체 나열에서 그 태그를 모두 가진 관찰 수다.

| 질의 | recall 관찰(전 -> 후) | 뱅크 일치 관찰(전 -> 후) |
|---|---|---|
| `kind:completion` + `project:hindsight-crew` | 41 -> 0 | 260 -> 0 |
| `kind:decision` + `project:hindsight-crew` | 34 -> 0 | 151 -> 0 |
| topic 모델 1의 태그(topic 단일 태그) | 48 -> 1 | 60 -> 1 |
| topic 모델 2의 태그(topic 단일 태그) | 51 -> 0 | 51 -> 0 |

- 원인: `[["project:*"]]` 범위의 관찰은 `project:` 태그만 물려받는다. 그래서 project 가 있는 관찰(3,373 - 531 = 2,842)에는 `kind:`·`topic:` 태그가 없다.
  원문 기억은 태그를 그대로 가지므로, kind·topic 필터 recall 은 원문 사실만 돌려준다. 관찰이 남은 곳은 project 없는(combined) 관찰 531개뿐이다(그중 `topic:` 있음 526).
- project 없는 기억: 원문 239개(전체 2,635의 9.1%, 그중 topic 있음 234, 둘 다 없음 5). 이들은 combined 로 돌아가 범위 176개·관찰 531개가 재통합 전과 같다. 가시성은 변하지 않았다.
- 영향: 멘탈 모델 8개 중 topic 단일 태그 모델 2개(all_strict)는 관찰만 읽는 지식 페이지·모델 기본값에서 근거가 거의 사라진다. `kind:` 필터 관찰 recall 을 쓰는 소비자도 같은 영향을 받는다.

## 7. 벤치 (동시 두 세트, 한 run)

- 명령: `bench/README.md` "1단계 arm 벤치" 절 그대로. 벤치 사이드카 `r1`(18781) 공유, s1 = D10, s2 = arm 백업(`--expected-overrides <LINEAGE>`),
  팔 base(s1, 18891)·cand(s2, 18892) 모두 `gw-up --ref 270242e --local-policies <운영 체크아웃>/policies/local`(코디네이터 결정: 기준선 A/A 와 같은 게이트웨이).
- 세트: s1 `backup_sha256` = `8a4900ea913d...`(D10), s2 = `51386018de6b...`(LINEAGE `backup.pg0_sha256`과 같다). 이미지 둘 다 `sha256:50311d02...`(v1.1.0).
  세트 시계 s1 `2026-10-26T19:30:00Z`, s2 `2026-10-29T21:51:00Z`(재통합이 `last_memory_write_at`을 바꿨다) -> `--clock-from-set s1`로 D10 시계를 썼다(`bench_clock_common.clocks_differ: true`).
- 팔: commit `270242e288ac20e48644ccf28bc03c79fb426996`, 두 팔 이미지 같음(`sha256:90b0fda19ca0...`, 이번에 로컬 빌드. #297 A/A 의 `006d066b542d...`와 digest 는 다르고 ref 는 같다).
  정책 sha256: `bank-policies.json` `0587ecbb315e...`, `context-triggers.json` `8ed1ebaa4e27...`(#297 과 같다).
- verify(run 직전): s1 `pass: true`(기대 override 없음), s2 `pass: true`(`expected_overrides` = LINEAGE, `bank_overrides_equal: true`, `bank_config_diff_unexplained: []`).

| run | 시작~끝 (KST) | 속도 기하평균 비 [95% CI] | 섞은 p95 비 [95% CI] | 토큰 base / cand | 결과 변경 | 오염 쌍 | arm_compare |
|---|---|---|---|---|---|---|---|
| r1b-1 | 07:02:59~07:27:39 | 1.0285 [0.9918, 1.0512] | 1.0413 [1.0240, 1.0505] | 295,475 / 284,530 | 40/40 | 4/120 | PASS (valid) |

- 기준선 대비: base 토큰 295,475 는 #297 A/A 와 같다. 속도는 cand 가 2.8% 느리다(중앙값 4,361.5ms -> 4,540.4ms). 문턱 3.0% 안이지만 경계에 가깝고, A/A 흔들림 폭(0.983~1.007)보다 바깥이다.
  섞은 p95 는 4.1% 느리다(문턱 6.0% 안). 토큰은 -3.7%(쿼리 33개 감소, 7개 증가, 쿼리별 증가 최대 +424, p95 +270).
- 결과 변경 40/40 은 설계상 예상된 값이다(관찰이 바뀌는 arm, 스펙 #296 - 보고만 한다).
- p99 는 내지 않았다(참고용).

## 8. arm 백업

- `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"; bash scripts/backup.sh` -> `backup ok: backups/20261030-070107/pg0.tar.gz (173766972 bytes)`(07:01:07~07:01:15 KST).
  워크트리에서 실행해 워크트리 `backups/`에 쓰였다(운영 체크아웃 `backups/`에는 생기지 않았다 - `ls`로 확인). 곧바로 리포 밖으로 옮기고 워크트리 `backups/`를 지웠다.
- 위치: `~/.local/share/hindsight-bench/arm-backups/r1-b-20261030-070107/`(0700). `pg0.tar.gz` sha256 **`51386018de6b363d7b66fd0499692dd8307bee4fcaf670ee61ac7e08cc04fa8a`**, `LINEAGE`(0600, schema_version 1, arm `r1-b-i`).
- LINEAGE 요지: source_backup = D10(sha256 `8a4900ea...`), settings 위 2절, directives [], document_patches 2633, reconsolidation 19:46:39Z ~ 21:53:13Z(llm_requests 1957, 생성형 666, 통합 618),
  pages `-pre`/``(full), expected_bank_overrides 위 2절. 이 백업에는 `-pre` 페이지 7개와 full 페이지 7개가 들어 있다.

## 9. O2 후보 (ii)·(iii) 비용 추정과 #299 권고

근거 값(D10 원문 2,635개, 벤치 복제본에서 읽기만): project 태그 0개 239, 1개 2,156, 2개 이상 240. `topic:` 태그가 있는 원문 1,996(그중 project 도 있음 1,762, project 없음 234). 서로 다른 project 묶음 132, topic 묶음 421.

- (ii) `[["project:*"], ["topic:*"]]`: 원문이 범위마다 한 번씩 통합된다. 기억-범위 쌍은 project 2,396 + topic 1,996 + combined 5 = 4,397 로, (i)의 2,635 대비 **약 1.67배**다.
  (i) 실측(생성형 통합 618건, 2시간 6분)에 비례하면 **생성형 약 1,030~1,110건, 약 3시간 30분**이다. 상한(2,000건)은 넘지 않지만 4시간 벽시계 상한에 가깝다.
  기대 효과: topic 범위 관찰이 `topic:` 태그를 가지므로 topic 모델 2개의 관찰 가시성이 돌아온다. `kind:` 필터 관찰 recall 은 (ii)에서도 돌아오지 않는다.
- (iii) project 없는 retain 에 기본 project 태그: 대상은 원문 239개(9.1%)뿐이다. 재태깅 시 재통합 비용은 비례 추정 **생성형 약 56건**(618 x 239/2,635)이다.
  효과는 그 기억들을 project 범위 하나로 묶는 것뿐이고, topic·kind 가시성 문제는 풀지 못한다. 기존 재태깅 도구는 hc-next 를 대상으로 할 수 없어(스펙 #296) 사본 실측은 하지 않는다.
- **#299 권고**: (ii)를 실측 1순위로 잰다.
  - (ii)는 6절의 topic 모델 근거 소실을 막는 유일한 후보다.
  - 재통합이 4시간 상한에 가까우므로 `scope-migrate.py drain --timeout` 을 남은 벽시계에 맞추고, 시작 30분의 처리 속도로 완료 시각을 먼저 외삽한다.
  - (iii)은 추정만 보고서에 넣는다.
  - `kind:` 필터로 관찰을 찾는 소비자가 있으면 (i)·(ii) 모두 영향이 있다. 1단계 보고서(#304)에 소비자 목록 확인을 넣는다.

## 10. 부하 구간 (canary-exclude 형식)

```
2026-10-30 04:42:19 2026-10-30 07:06:35  # #298 R1-b hc-next D10 사본(up 04:42:19 ~ down 확인 07:01:35 +300s): -pre 페이지·문서 PATCH 2633·재통합(Opus)·delta 갱신·score 세 경로·arm 백업
2026-10-30 07:01:57 2026-10-30 07:33:30  # #298 R1-b 벤치 복제본 s1 D10 / s2 arm 백업 + 벤치 사이드카(up 07:01:57 ~ down 확인 07:28:30 +300s): run r1b-1
```

정리 확인(07:28:30): `docker ps -aq --filter label=hc-bench=1`·`docker volume ls -q --filter label=hc-bench=1`·`docker network ls -q --filter name=hc-bench`·`lsof -nP -iTCP:18780-18799 -sTCP:LISTEN` 모두 빈 출력, `next-stack.sh status` exit 3.
워크트리 심링크(`bench/.data`·`audit`·`secrets`)는 지웠다.
