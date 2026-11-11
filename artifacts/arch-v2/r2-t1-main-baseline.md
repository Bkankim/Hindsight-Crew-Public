# 런 2 T1: main 양립과 기준선 main (issue #319)

상위: #318. 사본 사슬의 첫 자리다. 1단계에서 v1.1.0 D10 사본을 #323에 내줬고, 2단계에서 upstream main 고정 빌드로 D10 사본을 띄워 양립(게이트·계약·`tags_match`)과 기준선 main 채점을 했고, 3단계에서 main 사본 백업을 v1.1.0 bench 복제본에 복원해 verify 했다.
이 문서에는 id·판정·해시·수치만 쓴다(정답표 항목 이름, 질의·응답 원문, 기억 본문, 뱅크 실명 없음, 공개 전환 가드 #4). 원문은 리포 밖 `~/.local/share/hindsight-bench/`(0700)에만 있다.

## 요약

- **양립: 실패 없음.** 게이트 9개 모두 exit 0. MCP `tools/list`는 v1.1.0 fixture와 main 모두 39도구이고 사라진 도구·바뀐 필수 인자가 없다(게이트웨이 정책 집합 영향 0). 차이는 인자 설명 문구 추가(35도구)와 `recall`의 선택 인자 `rerank` 추가뿐이다.
  게이트웨이 경유 retain 응답은 v1.1.0 fixture와 같은 모양이고 `storage_status`·`mcp_successes`가 operation id 를 잡는다. `tags_match` 다섯 값 모두 게이트웨이 MCP·upstream REST 200.
- **기준선 main (합친 세트 A∪B, 끝난 30·대조군 6)**: (a) 유효 run 3개 평균 **2.00**(1, 1, 4, 시도 5회, 대조군 0 run 2개), (b) 두 생성 평균 **2.50**(2, 3), (c) **5**. invalid 0.
  기준선 v1.1.0(#323)과 나란히 놓으면 (a) 1.67, (b) 2.50, (c) 5 로 같은 폭 안이다(참고 판정은 4절).
- **v1.1.0 복제본 양립**: main 사본 백업을 bench 복제본(v1.1.0)에 복원해 `verify --set s2` pass, `backup_sha256` = LINEAGE `backup.pg0_sha256`. #324·#325 벤치의 전제가 선다.

## 1. 사본과 복원

| 단계 | 이미지 태그 | 이미지 ID(`docker image inspect -f '{{.Id}}'`) | 라벨 확인 | up 시각(KST) | 기동 시간 | 마이그레이션 head | `restore:` 줄(홈 경로만 바꿈) |
|---|---|---|---|---|---|---|---|
| 1단계 v1.1.0 D10(#323 사용) | 기본 `ghcr.io/vectorize-io/hindsight@sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06`(v1.1.0) | `sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06`(로컬 이미지, 재작업 때 읽음) | 해당 없음(next-stack 기본 이미지, 스펙은 main 이미지만 확인 요구) | 2026-10-31 06:35:08 ~ 06:35:40, commit 74b62c4 | 32초(up 시작 ~ `up ok`) | 기록 없음(사유: `up` 출력에 마이그레이션 줄이 없고, hindsight 컨테이너 로그는 07:22 `down`으로 지워져 다시 볼 수 없다. 스펙 #318은 v1.1.0 head 도 `c7e2f9a4d1b8`이라고 적지만 이 사본에서 재지 않았다) | `restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4` |
| 2단계 main D10 | `hc-upstream-main:b801b694d060` | `sha256:7707479b02f9b2008a3e1cb7fda308bc579b95732cf7cc25630daaf3c4124c02` | 라벨 `null`(두 라벨 없음) | 2026-10-31 08:11:54 ~ 08:12:21, commit 2e24245 | 27초(up 시작 ~ `up ok`) | `c7e2f9a4d1b8`(hindsight 로그, 아래) | `restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4` |

- 기동 명령(워크트리): `HC_NEXT_BACKUP_SHA256=8a4900ea... bash scripts/next-stack.sh up --llm opus --backup <운영 체크아웃>/backups/keep-arch-v2-20261027-043001`. 2단계는 앞에 `HC_NEXT_IMAGE=hc-upstream-main:b801b694d060`. 두 번 모두 exit 0, `pg_verifybackup ok`.
- 2단계 기동 전 이미지 확인(읽기 전용):
  - `docker image inspect -f '{{.Id}}' hc-upstream-main:b801b694d060` -> `sha256:7707479b02f9b2008a3e1cb7fda308bc579b95732cf7cc25630daaf3c4124c02`(기대값과 같음)
  - `docker image inspect hc-upstream-main:b801b694d060 -f '{{json .Config.Labels}}'` -> `null`(`com.docker.compose.project=hc-next`·`hc.next.commit` 둘 다 없음)
  - 기동 뒤 `docker inspect -f '{{.Config.Image}}' hc-next-hindsight-1` = `hc-upstream-main:b801b694d060`, 그 ID 같음. down 뒤에도 이미지가 그대로 남았다(ID 같음).
- 마이그레이션(main 사본 hindsight 로그): D10 의 `c8e2f4a6b1d3`에서 `b2429c65cc20 -> d4f7a2c9e1b5 -> e8c1a4f7b2d9 -> f2a9c4e7b1d3 -> c7e2f9a4d1b8` 5단계, **head `c7e2f9a4d1b8`**(기대값과 같음). 마지막 단계는 연 1·9999 자리표시 `event_date`를 지운다(데이터 변경. 스펙 #318은 v1.1.0 head 도 같다고 적는다).
- 복원 직후 stats(main): documents 2633, observations 4924(world 1916, experience 719), pending_consolidation 0, pending_operations 0, failed_operations 40. 런 1 D10 v1.1.0 과 같다.

사본 상태 확인(`bash scripts/next-stack.sh status`, 이 워커 셸 출력 기록):

| 시점 | 시각(KST) | status exit | 함께 본 것 |
|---|---|---|---|
| 1단계 up 전 | 2026-10-31 06:35:08 | 3(`hc-next: no resources`) | `docker ps -aq --filter label=hc-bench=1` 0줄 |
| 1단계 down 뒤 | 2026-10-31 07:22:11 | 3 | `down` exit 0 |
| 2단계 up 전 | 2026-10-31 08:11:49 | 3 | `docker ps -aq --filter label=hc-bench=1` 0줄, 이미지 ID·라벨 확인 |
| 2단계 down 뒤 | 2026-10-31 08:35:47 | 3 | `down` exit 0, main 이미지 ID 그대로 |
| 3단계 bench up 전 | 2026-10-31 08:35:57 | 3 | `docker ps -aq --filter label=hc-bench=1` 0줄 |
| 3단계 정리 뒤(끝) | 2026-10-31 08:36:33 | 3 | hc-bench ps·volume·network·lsof 4종 빈 출력(6절) |

## 2. 양립

### 2.1 게이트 9개 (사본 env, 하나씩, 08:12:34 ~ 08:13:07 KST)

명령: `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"; bash verify/<게이트>.sh`. 모든 게이트 stderr 첫 줄이 `target: gw=127.0.0.1:18888 project=hc-next ...`(gate8·9 는 `container=hc-next-hindsight-1`). gate6 은 사본 대상이 아니라 뺐다.

| 게이트 | exit | 요지(마지막 줄) |
|---|---|---|
| gate1_health | 0 | gateway /healthz 200, upstream reachable |
| gate2_banks | 0 | personal-alice + team-eng provisioned (PUT 200 + recall 200) |
| gate3_roundtrip | 0 | sync_retain->recall round-trip ok |
| gate4_isolation | 0 | isolation + team-retain attribution (forgery overwritten, round-trip) ok |
| gate5_adversarial | 0 | all adversarial inputs rejected |
| gate7_mcp_enforce | 0 | MCP rule enforcement holds |
| gate8_rerank_provider | 0 | reranker on MPS sidecar: provider=tei, degraded=false, synthetic 100cand 920.9ms < 4000ms |
| gate9_llm_fallback | 0 | 슬롯 5개(default·retain·reflect·consolidation·mental_model_refresh) failover 판정, cli=2.1.282 |
| gate_contract_drift | 0 | contract holds: bank-in-path routing + data-layer cross-bank isolation |

- 게이트가 쓰는 뱅크는 시험 뱅크(personal-alice, team-eng)다. 채점 대상 뱅크(`<bank>`)에는 쓰지 않는다.

### 2.2 MCP `tools/list` 계약

방법: 리포 밖 프로브로 사본 게이트웨이(18888, `/mcp/<bank>`)와 upstream 직결(컨테이너 안 `POST /mcp/` + `X-Bank-Id`) 두 쪽에서 `tools/list`를 떠서 `fixtures/mcp/tools_list.raw`(v1.1.0 원문, #285)와 도구 이름·`inputSchema` 속성·`required`·`additionalProperties`를 비교했다. 원문은 리포 밖에 있다(`fixtures/mcp/`는 바꾸지 않았다).

| 비교 | 도구 수 | 사라진 도구 | 생긴 도구 | 필수 인자 변화 | 스키마 차이(설명 문구 제외) | 설명 문구만 다른 도구 |
|---|---|---|---|---|---|---|
| v1.1.0 fixture -> upstream main | 39 -> 39 | 없음 | 없음 | 없음 | `recall`에 선택 인자 `rerank`(object 또는 null, 기본 null) 추가 | 35 |
| upstream main -> 게이트웨이 main | 39 -> 39 | 없음 | 없음 | 없음 | 없음 | 2(`retain`·`sync_retain`의 `tags` 설명, 게이트웨이가 태그 규칙을 붙인다 - 의도된 가공) |

- 게이트웨이 정책 집합(`gateway/policy.py`): READ 14·CORRECTIVE 10·RETAIN 2·KB_READ 3·DESTRUCTIVE 4 도구가 main `tools/list`에 모두 있다(소멸 0). 이름·필수 인자 변화 0. 설명 문구 변화는 게이트웨이가 읽지 않는다.
- `recall`·`reflect`의 `tags_match` enum 은 v1.1.0 과 같은 `["any","all","any_strict","all_strict","exact"]`, 기본 `any`.
- 주요 도구 필수 인자(v1.1.0 = main): `recall` `["query"]`, `reflect` `["query"]`, `retain` `["content"]`, `sync_retain` `["content"]`, `invalidate_memory` `["memory_id"]`.

### 2.3 retain 응답 형태 (게이트웨이 경유, 원문 1건 리포 밖 저장)

- 채집 시점: 채점과 양립용 백업이 끝난 뒤(08:35:20 KST). 채점 전에 retain 하면 기준 시각(2026-10-28T15:00Z) 뒤 기억이 채점 근거에 섞여 그 응답이 `post-cutoff-evidence` invalid 가 될 수 있어서 순서를 뒤로 뺐다. 그래서 양립용 백업에도 이 기억은 없다.
- 형태: HTTP 200, `text/event-stream`, `isError: false`. `content`는 text 블록 3개(1 = upstream JSON `{"status":"accepted","message":...,"operation_id":...}`, 2·3 = 게이트웨이 안내 문구(async retain operation 안내, timestamp 자동 기록)), `structuredContent` 키 `message`·`operation_id`·`status`.
  upstream 부분은 v1.1.0 fixture `retain_async_ok.raw`와 같은 모양이다. 그 operation 은 `completed`(`batch_retain`).
- 게이트웨이 파싱: `gate_effects.storage_status(recurrence.mcp_recall_payload(result))` = `(False, <operation_id>)`, `gate_effects.mcp_successes(...)` = `{요청 id: <operation_id>}`. 일치한다(파싱 불일치 없음).
- S0 의 `ToolError: retain: non-JSON result`는 main 비호환이 아니다. `bench/arch_v2_regression.py`의 `Gateway.call`이 text 블록을 전부 이어 붙여(게이트웨이 안내 문구 포함) JSON 으로 읽으려 해서 난다. 채점 경로(reflect·recall)는 안내 블록이 없어 영향이 없다(7절 범위 밖 발견).

### 2.4 `tags_match`

- 게이트웨이·길목이 보내는 값: 트리거 recall(`gateway/app.py` 412행)은 트리거의 `tags_match`(검증 집합 `context_triggers._TAGS_MATCH` = any·all·any_strict·all_strict) 또는 기본 `any`(`context_triggers.py` 665행), 원장 기록(933·1137행)은 호출자 값 또는 `any`. 운영 `policies/local/context-triggers.json`에는 `tags_match` 필드가 없다(읽기만, 전부 기본 `any`). 모두 upstream enum 다섯 값 안이다(`exact`는 게이트웨이가 스스로 보내지 않는다).
- main 사본 recall(같은 질의, `tags: ["project:hindsight-crew"]`, 08:13:37 KST):

| tags_match | 게이트웨이 MCP recall | upstream REST recall | 결과 행 수(게이트웨이 / REST) |
|---|---|---|---|
| any | 성공 | 200 | 11 / 12 |
| all | 성공 | 200 | 11 / 12 |
| any_strict | 성공 | 200 | 11 / 12 |
| all_strict | 성공 | 200 | 11 / 12 |
| exact | 성공 | 200 | 0 / 0 |

## 3. 기준선 main 채점 (재통합 없음, 합친 세트)

- 입력 sha256 대조(채점 전): 세트 A 고정 세트 `3fbe114b588af11e154b343639b8d5dcd9efe4b3c49343fb91559afbdd9d904d`, 세트 B 정답표 `6c50a56b42cf67b31b5d9592bb0ea4a63ac78e1fa891edcedcce6591e0566e6b`, 세트 B 고정 세트 `6798f3eb07f921baf00bf3c03b61224e34b4854fd89f355ce0ffc3c5803451a3`, 키 표 `3575b1c02e5baaca34c2e54f9dbb57ba87b8fe460ed636e0f1eef9d3d604138a`. 모두 #323 산출물 값과 같다. 두 정답표 세 값 일치 확인 `{'query_date': True, 'ignore_mentioned_at_from': True, 'query_instruction': True}`.
- 명령(워크트리 2e24245, `next_env` 뒤. `A2=(--answer-key <세트 A> --answer-key <세트 B> --evidence <r1-fixed-set> --evidence <r2-setb-fixed-set> --key-split <r2-key-split>)`):
  (a) `score "${A2[@]}" --paths a`를 README 반복 그대로(유효 3개 또는 시도 6회), (b) `score "${A2[@]}" --paths b --share-pages`, `--paths b --share-pages --page-suffix -g2`, (c) `score "${A2[@]}" --paths c`. 순서 (a) -> (b) -> (c), 08:14:25 ~ 08:34:02 KST.
  `--share-pages`는 코디네이터 지시로 붙였다. 새 D10 사본이라 같은 이름 페이지가 없어 두 생성 모두 새로 만들었다(detail 의 `reused` 없음, `deleted_evidence` 0).

경로·run별 출력(#326 judge-arms manifest 입력, 디렉터리는 `~/.local/share/hindsight-bench/arch-v2/` 아래):

| 경로·run | 출력 디렉터리 | summary.json sha256 | detail.json sha256 |
|---|---|---|---|
| a #1 | `score-20261031-081425` | `cdacc5516e9debfeb24d22e6e8cca5b5af04da452563372c4712877e98ed5292` | `207ccbd955c5c01e4e719f165ed6c9081f580fd94cb34eb49fffc0d0e9f59c33` |
| a #2 | `score-20261031-081622` | `b6e3f8c1502bb7a1be02a30eabb4cab62ce4853f666e6686aa78e7452c7c6b28` | `7aaddb14a4422165235cda9ae2e9c36164af7848171ee9875436c6207fbbb457` |
| a #3 | `score-20261031-081838` | `5fbb2b44db21361780d6e3616669103648780b1b5e0c76f4e93edf63eb0abb3e` | `54787dfcbf4c5fd8de055b8030248365d920a227bc3594ca30ceb23a6e699b4f` |
| a #4 | `score-20261031-082107` | `d0bb8dd1f9dbd3f39c93756eb4bb2d4a11ddb58599a921f1ea1d6d45d2c96db4` | `313a2df88e61afd6e2bcc0371d8faa5e7cc158c68b8305df95b2148d7917e4b6` |
| a #5 | `score-20261031-082322` | `061cb0d0eab915812c620dbd6f7f33f8beed3353f997cffcc82c811a3d7ce482` | `d42fa53850fa0fb936b62b0fb7fd6765960bd249fa0c1312b1696596157a8ed8` |
| b full | `score-20261031-082513` | `8b51d4f4e8b4f861163c9017a7788dfa93869c387aebb1a5f4c0ad79509139a5` | `c1a8b8f3fdd4e21d8c6ef2eb0e23eacd895879a578e0a148e4731edae9acfd5f` |
| b full -g2 | `score-20261031-082818` | `c7708f7190a04f3ee8f4bc2b1e62a3be3c0be87ee6b9626b2aaeb128224bf621` | `2533ad080e4d4551de79282ac63fcc75d69b37ca595325bb1b9e9c8e3ac06cc1` |
| c | `score-20261031-083124` | `a4840a2f097e6ae8e9dbaf0e226cd0faefec3212499ed9c36c20d9ff981f89b8` | `767310716af0a06725976fdb8a1adcd1c43c06e9f90109ebe47028fa13b78b73` |

(a) run 별 (끝난 항목 open, 대조군 open, 판정 가능): **시도 5회, 유효 run 3개(#2·#4·#5), 대조군 0 run(undecidable) 2개(#1·#3)**, invalid 0.

| (a) 시도 | 끝난 항목 open(합친 30) | 세트 A (15) | 세트 B (15) | 대조군 open(합친 6) | invalid | 판정 가능 |
|---|---|---|---|---|---|---|
| 1 | 2 | 2 | 0 | 0 | 0 | undecidable |
| 2 | 1 | 1 | 0 | 2 | 0 | valid |
| 3 | 0 | 0 | 0 | 0 | 0 | undecidable |
| 4 | 1 | 1 | 0 | 3 | 0 | valid |
| 5 | 4 | 3 | 1 | 1 | 0 | valid |

경로·run 별 세트·키 분리(끝난 항목 open, 분모는 합친 세트):

| 경로·run | 끝난 open 합친 | 세트 A | 세트 B | 키 있음 (15) | 키 없음 (14) | 미정 K02 | 대조군 open 합친 (6) | 세트 A 대조군 (3) | 세트 B 대조군 (6) | invalid |
|---|---|---|---|---|---|---|---|---|---|---|
| a #1 | 2 | 2 | 0 | 0 | 1 | 1 | 0 | 0 | 0 | 0 |
| a #2 | 1 | 1 | 0 | 0 | 1 | 0 | 2 | 2 | 2 | 0 |
| a #3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| a #4 | 1 | 1 | 0 | 1 | 0 | 0 | 3 | 3 | 3 | 0 |
| a #5 | 4 | 3 | 1 | 3 | 0 | 1 | 1 | 1 | 1 | 0 |
| b full | 2 | 2 | 0 | 2 | 0 | 0 | 3 | 3 | 3 | 0 |
| b full -g2 | 3 | 3 | 0 | 2 | 0 | 1 | 3 | 3 | 3 | 0 |
| c | 5 | 4 | 1 | 2 | 3 | 0 | 2 | 2 | 2 | 0 |

- 새 대조군 BC01~BC03 은 어느 경로·run 에서도 open 이 아니었다(대조군 open 은 전부 공유 대조군 C01~C03, 기준선 v1.1.0 과 같다).

판정 값(스펙 #318 산식, 합친 세트 끝난 항목 open. `judge-arms` 출력과 같다, 4절):

| 경로 | 값 | 기준선 반복 차 | 대조군 open 값 | 세트 A | 세트 B | 키 있음 | 키 없음 | 미정 | invalid | 판정 불가 |
|---|---|---|---|---|---|---|---|---|---|---|
| (a) 유효 run 3개 평균 | 2.00 (1, 1, 4) | 3 | 2.00 | 1.67 | 0.33 | 1.33 | 0.33 | 0.33 | 0 | 아님 |
| (b) 두 생성 평균 | 2.50 (2, 3) | 1 | 3.00 | 2.50 | 0.00 | 2.00 | 0.00 | 0.50 | 0 | 아님 |
| (c) 1회 | 5 | 0 | 2.00 | 4.00 | 1.00 | 2.00 | 3.00 | 0.00 | 0 | 아님 |

항목 id x 경로 판정:

| id | 세트 | a #1 | a #2 | a #3 | a #4 | a #5 | b full | b full -g2 | c |
|---|---|---|---|---|---|---|---|---|---|
| K01 | A | not-open | not-open | not-open | not-open | open | not-open | open | open |
| K02 | A | open | not-open | not-open | not-open | open | not-open | open | not-open |
| K03 | A | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K04 | A | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K05 | A | not-open | not-open | not-open | not-open | not-open | not-open | not-open | open |
| K06 | A | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K07 | A | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K08 | A | not-open | open | not-open | not-open | not-open | not-open | not-open | open |
| K09 | A | not-open | not-open | not-open | open | open | open | open | not-open |
| K10 | A | not-open | not-open | not-open | not-open | not-open | open | not-open | not-open |
| K11 | A | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K12 | A | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K14 | A | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K15 | A | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K16 | A | not-open | not-open | not-open | not-open | not-open | not-open | not-open | open |
| B02 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B03 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B04 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B05 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B06 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B07 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B11 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B12 | B | not-open | not-open | not-open | not-open | open | not-open | not-open | not-open |
| B13 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B14 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B15 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B16 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | open |
| B17 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B18 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B19 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| C01 | A·B | not-open | open | not-open | open | not-open | open | open | not-open |
| C02 | A·B | not-open | not-open | not-open | open | not-open | open | open | open |
| C03 | A·B | not-open | open | not-open | open | open | open | open | open |
| BC01 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC02 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC03 | B | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |

경로별 비용(`llm-requests`를 경로 구간으로 나눠 provider 별로 셌다: 생성형 = local·tei 밖, 임베딩 = local, 리랭크 = tei. 괄호는 도구 `metrics.json`의 `llm_calls`, 구간 끝 경계 1행 차이):

| 경로·run | 질의 수 | 소요(초) | llm-requests (metrics) | 생성형 | 임베딩 | 리랭크 | 생성형 operation | 생성형 토큰(입력/출력) | 판정 사유 |
|---|---|---|---|---|---|---|---|---|---|
| a #1 | 1 | 116.6 | 22 (21) | 9 | 7 | 6 | reflect/reflect 1, reflect/reflect_tool_call 8 | 679511/5911 | absent 34, open-section 2 |
| a #2 | 1 | 136.3 | 25 (24) | 10 | 8 | 7 | reflect/reflect 1, reflect/reflect_tool_call 9 | 837658/8676 | absent 33, open-section 3 |
| a #3 | 1 | 148.0 | 27 (26) | 10 | 9 | 8 | reflect/reflect 1, reflect/reflect_tool_call 9 | 822958/6728 | absent 36 |
| a #4 | 1 | 135.2 | 25 (24) | 10 | 8 | 7 | reflect/reflect 1, reflect/reflect_tool_call 9 | 835992/8075 | absent 32, open-section 4 |
| a #5 | 1 | 110.3 | 19 (18) | 8 | 6 | 5 | reflect/reflect 1, reflect/reflect_tool_call 7 | 598974/6657 | absent 31, open-section 5 |
| b full | 7 | 185.4 | 142 (142) | 52 | 52 | 38 | refresh_mental_model/reflect 7, refresh_mental_model/reflect_tool_call 45 | 2083833/37548 | absent 31, open-section 5 |
| b full -g2 | 7 | 185.8 | 142 (142) | 52 | 52 | 38 | refresh_mental_model/reflect 7, refresh_mental_model/reflect_tool_call 45 | 2145104/37321 | absent 30, open-section 6 |
| c | 36 | 157.0 | 72 (71) | 0 | 36 | 36 | - | 0/0 | latest-done 27, latest-open 7, no-status 2 |

- 기준선 main 생성형 합계: (a) 47건(5회 시도), (b) 104건, (c) 0건. 완전 재통합 상한 횟수·시간에 들지 않는다(스펙 #318 상한).
- 페이지: (b) 7개(project_hint 합집합 6 + 전체 1) x 2회. (b) 두 생성 모두 `deleted_evidence` 0.

## 4. 기준선 v1.1.0 과 나란히 (참고, 판정은 #326 judge-arms)

`judge-arms`(질의 0회)를 리포 밖 manifest(`baseline-v1.1.0` = #323 머지본 rescore summary 6개, `baseline-main` = 위 8개, `read_path_only`)로 돌렸다. 판정은 #326 이 낸다. 이 값은 위 표의 검산이다.

| 경로 | 기준선 main | 기준선 v1.1.0 | W | 대조군 open main / v1.1.0 |
|---|---|---|---|---|
| a | 2.00 | 1.67 | 3 | 2.00 / 1.67 |
| b-full | 2.50 | 2.50 | 1 | 3.00 / 3.00 |
| c | 5.00 | 5.00 | 0 | 2.00 / 2.00 |

- 출력 첫 줄: `read-path-only(baseline-main) vs baseline-v1.1.0: 효과 없음 [no-effect]`, 판정 불가 경로 없음, (a) 대조군 0 run 수 2, 개선 막힘 False.
- (c)는 두 이미지에서 항목별 판정까지 같다(open = K01·K05·K08·K16·B16, 대조군 C02·C03). main 읽기 경로 변경이 recall 최신 상태 판정을 바꾸지 않았다.
- (a)는 main 에서 대조군 0 run 이 2개 나왔다(v1.1.0 은 0개). reflect 1회 응답의 흔들림 범위 안이고, 유효 run 3개는 시도 5회로 채웠다.

## 5. 양립용 백업과 v1.1.0 복제본 verify

- 백업(채점 뒤, retain 프로브 전, 08:34:54 KST): `bash scripts/backup.sh` exit 0, `backup ok: backups/20261031-083455/pg0.tar.gz (165969551 bytes)`. 워크트리 `backups/`에 썼고(운영 체크아웃 `backups/`에는 생기지 않음 확인) 곧바로 리포 밖으로 옮겼다.
- 경로: `~/.local/share/hindsight-bench/arm-backups/r2-main-compat-20261031-083455/`(0700), `pg0.tar.gz` sha256 `bd18356bfd39419d2540040ed052d19de8b921d4b3441571657e10f1e9222b1d`.
- `LINEAGE`(0600, `schema_version` 2, #323 README 모양): `arm` `r2-main-compat`, `source_backup` D10, `settings` {}, `directives` [], `document_patches` 0, `reconsolidation` 없음(null, llm_requests 0), `pages` full 접미사 "", `image` `{"tag": "hc-upstream-main:b801b694d060", "id": "sha256:7707479b..."}`, `invalidations` null, `expected_bank_overrides` = 백업 직전 사본 overrides(키 `memory_defense` 하나). **LINEAGE sha256 `088eb3930249f1c14c24a1b41f11454179865c6f2c2c97be48116c3d1952466e`**.
- 3단계(hc-next down 뒤, 08:35:57 ~ 08:36:33 KST, `bench/README.md` "1단계 arm 벤치" 1)·2) s2·4) verify·5)만, 팔·runner 없음):
  - `python3 -B bench/replica.py sidecar-up r1 --port 18781` exit 0
  - `python3 -B bench/replica.py up --set s2 --backup <양립용 백업>/pg0.tar.gz --sidecar-port 18781 --expected-overrides <LINEAGE>` exit 0, `backup_sha256` `bd18356bfd39419d2540040ed052d19de8b921d4b3441571657e10f1e9222b1d` = LINEAGE `backup.pg0_sha256`, 이미지 `sha256:50311d02...`(v1.1.0)
  - `python3 -B bench/replica.py verify --set s2` exit 0, **`pass: true`**, `bank_overrides_equal` true, `bank_config_diff_unexplained` [], `env_wrong_override` [], `image_equal` true, 동결 stats documents 2633.
  - 정리: `set-down s2`·`sidecar-down r1`·`down` 모두 exit 0, 아래 확인 4개 빈 출력.
- 결론: main 사본이 만든 백업이 v1.1.0 복제본에 복원·verify 된다(alembic head 같음). #324·#325 벤치(데이터 효과)의 전제가 선다.

## 6. 사본·복제본 구간과 정리

canary-exclude 형식(KST, up 부터 down 확인 + 300초):

```
2026-10-31 06:35:08 2026-10-31 07:27:11  # #319 1단계 v1.1.0 hc-next D10 사본(#323 사용, up 06:35:08 ~ down 확인 07:22:11 +300s)
2026-10-31 08:11:54 2026-10-31 08:40:47  # #319 2단계 기준선 main hc-next D10 사본(up 08:11:54 ~ down 확인 08:35:47 +300s)
2026-10-31 08:35:57 2026-10-31 08:41:33  # #319 3단계 bench 복제본(main 양립 백업 verify, up 08:35:57 ~ 정리 확인 08:36:33 +300s)
```

- 끝 확인(08:36:33 KST): `bash scripts/next-stack.sh status` -> `hc-next: no resources` exit 3. `docker ps -aq --filter label=hc-bench=1`, `docker volume ls -q --filter label=hc-bench=1`, `docker network ls -q --filter name=hc-bench`, `lsof -nP -iTCP:18780-18799 -sTCP:LISTEN` 모두 빈 출력.
- 운영 스택·운영 뱅크에는 쓰지 않았다. main 이미지는 지우거나 다시 빌드하지 않았다.

## 7. 범위 밖 발견

- `bench/arch_v2_regression.py` `Gateway.call`(666-688행, 2e24245 기준): 게이트웨이가 retain 응답에 안내 text 블록을 덧붙이면 블록을 전부 이어 붙여 `json.loads`하므로 retain 은 늘 `non-JSON result`로 실패한다(S0 기록의 원인). 지금 채점 경로(reflect·recall)는 영향이 없다. 첫 text 블록이나 `structuredContent`를 읽으면 고쳐진다(약 3줄, 파일 1개). 하네스는 #323 전담이라 고치지 않았다.
