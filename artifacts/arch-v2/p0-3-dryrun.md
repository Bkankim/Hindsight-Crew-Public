# P0-3 dry run: 세 경로 상태 채점 사전 점검 (issue #287, 상위 #284)

1단계 기준선 arm 전의 사전 점검이다. **정본 점수가 아니다.** 같은 D10 사본(`p0-3-evidence.md`의 대상·복원원·이미지와 같다)에서 score 를 세 경로 모두 1회 돌렸고(설정 불변),
채점 규칙을 그 원문으로 고친 뒤 `rescore`(질의 0회)로 다시 판정했다. 이 파일에는 항목 id·경로·판정과 집계만 쓴다.

## 실행

- score: `python3 -B -m bench.arch_v2_regression score --answer-key <정답표> --evidence <evidence summary.json>`(20:40:29~20:48:36 KST, exit 0, 세트 19건 = 당시 evidence 표)
- 최종 판정: `python3 -B -m bench.arch_v2_regression rescore --answer-key <정답표> --detail <score detail.json> --evidence <최종 evidence summary.json>`
  (세트 18건 = `p0-3-evidence.md` 표, K13 뺌). 같은 명령을 두 번 돌려 요약 JSON 이 같았다(`identical: True`).
- 질의 인자: 모든 질의에 정답표 `query_instruction`, recall 은 `query_timestamp=2026-10-28T14:59:59Z` 고정, reflect 는 `include_based_on: true`·`budget: mid`.
  (b)는 프로젝트별 페이지 8개(`arch-v2-status-<hint|all>`, 기본 trigger = delta, 첫 생성은 full)를 upstream 직결 REST 로 만들었다. 8개 operation 전부 `completed`(각 약 214초, 동시 진행).
- 리포 밖 출력(0700): `~/.local/share/hindsight-bench/arch-v2/score-20261029-204029/`(`detail.json` 원문, `metrics.json`, `summary.json`),
  최종 판정 `~/.local/share/hindsight-bench/arch-v2/rescore-20261029-212441/`(규칙 v3, `summary.json`, `reasons.json`. v2 판정은 `rescore-20261029-210111/`)

## 측정 전후 사본 상태

| 시점 (UTC) | `Config.Image` | documents | observations | pending_consolidation | pending_operations | failed_operations |
|---|---|---|---|---|---|---|
| 복원 직후 2026-10-29T11:34:21Z | `ghcr.io/vectorize-io/hindsight@sha256:50311d02...d2aa06` | 2633 | 4924 | 0 | 0 | 40 |
| score·rescore 뒤 2026-10-29T12:01:20Z | `ghcr.io/vectorize-io/hindsight@sha256:50311d02...d2aa06` | 2633 | 4924 | 0 | 0 | 40 |

- 명령: `python3 -B -m bench.arch_v2_regression stats`(upstream 직결 `GET .../banks/personal-demo/stats`), 이미지는 `docker inspect -f '{{.Config.Image}}' hc-next-hindsight-1`.
- 복원 직후 `pending_consolidation`이 이미 0이라 기다리지 않았다. 페이지 8개 생성 뒤에도 문서·관찰 수가 그대로다(페이지는 기억을 쓰지 않는다).
- nodes_by_fact_type 은 전후 모두 world 1916, experience 719, observation 4924.

## 판정 (최종 규칙 v3, v2 와 같다)

| id | a_reflect_list | b_knowledge_page_status | c_recall_status |
|---|---|---|---|
| K01 | open | not-open | open |
| K02 | not-open | not-open | not-open |
| K03 | not-open | not-open | not-open |
| K04 | not-open | not-open | not-open |
| K05 | open | not-open | open |
| K06 | not-open | not-open | not-open |
| K07 | not-open | not-open | open |
| K08 | not-open | not-open | not-open |
| K09 | open | not-open | not-open |
| K10 | not-open | not-open | not-open |
| K11 | not-open | not-open | not-open |
| K12 | not-open | not-open | not-open |
| K14 | not-open | not-open | not-open |
| K15 | not-open | open | not-open |
| K16 | not-open | not-open | not-open |
| C01 | open | open | not-open |
| C02 | open | open | open |
| C03 | open | open | open |

