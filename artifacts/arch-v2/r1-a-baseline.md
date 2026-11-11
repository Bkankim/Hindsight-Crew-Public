# R1-a 1단계 기준선 arm (issue #297, 상위 #296)

1단계 **기준선 arm**(v1.1.0, 설정 불변)의 정본 값이다. 이후 arm(#298~#301)은 모두 이 값과 비교한다.
이 파일에는 항목 id·판정·해시·집계만 쓴다(정답표 항목 이름, 질의·응답 원문, 개인 경로 없음). 원문은 리포 밖 `~/.local/share/hindsight-bench/arch-v2/`(0700)에만 있다.

## 요약

- 회귀 세트(D10 hc-next, `--llm opus`): evidence 근거 있음 15건(K13만 없음), **고정 세트 18건**(완료 15 + 대조군 3). score 세 경로 모두 invalid 0.
  끝난 항목이 open 으로 나온 수는 (a) 1/15, (b) 3/15, (c) 2/15, 대조군 open 은 (a) 1/3, (b) 3/3, (c) 2/3. 기준선은 합격 조건(끝난 항목 open 0, 대조군 3/3)을 세 경로 모두 못 채운다.
- 평가 세트 고정: `~/.local/share/hindsight-bench/arch-v2/r1-fixed-set.json`(evidence `summary.json` 사본). 이후 arm 은 `score --evidence <이 경로>`로 읽는다.
- (b) delta: 작은 재통합(문서 3개) 뒤 범위가 겹친 페이지 2개가 통합 완료 즉시 stale 이 되고 delta refresh 로 갱신됐다. 대기 판정 = stale 페이지 0 + 진행 중 refresh 0(`stats --wait-pages`).
- 재통합 관찰 날짜: 다시 만든 관찰의 `mentioned_at`은 근거 원문 사실의 날짜다(재통합 시각 아님). 회귀 도구의 날짜 판정은 바꾸지 않는다.
- recall LLM 의존: 없다. v1.1.0 recall 구간의 `llm-requests` 행은 리랭크(tei)·질의 임베딩(local) 추적뿐이고 생성형 LLM 호출은 0이다. 동결 복제본(LLM none)도 같은 경로를 잰다.
- 벤치 A/A(D10/D10 두 세트 동시, 6 run): 6 run 중 유효 4(r1a-aa-1~4), 무효 2(r1a-aa-5·6, 운영 요청과 겹친 오염 쌍 36%·69% > 20%). 유효 4 run 모두 토큰 295,475 = 295,475, 결과 변경 0/40, 속도 비 0.983~1.007, 섞은 p95 비 0.979~1.021.
- 확정 문턱: **속도 3.0%, 섞은 p95 6.0%**(#288 산식, n=4 예측한계, 0.5% 올림, 하한 1.0%·4.0%보다 넓다). 비용(토큰) 허용 0.

## 1. 사본과 복원원

- 사본 up(2026-10-30 00:57:13~00:57:45 KST): `HC_NEXT_BACKUP_SHA256=8a4900ea... HC_NEXT_BACKUP_DIR=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001 HC_NEXT_REQUIRED_BANK=personal-demo bash scripts/next-stack.sh up --llm opus` exit 0.
- 복원 줄 원문(홈 경로만 바꿈):

```
restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4
pg_verifybackup ok
up ok: commit=f2a1c478998ac5070b93ae226efa76fa8e48b2e4 gateway=http://127.0.0.1:18888 ui=http://127.0.0.1:9998 llm=opus
```

- sha256 이 D10 값과 같다. hc-next hindsight 이미지: `ghcr.io/vectorize-io/hindsight@sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06`(v1.1.0, `docker inspect -f '{{.Config.Image}}' hc-next-hindsight-1`).
- 복원 직후 stats: documents 2633, observations 4924(world 1916, experience 719), pending_consolidation 0, pending_operations 0, failed_operations 40.
- 회귀 명령은 이 워크트리(머지 전 하네스)에서 `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"` 뒤 `python3 -B -m bench.arch_v2_regression ... --answer-key <정답표>`로 돌렸다.
- down: `bash scripts/next-stack.sh down` exit 0(01:18:12), 직후 `status` exit 3, `docker ps -aq --filter name=hc-next`·`docker volume ls -q --filter name=hc-next` 빈 출력.

## 2. evidence 와 고정 세트

- 명령: `evidence --bank personal-demo`(00:59:37~01:02:31 KST, exit 0, recall 29회). 정답표 keyword 는 2026-10-29 정리본(`revisions` 있음).

| id | 뱅크 안 완료 근거 | mentioned_at (UTC 날짜) | 근거 종류 |
|---|---|---|---|
| K01 | 근거 있음 | 2026-10-17 | kind:completion |
| K02 | 근거 있음 | 2026-10-19 | text |
| K03 | 근거 있음 | 2026-09-27 | kind:completion |
| K04 | 근거 있음 | 2026-09-27 | text |
| K05 | 근거 있음 | - | kind:completion |
| K06 | 근거 있음 | 2026-09-26 | kind:completion |
| K07 | 근거 있음 | 2026-10-20 | kind:completion |
| K08 | 근거 있음 | 2026-09-30 | kind:completion |
| K09 | 근거 있음 | 2026-10-06 | kind:completion |
| K10 | 근거 있음 | 2026-09-17 | kind:completion |
| K11 | 근거 있음 | 2026-09-30 | kind:completion |
| K12 | 근거 있음 | 2026-09-30 | kind:completion |
| K13 | 없음 -> 세트에서 뺌 | - | - |
| K14 | 근거 있음 | 2026-10-16 | kind:completion |
| K15 | 근거 있음 | 2026-10-20 | kind:completion |
| K16 | 근거 있음 | 2026-10-16 | kind:completion |
| C01 | 대조군(expect open, 근거 확인 대상 아님) | - | - |
| C02 | 대조군(expect open, 근거 확인 대상 아님) | - | - |
| C03 | 대조군(expect open, 근거 확인 대상 아님) | - | - |

남은 세트: 완료 15건 + 대조군 3건 = **18건** (정답표 완료 16건 중 1건 뺌). K05 근거는 mentioned_at 이 없는 기억이다(사본 정책상 근거로 받는다).

**고정 세트 파일**: `~/.local/share/hindsight-bench/arch-v2/r1-fixed-set.json`(0600) = `evidence-20261030-005937/summary.json`의 사본,
sha256 `3fbe114b588af11e154b343639b8d5dcd9efe4b3c49343fb91559afbdd9d904d`(원본과 같다). 세트 id: K01 K02 K03 K04 K05 K06 K07 K08 K09 K10 K11 K12 K14 K15 K16 C01 C02 C03.
이후 arm 은 evidence 를 다시 만들지 않고 `score --evidence ~/.local/share/hindsight-bench/arch-v2/r1-fixed-set.json`으로 읽는다(새 입력 형식 없음).

## 3. score 세 경로 (기준선 정본)

- 명령: `score --bank personal-demo --evidence <고정 세트>`(01:02:52~01:10:21 KST, exit 0). (b)는 페이지 7개(프로젝트 힌트 6 + 전체 1)를 접미사 없는 이름으로 첫 생성(full)했고,
  trigger 는 기본값(delta, `refresh_after_consolidation`)이다. 7개 operation 모두 `completed`(각 약 165~175초, 동시 진행).
- 리포 밖 출력: `score-20261030-010252/`(`detail.json` 원문, `metrics.json`, `summary.json`).

| id | a_reflect_list | b_knowledge_page_status | c_recall_status |
|---|---|---|---|
| K01 | not-open | open | open |
| K02 | not-open | not-open | not-open |
| K03 | not-open | not-open | not-open |
| K04 | not-open | not-open | not-open |
| K05 | not-open | not-open | open |
| K06 | not-open | not-open | not-open |
| K07 | not-open | not-open | not-open |
| K08 | not-open | not-open | not-open |
| K09 | not-open | open | not-open |
| K10 | not-open | not-open | not-open |
| K11 | not-open | not-open | not-open |
| K12 | not-open | not-open | not-open |
| K14 | open | not-open | not-open |
| K15 | not-open | open | not-open |
| K16 | not-open | not-open | not-open |
| C01 | open | open | not-open |
| C02 | not-open | open | open |
| C03 | not-open | open | open |

| 경로 | open | not-open | invalid | 끝난 항목 open | 대조군 open | 질의 수 | 소요(초) | LLM 호출 |
|---|---|---|---|---|---|---|---|---|
| a_reflect_list | 2 | 16 | 0 | 1/15 (K14) | 1/3 | 1 | 165.7 | 19 |
| b_knowledge_page_status | 6 | 12 | 0 | 3/15 (K01, K09, K15) | 3/3 | 7 | 176.7 | 116 |
| c_recall_status | 4 | 14 | 0 | 2/15 (K01, K05) | 2/3 | 18 | 106.2 | 35 |

- LLM 호출은 경로 구간의 `GET .../llm-requests?start_date&end_date` `total`이다. (c)의 35건은 recall 18회의 리랭크·임베딩 추적 행이다(아래 6절, 생성형 호출 아님).
- 판정 사유: (a) open-section 2·absent 16, (b) open-section 6·absent 12, (c) latest-done 13·latest-open 4·no-status 1. invalid 0.
- 합격 조건(스펙 #296) 대비: 세 경로 모두 불합격(끝난 항목 open > 0, (a)(c)는 대조군도 3/3 아님). 기준선은 이 값 그대로 이후 arm 의 비교 기준이다.
- #291 사전 점검(같은 D10, 정답표 정리 전)과 비교: (a)는 끝난 항목 open 3 -> 1, 대조군 3/3 -> 1/3 으로 움직였다. reflect 1회 응답의 표본 흔들림이 크다(같은 사본, 같은 질의).
  (b)(c)는 정답표 keyword 정리 뒤 각각 1 -> 3, 3 -> 2 다. 이 값들은 run 1회의 점이다 - (a)는 특히 반복하면 흔들린다는 점을 이후 arm 판정에 감안한다.

### (b) 읽기 전용 재채점 (새 모드 확인)

- `score --paths b --read-pages`(01:10:51, POST 0회, LLM 0회, 0.6초): 판정이 위 (b) 열과 같다(`identical True`). detail 에 페이지 이름과 `last_refreshed_at`이 남는다.

## 4. (b) delta 갱신 실측 (작은 재통합과 함께)

방법: 1~3절 채점이 끝난 뒤, 같은 사본에서 문서 3개(`project:` 태그와 `kind:` 태그가 있고 memory unit 1개인 문서, 2026-10-26 retain)의 `observation_scopes`를
`[["project:*"]]`로 PATCH 했다(`PATCH /v1/default/banks/personal-demo/documents/<id>`, upstream 직결, 3건 모두 `success: true`). 1단계 재통합 상한에 넣지 않는다.

| 시각 (UTC) | 사건 |
|---|---|
| 16:12:16 | PATCH 3건. 그 문서들의 관찰 12개가 바로 지워짐(관찰 4924 -> 4912), pending_consolidation 3, consolidation 작업 processing |
| 16:13:04 | 통합 완료(관찰 4918, pending 0). 같은 초에 페이지 2개 `is_stale=true`, `refresh_mental_model` 작업 2개 생성 |
| 16:14:38, 16:14:54 | 두 refresh 완료. `is_stale` false, 그 두 페이지만 `last_refreshed_at`·`last_memory_seen_at` 이 올라감. 나머지 5개는 그대로 |

- 갱신 조건: 통합이 끝나면 **범위(페이지 tags, all_strict)에 새 관찰이 생긴 페이지만** stale 이 되고 즉시 refresh 된다. refresh 는 delta 모드였다(LLM 호출에 `mental_model_delta_ops` 2건·`mental_model_retraction_ops` 2건).
- 대기 판정: `pending_consolidation == 0` 그리고 트리의 stale 페이지 0개 그리고 `pending`·`processing`인 `refresh_mental_model` 작업 0개.
  "모든 페이지의 `last_refreshed_at`이 바뀔 때까지"는 쓰면 안 된다(범위가 조용한 페이지는 갱신되지 않는다). 이 판정을 `stats --wait <초> --wait-pages`로 넣었다(final-review 뒤: 연속 2회 poll 이 같아야 완료, operations 전부 조회).
- delta 결과 채점: `score --paths b --read-pages`(01:15:21) 판정이 3절 (b)와 같다(`identical True`). 문서 3개 이동이라 페이지 내용이 판정을 바꿀 만큼 바뀌지 않았다.
- 비용(16:12:16~16:15:30 UTC `llm-requests` 33건): 통합 = claude-code 1건 + 임베딩 2건, refresh 2개 = claude-code 16건(reflect 4, reflect_tool_call 8, delta_ops 2, retraction_ops 2) + 리랭크 6·임베딩 8.
  claude-code 입력 193,052 / 출력 21,893 토큰. 통합 48초, refresh 약 1분 40초.
- 이후 arm 의 (b) 순서: 재통합 전 `-pre` 페이지 생성(full) -> 설정 적용·문서 PATCH(재통합) -> `stats --wait 14400 --wait-pages` -> `score --paths b --read-pages --page-suffix -pre`(delta 결과)
  -> 접미사 없는 새 페이지로 `score`(full 결과와 (a)(c)). 명령은 `bench/README.md` "(b) 페이지 이름·읽기 전용 모드와 delta 갱신" 절.

## 5. 재통합된 관찰의 날짜

- 재통합 전: 세 문서에서 파생된 관찰 12개의 `mentioned_at`은 근거 사실의 `mentioned_at`(2026-10-26)과 같았다.
- 재통합 뒤 다시 만든 관찰 9개(`time_field=updated_at`, 16:12 이후, source_memory_ids 가 세 문서의 memory unit 을 포함): **`mentioned_at`이 모두 근거 원문 사실의 `mentioned_at`(source facts 중 가장 늦은 것, 2026-10-26)** 이다.
  재통합 시각(2026-10-29 16:13 UTC)은 `updated_at`에만 찍혔다. 범위 태그는 새 범위(`project:` 태그만, 또는 다른 문서 관찰과 합쳐진 태그)로 바뀌었다.
- recall 결과 행: 다시 만든 관찰 5개를 본문으로 recall 해 5개 모두 결과에 나왔고, 행의 `mentioned_at`이 관찰·근거 사실의 값과 같다.
- (c) 재채점(01:15:42~01:17:20, 18회): 판정이 3절 (c)와 같다(`identical True`), 결과 행 최대 mentioned_at 2026-10-23 < 기준 시각.
- 결론: 재통합한 arm 의 응답이 기준 시각 규칙 때문에 invalid 가 되지 않는다. **회귀 도구의 관찰 날짜 판정은 바꾸지 않는다**(source facts 날짜로 판정하는 수정 불필요).
- 부수 발견: 페이지(멘탈 모델) `reflect_response.based_on`의 관찰 항목에는 `mentioned_at`이 없다(id·type·text·context 뿐, reflect MCP 의 based_on 에는 있다).
  그대로 두면 (b)의 근거 날짜 검사가 운영 연막 채점에서 늘 `undated-evidence`로 invalid 가 된다. 그래서 회귀 도구가 날짜 없는 항목의 id 로 `GET .../memories/{id}`(읽기만)를 조회해
  `mentioned_at`을 채운 뒤 검사한다(rework #297-1). 조회가 실패하면 대상과 무관하게 그 페이지 판정은 invalid(`date-lookup-failed`)다.

## 6. v1.1.0 recall 의 LLM 의존

- hc-next(LLM 있음): dev 질의 6개(seed 297 표본, `query_timestamp`=D10 벤치 시계 2026-10-26T19:30:00Z)를 게이트웨이 MCP recall 로 돌렸다(KST 00:58:34~00:59:20, UTC 15:58:34~15:59:20). 오류 0.
  그 구간 `llm-requests` 12건 = `operation=recall`의 `scope=rerank`(provider tei) 6건 + `scope=embedding_query`(provider local, bge-m3) 6건. **생성형 LLM(claude-code) 호출 0건.**
  #287 사본 실측의 "recall 구간 LLM 요청"도 같은 추적 행이다(recall 1회당 2행).
- 동결 복제본(LLM_PROVIDER=none, 벤치 세트 s1·s2): 같은 6개 질의를 팔 두 개로 각각 2회 돌렸다. 오류 0, 두 팔·두 회 모두 같은 결과(결정적).
  hc-next 결과와 비교: 5/6 은 결과 digest 가 바이트 단위로 같고, 1/6 은 결과 구성이 다르다(17행 대 16행, 순위가 바뀐 행 2, 빠진 행 1).
- 해석: 벤치는 운영과 **같은 recall 경로**를 잰다(recall 에 생성형 LLM 이 없다). 1/6 차이는 LLM 이 원인일 수 없다. 리랭커 인스턴스 차이로 추정한다
  (hc-next 는 운영 사이드카, 벤치는 벤치 사이드카. 같은 모델이지만 배치 구성에 따른 fp16 점수 차가 동점 근처 순위를 바꿀 수 있다) - **미검증**.
  그래서 코디네이터에 ask 하지 않았다(의존 없음).

## 7. 벤치: D10/D10 두 세트 A/A

- 방식(스펙 #296): 세트 s1 = D10, s2 = D10 을 동시에 띄우고(공유 벤치 사이드카 `r1a`, 포트 18781), 세트마다 같은 ref 팔(base = s1 18891, cand = s2 18892)을 세워
  `bench.runner --arm base=18891 --arm cand=18892 --split dev --limit 40 --seed 13 --rounds 3`을 run 마다 돌렸다. run 직전마다 두 세트 verify, run 뒤 `bench/arm_compare.py`.
- 게이트웨이 ref: 이 브랜치 HEAD `6c46e73b6ae6b926bb3dca393e90ca295ae21cbc`(하네스 커밋, 머지 전). 두 팔 이미지 같음(`sha256:006d066b542d...`).
  1단계 고정 ref 는 이 티켓의 머지 커밋이다. 브랜치 HEAD 와 머지 커밋의 게이트웨이 코드가 같은지는 머지 때 `git diff <HEAD> <머지 커밋> -- gateway/`로 확인한다(이 브랜치는 `gateway/`를 바꾸지 않는다).
- 운영 정책: `gw-up --local-policies <운영 체크아웃>/policies/local`(읽기만). `bank-policies.json` sha256 `0587ecbb315e5504b019f53daa4a3bc3081478d1d3711e5ebdf1a03735c991ca`,
  `context-triggers.json` sha256 `8ed1ebaa4e27295a12c3c95d7cd516bd0ba3fb93c1cebd0a630d2d7630049151`(두 팔의 gw-up 출력 `local_policies.files`와 같다).
- 세트 up 출력: s1·s2 모두 `backup_sha256` = `8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4`(D10 과 같다), 이미지 `sha256:50311d02...`(v1.1.0, 운영과 같다), `bench_clock` `2026-10-26T19:30:00Z`(두 세트 같다 - `--clock-from-set` 불필요).
- D10 verify: 기대 override 없이 `pass: true`. D10 의 뱅크 override 가 지금 운영과 같은 `memory_defense`(`{"rules": [], "enabled": false}`) 하나다(`bank_overrides_equal: true`).
  해석된 설정 차이는 env override 로 설명되는 `enable_auto_consolidation`·`enable_observations`·`retain_extraction_mode` 뿐(`bank_config_diff_unexplained: []`). 6 run 의 run 직전 verify 12건 모두 pass.
- arm_compare 호출: `python3 -B -m bench.arm_compare bench/.data/runs/<id> --base base --cand cand --expect-sha base=<D10> --expect-sha cand=<D10> --verify base=<verify-s1> --verify cand=<verify-s2> --speed-threshold 0.01 --p95-threshold 0.04`
  (run 당시 문턱은 #288 하한값이다. 판정 결과는 아래 확정 문턱에서도 같다).

| run | 시작~끝 (KST) | 복원 sha256 base / cand | 세트 시계 s1 / s2 | verify s1 / s2 | 속도 기하평균 비 [95% CI] | 섞은 p95 비 [95% CI] | 토큰 base / cand | 결과 변경 | 오염 쌍 | 가드 정지 | arm_compare |
|---|---|---|---|---|---|---|---|---|---|---|---|
| r1a-aa-1 | 01:22:00~01:51:28 | 8a4900ea913d... / 8a4900ea913d... | 2026-10-26T19:30:00Z / 2026-10-26T19:30:00Z | True / True | 0.9830 [0.9474, 1.0055] | 1.0200 [0.9617, 1.0655] | 295,475 / 295,475 | 0/40 | 14/120 | 6 | PASS |
| r1a-aa-2 | 01:51:31~02:17:13 | 8a4900ea913d... / 8a4900ea913d... | 2026-10-26T19:30:00Z / 2026-10-26T19:30:00Z | True / True | 0.9973 [0.9872, 1.0058] | 1.0069 [0.9888, 1.0277] | 295,475 / 295,475 | 0/40 | 16/120 | 2 | PASS |
| r1a-aa-3 | 02:17:16~02:41:39 | 8a4900ea913d... / 8a4900ea913d... | 2026-10-26T19:30:00Z / 2026-10-26T19:30:00Z | True / True | 1.0066 [0.9951, 1.0176] | 1.0206 [0.9760, 1.0485] | 295,475 / 295,475 | 0/40 | 11/120 | 1 | PASS |
| r1a-aa-4 | 02:41:42~03:07:22 | 8a4900ea913d... / 8a4900ea913d... | 2026-10-26T19:30:00Z / 2026-10-26T19:30:00Z | True / True | 0.9972 [0.9857, 1.0098] | 0.9794 [0.9564, 1.0235] | 295,475 / 295,475 | 0/40 | 8/120 | 3 | PASS |
| r1a-aa-5 | 03:07:24~03:30:07 | 8a4900ea913d... / 8a4900ea913d... | 2026-10-26T19:30:00Z / 2026-10-26T19:30:00Z | True / True | 0.9992 [0.9954, 1.0025] | 1.0071 [0.9948, 1.0124] | 295,475 / 295,475 | 0/40 | 43/120 | 0 | INVALID (contaminated:0.36) |
| r1a-aa-6 | 03:30:34~03:54:29 | 8a4900ea913d... / 8a4900ea913d... | 2026-10-26T19:30:00Z / 2026-10-26T19:30:00Z | True / True | 0.9952 [0.9845, 1.0037] | 1.0202 [0.9895, 1.0473] | 295,475 / 295,475 | 0/40 | 83/120 | 1 | INVALID (contaminated:0.69) |

- 무효 run: r1a-aa-5 는 운영 감사 로그의 리랭크 사용 요청 9건(retain 6, recall 2, 멘탈 모델 1)과 겹친 쌍 43/120, r1a-aa-6 은 9건(retain 5, recall 2, 멘탈 모델 2)과 겹친 쌍 83/120 이다.
  멘탈 모델 요청은 앞뒤 240초를 제외 구간으로 잡아(judge.py `PROD_WINDOW_S`) 한 건이 쌍 수십 개를 뺀다. 가드 정지는 0·1회. 판정 규칙대로 문턱 계산에서 뺐다. 다시 재지 않은 까닭: 같은 시간대 운영 활동이 이어져 무효 위험이 컸고, 유효 4 run 으로 완료 기준(3 run 이상)을 채웠다.
- 유효 run 의 가드 정지(호스트 swapout) 1~6회는 겹친 쌍을 비교기가 뺐다(오염 쌍 8~16/120, 7~13%).
- 두 세트 사이 기본 차이: 같은 D10 데이터인데 run 마다 속도 비가 0.983~1.007 로 움직였다(평균 0.9960). r1 의 -1.7% 는 r2~r4 에서 되풀이되지 않아 고정된 세트 편향이 아니라 run 사이 흔들림으로 본다.
- p99 는 `arm_compare.py`가 내지 않는다(스펙 #296: 참고용). 필요하면 같은 run 을 `bench/judge.py`로 본다.

## 8. 확정 문턱

산식(스펙 #296 + #288 `bench-noise-2026-10-29.md`): 점추정 흔들림 폭 = `|평균 비 - 1| + k x 표준편차`, `k = t(0.95, 자유도 n-1) x sqrt(1 + 1/n)`(다음 1 run 의 95% 단측 예측한계), 0.5% 단위 올림.
속도 문턱 = max(1.0%, 속도 흔들림 폭), 섞은 p95 문턱 = max(4.0%, p95 흔들림 폭). 유효 run n = 4, k = 2.3534 x 1.1180 = **2.631**.

| 축 | 유효 run 값 (r1~r4) | 평균 | 표준편차 | 흔들림 폭(올림 전) | 올림 | **확정 문턱** |
|---|---|---|---|---|---|---|
| 속도(기하평균 지연 비, 점추정) | 0.9830, 0.9973, 1.0066, 0.9972 | 0.9960 | 0.0098 | 0.0297 | 3.0% | **3.0%** (`--speed-threshold 0.03`) |
| 섞은 p95 비(점추정) | 1.0200, 1.0069, 1.0206, 0.9794 | 1.0067 | 0.0193 | 0.0575 | 6.0% | **6.0%** (`--p95-threshold 0.06`) |
| 비용(토큰 합계) | 295,475 = 295,475 (4/4) | - | 0 | 0 | - | **0** (cand <= base) |

- 판정: arm 의 cand/base 점추정이 `1 + 문턱` 이하면 그 축 ok. 세 축이 모두 ok 면 PASS(`arm_compare.py` exit 0).
- 두 문턱 모두 하한(1.0%, 4.0%)보다 넓다. #288(한 인스턴스 안 두 팔, 5 run, 속도 sd 0.0032)과 달리 이 방식은 **서로 다른 두 인스턴스**를 비교해 run 사이 흔들림이 크다(속도 sd 0.0098).
  그래서 1단계 벤치는 3% 안쪽의 속도 차이를 가르지 못한다. 이 해석을 1단계 보고서(#304)에 적는다.
- run 을 더 쌓으면 k 와 표준편차 추정이 줄어 문턱이 좁아질 수 있다(n=5 이면 k=2.335). 이 값은 유효 4 run 의 정본이다.

## 9. 하네스 변경 (O10)

- `bench/replica.py`: `up|verify --expected-overrides <LINEAGE>`. 세트 override 가 LINEAGE `expected_bank_overrides`와 같으면 통과, 운영 override 와 기대가 다른 키는 해석된 설정 차이에서도 허용. 옵션이 없으면 지금과 같다.
- `bench/runner.py`: `--clock-from-set <세트>`. 세트 시계가 다르면 그 세트 시계를 쓰고 `meta.bench_clock_common`에 세트별 시계를 남긴다. 옵션이 없으면 지금처럼 거부한다.
- `bench/arm_compare.py`(새 파일): 한 run 안 두 팔의 쿼리별 지연 비 중앙값 기하평균과 부트스트랩 CI, 섞은 p95 비, 토큰 합계와 쿼리별 증가 분포, 결과가 바뀐 쿼리 비율, 문턱 판정.
  중단 run·오류 응답·outcome 차이·동결 지문 변화·rewrite 실패·sha256 불일치·verify 미통과·오염 초과·표본 하한 미달(기본 3라운드·40쿼리)은 INVALID(exit 2).
  두 팔 모두 ok-unstructured 라 속도 표본에서 빠진 쌍 수는 `speed.unstructured_pairs`로 낸다(기준선 유효 4 run 은 모든 측정 호출이 ok 라 0).
- `bench/arch_v2_regression.py`: `--page-suffix`, `--read-pages`, `--page-map`, `stats --wait-pages`.
- arm 백업 위치 규약과 LINEAGE 스키마, 1단계 arm 벤치 명령: `bench/README.md` "1단계 arm 벤치" 절.
- 테스트(수정 전 red -> 수정 뒤 green): 수정 전 red 는 새 테스트 32 failed + `tests/test_bench_arm_compare.py` import 실패(collection error),
  `stats --wait-pages` 2 failed. 수정 뒤 해당 파일 green(replica 303, runner 274, arm_compare 18, 회귀 도구 96 passed). CI 동등 전체 pytest 결과는 커밋 메시지와 완료 보고에 있다.
  - replica 기대 override: 옵션 없음 = 불일치 실패, 기대와 같음 = 통과(arm 모양 범위·미션·성향·관찰 우선 포함), 기대와 다름·기대에 없는 override = 실패, 설명 안 되는 설정 키 = 실패(`test_bank_override_report_*`, `test_verify_uses_expected_overrides_*`, `test_up_records_expected_overrides_*`)
  - arm_compare: 합성 run 으로 비·토큰·변경 비율, 라운드 중앙값·기하평균, 문턱, 중단(meta 없음·가드)·호출 부족·오류 응답·outcome 차이·동결 변화·rewrite 실패·sha256 불일치·verify 미통과 거부, CLI exit 코드
  - runner 공통 시계: 옵션 없음 = 시계 불일치 거부, 옵션 = 기준 세트 시계 사용 + meta 기록 + 호출 인자에 그 시계, CLI 전달
  - 회귀 도구: `-pre`와 접미사 없는 페이지 공존·각각 채점, 읽기 전용 모드 쓰기 0, 접미사 생성·충돌 검사, 매핑 파일, 옵션 충돌 거부, `stats --wait-pages`
  - 관찰 날짜를 source facts 로 판정하는 테스트: 5절 실측 결과 도구 변경이 필요 없어 넣지 않았다.

## 10. 부하 구간 (canary-exclude 형식)

```
2026-10-30 00:57:13 2026-10-30 01:23:12  # #297 R1-a hc-next D10 사본(up 00:57:13 ~ down 확인 01:18:12 +300s): recall LLM 확인·evidence·score 세 경로·작은 재통합(문서 3개)·delta 갱신
2026-10-30 01:18:40 2026-10-30 03:59:59  # #297 R1-a 벤치 복제본 D10/D10 두 세트 + 벤치 사이드카(up 01:18:40 ~ down 확인 03:54:59 +300s): A/A r1a-aa-1~6
```