| 경로 | open | not-open | invalid | 질의 수 | 소요(초) | LLM 호출 |
|---|---|---|---|---|---|---|
| a_reflect_list | 6 | 12 | 0 | 1 | 190.2 | 27 |
| b_knowledge_page_status | 4 | 14 | 0 | 8 | 215.9 | 137 |
| c_recall_status | 5 | 13 | 0 | 19 | 80.3 | 37 |

- 집계의 질의 수·소요·LLM 호출은 score 1회(20:40~20:48) 값이다. LLM 호출은 경로 구간의 `GET .../llm-requests?start_date&end_date` `total`이라
  그 구간의 뱅크 전체 LLM 요청(백그라운드 작업 포함)이다. 사본 recall 도 LLM 을 부른다(recall 19회 구간 37건).
- 대조군(C01~C03, expect open): (a) 3/3 open, (b) 3/3 open, (c) 2/3 open. 경로가 열린 일을 열린 일로 내놓는 감도는 (a)(b)가 높다.
- 끝난 항목이 open 으로 나온 수(상태 오답): (a) 3/15(K01, K05, K09), (b) 1/15(K15), (c) 3/15(K01, K05, K07).

## LLM 사용량 (사본, 운영 LLM = 운영자 구독)

| 구간 | 호출 | 소요 |
|---|---|---|
| (a) reflect 1회 | llm-requests 27건, reflect `usage` input 543,704 / output 10,192 토큰(cached 44,133) | 190.2초 |
| (b) 페이지 생성 8개 | llm-requests 137건 | 215.9초 |
| (c) recall 19회 | llm-requests 37건 | 80.3초 |
| evidence 5회(회당 recall 37회) 등 나머지 | 378건(전체 - 위 셋, 백그라운드 포함) | 회당 약 145초 |
| 사본 전체(up 20:33:45 ~ 21:01 KST, 조회 창 UTC 11:33:45~12:10:00) | llm-requests 579건 | - |

## 채점 규칙 선택 (경쟁 가설 -> 증거 -> 판정)

합성 사례는 `tests/test_arch_v2_regression.py`, 실측 사례는 이 run 의 원문(리포 밖)이다.

| 가설 | 증거 | 판정 |
|---|---|---|
| H1 응답 전체 본문 keyword | 합성: 서두 "끝났다" 언급·DONE 절을 open 으로 센다(`test_sections_ignore_mentions_outside_open`) | 기각 |
| H2 구조화 출력(`response_schema`) | 사용자가 읽는 본문이 아닌 별도 추출을 채점, 추출 LLM 호출 추가, (b)는 페이지 trigger 설정 변경 | 기각 |
| H3 OPEN 절 + 줄 전체 부분 문자열 (v1) | 실측: `###` 하위 제목이 절을 끝내 (a) 19건 전부 not-open(대조군 0/3). 고친 뒤(v1.1)에도 번호 접두어(`#2`->`#250`)·설명 안 다른 일 언급·프로젝트 이름 keyword 로 끝난 항목 6건이 open | 기각 |
| H4 OPEN 절(수준 인식) + 줄 주어 + 영숫자 경계 + 맨 번호 프로젝트 동반 (v2) | 실측: 대조군 (a) 3/3·(b) 3/3 유지, 오탐 사례 제거. 테스트 6건으로 고정 | 채택 |
| (c) R1 1위 결과 | 합성: 옛 인계 1위·새 완료 2위를 open 으로 틀린다(`newer-done`) | 기각 |
| (c) R2 완료 기억 존재 | 합성: 완료 뒤 다시 열린 대조군 모양을 not-open 으로 틀린다(`reopened`) | 기각 |
| (c) R3 최신 상태 진술, 태그 우선 (v1) | 실측: 대조군 0/3. 범위 태그를 물려받은 관찰이 태그와 반대 내용(`kind:completion` 관찰의 "남은 작업", `kind:handoff` 기억의 "배포 완료") | 기각 |
| (c) R4 최신 상태 진술, keyword 문장 단서 우선 (v2) | 실측: 대조군 2/3. 테스트로 고정(`test_item_state_text_first`) | 채택 |

규칙 버전별 open 수(같은 원문):

| 규칙 | 세트 | (a) open | (a) 대조군 | (b) open | (b) 대조군 | (c) open | (c) 대조군 |
|---|---|---|---|---|---|---|---|
| v1 (score 1회 당시) | 19 | 0 | 0/3 | 9 | 3/3 | 4 | 0/3 |
| v1.1 (하위 제목 수정) | 19 | 9 | 3/3 | 9 | 3/3 | 4 | 0/3 |
| v2 | 18 | 6 | 3/3 | 4 | 3/3 | 5 | 2/3 |
| v3 (최종, final-review rework) | 18 | 6 | 3/3 | 4 | 3/3 | 5 | 2/3 |

## 규칙 v3 재판정 (final-review `.final-review/20261029-211057` rework #287-1)

- 바뀐 규칙: 줄 주어가 굵은 구간 뒤 설명 구분자(`:`·`. `·` - `·em dash·`(`·`,`·`|`)에서 끊긴다(구분자가 없으면 이어 본다), OPEN 제목은 정확히 OPEN(괄호 덧붙임 허용),
  `[미완료]` 라벨은 done 이 아니다, based_on 이 비면 (a)(b) invalid(no-provenance), upstream `{"error":...}` 본문은 ToolError, 쓰기 대상에 같은 이름 페이지가 있으면 질의 전 exit 1.
- 명령: `rescore --answer-key <정답표> --detail <score-20261029-204029/detail.json> --evidence <evidence-20261029-205831/summary.json>` 2회(질의 0회, 사본 불필요). 두 요약이 같다(`identical: True`).
- 전후 차이: **없음** - 18건 x 3경로 판정이 v2 와 모두 같다. 이 run 의 응답에는 새 규칙이 가르는 모양(설명 구분자 뒤 다른 일 언급, OPEN 이 아닌 open 제목, 빈 based_on)이 없었다.
- evidence: 재판정 명령이 없다(evidence 는 recall 원문 행을 저장하지 않고 받아들인 근거만 남긴다). `[미완료]` 수정은 완료 판정을 없애기만 하므로 받아들인 근거 116건의 라벨을 확인했다:
  `미완` 라벨 0건 -> 근거 표는 그대로다. error 본문 거부 수정의 영향은 evidence 쪽 저장 원문으로 확인할 수 없다(옛 코드는 그런 본문을 오류로 올리지 않았다). score 의 (c) 원문 19건은 결과 행이 모두 비어 있지 않다(error 본문은 `results: []`라 해당 없음).

## 남은 오차 (규칙 v3 에서도 남는 것)

- K01 (a)(c): 이 항목 번호가 다른 열린 후속 일의 줄 주어·기억 문장에 함께 나온다. 상태 오답인지 keyword 오탐인지 이 규칙으로는 가를 수 없다.
- C01 (c): 정답표 keyword 하나가 다른 완료 일의 파일 이름과 같아 그 완료 기억이 최신 상태로 잡힌다. 정답표 keyword 를 좁히면 풀린다(정답표 변경은 범위 밖).
- K05 (c): 완료 근거가 날짜 없는 기억뿐이라 날짜가 있는 옛 인계 관찰이 최신으로 이긴다(날짜 없는 기억은 가장 오래된 것으로 친다).
- 의역된 OPEN 줄은 (a)(b)에서 못 잡는다(not-open 쪽 오차). 대조군 open 수로 감도를 본다.
- 1단계 arm 채점 전에 이 오차를 정답표 keyword 정리(오케스트레이터)와 함께 다시 본다.

## 부하 구간 (canary-exclude 형식)

```
2026-10-29 20:33:45 2026-10-29 21:06:30  # #287 P0-3 hc-next D10 사본(up 20:33:45 ~ down 확인 21:01:30 +300s): evidence 5회·reflect·지식 페이지 8개·recall 채점
```

- down: `bash scripts/next-stack.sh down` exit 0(21:01:30), 직후 `docker ps -aq --filter name=hc-next`·`docker volume ls -q --filter name=hc-next`·`docker ps -aq --filter label=hc-bench=1` 빈 출력, `status` exit 3.
