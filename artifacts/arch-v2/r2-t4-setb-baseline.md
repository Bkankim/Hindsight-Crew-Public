# 런 2 T4: 세트 B와 기준선 v1.1.0 채점 (issue #323)

상위: #318. 도구 변경은 `bench/arch_v2_regression.py`(합친 세트·키 분리·페이지 공유·(a) 판정 불가 run·`judge-arms`), 사용법은 `bench/README.md` "세트 두 개, (a) 판정 불가 run, arm 판정" 절.
이 문서에는 id·판정·해시만 쓴다(항목 이름·질의·응답 원문·뱅크 실명 없음, 공개 전환 가드 #4).

## 1. 사본과 복원

- 사본: #319가 띄운 hc-next v1.1.0 D10 사본(up 2026-10-31 06:35:40 KST, 기본 이미지 v1.1.0, 커밋 74b62c4). 이 티켓은 띄우지도 내리지도 않았다.
- `restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4` (#319 기동 출력, 코디네이터 사본 차례 메시지로 전달받음)
- 사본에 한 일: evidence(식별자 recall), score(페이지 생성 포함), 읽기 GET(`memories/list`·`llm-requests`·멘탈 모델). 설정·문서 PATCH·무효화·retain 없음.
- 사본 사용 구간(canary-exclude 형식):
  `2026-10-31 06:55:22 2026-10-31 07:26:49  # #323 기준선 v1.1.0·세트 B evidence, #319 hc-next D10 사본 사용(06:55:22 ~ 07:21:49 +300s)`

## 2. 세트 B

선정 규칙(출처만, arm 점수·#320 코드 미사용):
- 닫힌 이슈: 소유자 리포들에서 D10 스냅숏(2026-10-26T19:30Z) 전에 COMPLETED 로 닫힌 이슈 228건(세트 A·대조군 번호 제외)에서 `random.Random(323).sample(..., 20)`, 표본 순서대로 근거 확인. 근거 있는 첫 8건까지 정답표에 넣었다(그 앞의 근거 없음 4건은 표에 "없음"으로 남는다).
- 이슈 없는 끝난 일: D10 사본 `kind:completion` 원문(world·experience, mentioned_at < 2026-10-26T19:30Z, 본문에 `#번호` 없음) 562개 문서에서 `random.Random(3231).sample(..., 30)`, 표본 순서대로 작업 완료 기록만(조사·결정·피드백·비업무 기록 제외, 세트 A 항목과 같은 일 제외) 10건을 후보로 근거 확인, 첫 7건을 넣었다.
- 남는 후보 11건(닫힌 이슈 8, 이슈 없음 3)은 할당 밖이라 채점하지 않는다(정답표 `excluded`).
- 새 대조군: 2026-10-28 시점에 열려 있던 이슈(세트 A 대조군·항목 번호 제외)에서 `random.Random(3232).sample(..., 2)` = BC01·BC02, 열린 이슈 없는 일 1건 = BC03.
- 키 유무는 출처로만 정했다(닫힌 이슈 = true, 이슈 없음 = false).
- keyword: 닫힌 이슈는 번호 식별자 하나(세트 A K05·K06·K11·K12 방식), 이슈 없는 일은 커밋 해시·고유 작업 이름. project_hint 는 세트 A에 있는 프로젝트일 때만 달고 나머지는 전체 페이지(세트 A K08·K16 방식). 그래서 (b) 페이지는 세트 A와 같은 7개다.

| id | 출처 종류 | 키 유무 | 근거 | mentioned_at (UTC 날짜) | 근거 종류 / 사유 |
|---|---|---|---|---|---|
| B01 | 닫힌 이슈 | true | 없음 -> 세트에서 뺌 | - | no-done-memory-before-cutoff |
| B02 | 닫힌 이슈 | true | 있음 | 2026-09-27 | kind:completion |
| B03 | 닫힌 이슈 | true | 있음 | 2026-09-30 | kind:completion |
| B04 | 닫힌 이슈 | true | 있음 | 2026-09-30 | kind:completion |
| B05 | 닫힌 이슈 | true | 있음 | - | kind:completion |
| B06 | 닫힌 이슈 | true | 있음 | 2026-10-07 | kind:completion |
| B07 | 닫힌 이슈 | true | 있음 | 2026-10-17 | kind:completion |
| B08 | 닫힌 이슈 | true | 없음 -> 세트에서 뺌 | - | no-done-memory-before-cutoff |
| B09 | 닫힌 이슈 | true | 없음 -> 세트에서 뺌 | - | no-done-memory-before-cutoff |
| B10 | 닫힌 이슈 | true | 없음 -> 세트에서 뺌 | - | no-done-memory-before-cutoff |
| B11 | 닫힌 이슈 | true | 있음 | - | kind:completion |
| B12 | 닫힌 이슈 | true | 있음 | 2026-10-16 | kind:completion |
| B13 | 이슈 없음(D10 kind:completion) | false | 있음 | 2026-09-11 | kind:completion |
| B14 | 이슈 없음(D10 kind:completion) | false | 있음 | 2026-08-31 | kind:completion |
| B15 | 이슈 없음(D10 kind:completion) | false | 있음 | 2026-08-23 | kind:completion |
| B16 | 이슈 없음(D10 kind:completion) | false | 있음 | 2026-10-18 | kind:completion |
| B17 | 이슈 없음(D10 kind:completion) | false | 있음 | 2026-08-22 | kind:completion |
| B18 | 이슈 없음(D10 kind:completion) | false | 있음 | 2026-10-20 | kind:completion |
| B19 | 이슈 없음(D10 kind:completion) | false | 있음 | - | kind:completion |
| C01 | 공유 대조군(세트 A 바이트 복사) | true | 대조군(근거 확인 대상 아님) | - | - |
| C02 | 공유 대조군(세트 A 바이트 복사) | true | 대조군(근거 확인 대상 아님) | - | - |
| C03 | 공유 대조군(세트 A 바이트 복사) | true | 대조군(근거 확인 대상 아님) | - | - |
| BC01 | 대조군(10-28 열린 이슈) | true | 대조군(근거 확인 대상 아님) | - | - |
| BC02 | 대조군(10-28 열린 이슈) | true | 대조군(근거 확인 대상 아님) | - | - |
| BC03 | 대조군(10-28 열린 이슈 없는 일) | false | 대조군(근거 확인 대상 아님) | - | - |

- 고정 세트 B(근거 확인 뒤 남은 세트): 끝난 15건(키 있음 8, 없음 7) + 대조군 6건(C01~C03, BC01~BC03, 키 있음 5, 없음 1) = 21건. 키 있음·없음 각 5건 이상이라 보충은 하지 않았다.
- 합친 세트(A∪B, 같은 id 한 번): 끝난 30건(키 있음 15, 없음 14, 미정 1 = K02) + 대조군 6건 = 36건.

| 파일(리포 밖, 0600) | sha256 |
|---|---|
| `~/.local/share/hindsight-bench/arch-v2/r2-setb-answer-key.json` | `6c50a56b42cf67b31b5d9592bb0ea4a63ac78e1fa891edcedcce6591e0566e6b` |
| `~/.local/share/hindsight-bench/arch-v2/r2-setb-fixed-set.json` | `6798f3eb07f921baf00bf3c03b61224e34b4854fd89f355ce0ffc3c5803451a3` |
| `~/.local/share/hindsight-bench/arch-v2/r2-key-split.json` | `3575b1c02e5baaca34c2e54f9dbb57ba87b8fe460ed636e0f1eef9d3d604138a` |
| 세트 A 고정 세트 `~/.local/share/hindsight-bench/arch-v2/r1-fixed-set.json`(대조, 읽기만) | `3fbe114b588af11e154b343639b8d5dcd9efe4b3c49343fb91559afbdd9d904d` |

- 확정 시각: **2026-10-31 07:02:55 KST**. #320 브랜치·PR·`gateway/item_keys.py`는 이 시각 전에도 뒤에도 열지 않았다.
- 고정 세트 B = 확정 정답표 evidence 출력 `evidence-20261031-070126/summary.json`의 복사본(queries 21, remaining 21).
- 키 유무 표 세트 A: K01·K05·K06·K09·K10·K11·K12 true, K03·K04·K07·K08·K14·K15·K16 false, K02 null(#322 확정), C01~C03 true(스펙 그대로).
- 두 정답표의 세 값 일치: `query_date` True, `ignore_mentioned_at_from` True, `query_instruction` True(README 확인 명령, `json.dumps` 비교와 UTF-8 바이트 비교 모두). 그래서 **페이지를 공유**하고(합친 세트 채점, 접미사 기본값: full 없음, 두 번째 생성 `-g2`) (a) 응답도 공유한다.

## 3. 기준선 v1.1.0 채점 (재통합 없음, 합친 세트)

명령(워크트리, `next_env` 뒤. `A2=(--answer-key <세트 A> --answer-key <세트 B> --evidence <r1-fixed-set> --evidence <r2-setb-fixed-set> --key-split <r2-key-split>)`):
(a) `score "${A2[@]}" --paths a`를 유효 run 3개까지 되풀이(3회 시도, 3개 유효), (b) `score "${A2[@]}" --paths b` 그리고 `--paths b --page-suffix -g2`, (c) `score "${A2[@]}" --paths c`. 순서는 (a) -> (b) -> (c).

경로·run별 출력(#326 judge-arms manifest 입력, 디렉터리는 `~/.local/share/hindsight-bench/arch-v2/` 아래):

| 경로·run | 출력 디렉터리 | summary.json sha256 | detail.json sha256 |
|---|---|---|---|
| a #1 | `score-20261031-070315` | `1473e57bd54a25edca74d1080fcb7f55b1b1e1d3e2e118bda2138402399bcfc4` | `99f83dbbce66b19e374c4a80224fdfcd9846de4945c587f2bc733a8d67329bf3` |
| a #2 | `score-20261031-070614` | `f9ed88ae49f2f54a1885044c3756bc17e954fc126790f5517a1be056f82a0ba6` | `a2a4984ebb5d8398dd38082359ce2577f8a4313d0189269407792b4ffe65de4c` |
| a #3 | `score-20261031-070945` | `767d639f4a8392be00c10cb2337eab1a99abc3e9c08770a847e9e22b2044eb18` | `d39a4c17f86047bef6f22e28dc0102a1b9fbb11145d32b7a5fa07c4d84c24eb8` |
| b full | `score-20261031-071303` | `8b29fee0e6d8f1a7b3efc0292187325ba996edbf36712a6d81cf1be036686110` | `ae3ff95b94a1a2eaf9dc83e1b6c99b42cb4ae85534b64c30530fc96e9169ad11` |
| b full -g2 | `score-20261031-071551` | `5b8dad6733d6d740bb169c6ad459cf37675867de6383b892aa7aecb7e4562bcd` | `ed753665ed3d2ff126f2d7bc3b9670c66dfc71f3302dda4b2b688d7a445f2181` |
| c | `score-20261031-071843` | `bd32d4e2010722b8f7e56dea35420fabac1f2955fa41b80892eab66ceab08a0b` | `b1d8209970012f69fe3cadc90f0226390788a09b9d57a5868f296e0bf1c0012d` |

(a) run 별 (끝난 항목 open, 대조군 open, 판정 가능): 시도 3회, 유효 run 3개, 대조군 0 run 0개.

| (a) 시도 | 끝난 항목 open(합친 30) | 세트 A (15) | 세트 B (15) | 대조군 open(합친 6) | invalid | 판정 가능 |
|---|---|---|---|---|---|---|
| 1 | 0 | 0 | 0 | 1 | 0 | valid |
| 2 | 2 | 2 | 0 | 1 | 0 | valid |
| 3 | 3 | 3 | 0 | 3 | 0 | valid |

경로·run 별 세트·키 분리(끝난 항목 open, 분모는 합친 세트):

| 경로·run | 끝난 open 합친 | 세트 A | 세트 B | 키 있음 (15) | 키 없음 (14) | 미정 K02 | 대조군 open 합친 (6) | 세트 A 대조군 (3) | 세트 B 대조군 (6) | invalid |
|---|---|---|---|---|---|---|---|---|---|---|
| a #1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 0 |
| a #2 | 2 | 2 | 0 | 0 | 1 | 1 | 1 | 1 | 1 | 0 |
| a #3 | 3 | 3 | 0 | 2 | 1 | 0 | 3 | 3 | 3 | 0 |
| b full | 3 | 2 | 1 | 2 | 1 | 0 | 3 | 3 | 3 | 0 |
| b full -g2 | 2 | 1 | 1 | 2 | 0 | 0 | 3 | 3 | 3 | 0 |
| c | 5 | 4 | 1 | 2 | 3 | 0 | 2 | 2 | 2 | 0 |

- 새 대조군 BC01~BC03 은 어느 경로·run 에서도 open 이 아니었다(대조군 open 은 전부 공유 대조군 C01~C03). 세트 B 대조군 열림 = 세트 A 대조군 열림.

판정 값(스펙 #318 산식, `judge-arms` 기준선 출력 `judgement.json`, 합친 세트 끝난 항목 open):

| 경로 | 값 | 기준선 반복 차 | 대조군 open 값 | 세트 A | 세트 B | 키 있음 | 키 없음 | 미정 | invalid | 판정 불가 |
|---|---|---|---|---|---|---|---|---|---|---|
| (a) 유효 run 3개 평균 | 1.67 (0, 2, 3) | 3 | 1.67 | 1.67 | 0.00 | 0.67 | 0.67 | 0.33 | 0 | 아님 |
| (b) 두 생성 평균 | 2.50 (3, 2) | 1 | 3.00 | 1.50 | 1.00 | 2.00 | 0.50 | 0.00 | 0 | 아님 |
| (c) 1회 | 5 | 0 | 2.00 | 4.00 | 1.00 | 2.00 | 3.00 | 0.00 | 0 | 아님 |

항목 id x 경로 판정:

| id | 세트 | a #1 | a #2 | a #3 | b full | b full -g2 | c |
|---|---|---|---|---|---|---|---|
| K01 | A | not-open | not-open | open | open | open | open |
| K02 | A | not-open | open | not-open | not-open | not-open | not-open |
| K03 | A | not-open | not-open | not-open | not-open | not-open | not-open |
| K04 | A | not-open | open | open | open | not-open | not-open |
| K05 | A | not-open | not-open | not-open | not-open | not-open | open |
| K06 | A | not-open | not-open | not-open | not-open | not-open | not-open |
| K07 | A | not-open | not-open | not-open | not-open | not-open | not-open |
| K08 | A | not-open | not-open | not-open | not-open | not-open | open |
| K09 | A | not-open | not-open | open | not-open | not-open | not-open |
| K10 | A | not-open | not-open | not-open | not-open | not-open | not-open |
| K11 | A | not-open | not-open | not-open | not-open | not-open | not-open |
| K12 | A | not-open | not-open | not-open | not-open | not-open | not-open |
| K14 | A | not-open | not-open | not-open | not-open | not-open | not-open |
| K15 | A | not-open | not-open | not-open | not-open | not-open | not-open |
| K16 | A | not-open | not-open | not-open | not-open | not-open | open |
| B02 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| B03 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| B04 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| B05 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| B06 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| B07 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| B11 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| B12 | B | not-open | not-open | not-open | open | open | not-open |
| B13 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| B14 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| B15 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| B16 | B | not-open | not-open | not-open | not-open | not-open | open |
| B17 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| B18 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| B19 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| C01 | A·B | open | open | open | open | open | not-open |
| C02 | A·B | not-open | not-open | open | open | open | open |
| C03 | A·B | not-open | not-open | open | open | open | open |
| BC01 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| BC02 | B | not-open | not-open | not-open | not-open | not-open | not-open |
| BC03 | B | not-open | not-open | not-open | not-open | not-open | not-open |

런 1 세트 A 값(참고, 판정 입력 아님, `artifacts/arch-v2/r1-a-baseline.md` 94-96행, 같은 D10 v1.1.0): (a) 1/15·대조군 1/3(1회), (b) full 3/15·대조군 3/3, (c) 2/15·대조군 2/3, invalid 0.
이번 세트 A 값: (a) 0·2·3/15(대조군 1·1·3/3), (b) 2·1/15(대조군 3/3), (c) 4/15(대조군 2/3).
(c)는 결정적 경로인데 세트 A 값이 2 -> 4 로 다르다. recall 리랭커 인스턴스 차이 가능성(런 1 6절 벤치 1/6 차이와 같은 추정)이 있고 이 티켓에서 원인은 재지 않았다. #326이 (c) W=0 해석에 이 차이를 적는다.

경로별 비용(`llm-requests`, 경로 구간의 행을 provider 로 나눔: 생성형 = local·tei 밖, 임베딩 = local, 리랭크 = tei):

| 경로·run | 질의 수 | 소요(초) | llm-requests | 생성형 | 임베딩 | 리랭크 | 생성형 operation | 생성형 토큰(입력/출력) |
|---|---|---|---|---|---|---|---|---|
| a #1 | 1 | 179.3 | 28 | 11 | 9 | 8 | reflect/reflect 2, reflect/reflect_tool_call 9 | 548883/10456 |
| a #2 | 1 | 210.3 | 28 | 11 | 9 | 8 | reflect/reflect 2, reflect/reflect_tool_call 9 | 596467/12236 |
| a #3 | 1 | 185.2 | 28 | 11 | 9 | 8 | reflect/reflect 2, reflect/reflect_tool_call 9 | 559334/11020 |
| b full | 7 | 168.2 | 110 | 42 | 41 | 27 | refresh_mental_model/reflect 8, refresh_mental_model/reflect_tool_call 34 | 1369560/9132 |
| b full -g2 | 7 | 171.3 | 113 | 45 | 41 | 27 | refresh_mental_model/reflect 9, refresh_mental_model/reflect_tool_call 36 | 1438239/16375 |
| c | 36 | 161.9 | 72 | 0 | 36 | 36 | - | 0/0 |


- 기준선 v1.1.0 생성형 합계: (a) 33건, (b) 87건, (c) 0건. 완전 재통합 상한 횟수·시간에 들지 않는다(스펙 #318 상한).
- 페이지: (b) 7개(project_hint 합집합 6 + 전체 1) x 2회. 사본에 `arch-v2-status-*`와 `arch-v2-status-*-g2`가 남아 있다(사본은 #319가 내린다).

## 4. 검증

- 도구 테스트: 수정 전 31 failed / 119 passed -> 수정 뒤 150 passed(`tests/test_arch_v2_regression.py`).
- 런 1 rescore 불변: 런 1 score detail 40개를 수정 전(74b62c4)·후 도구로 옵션 없이 `rescore`, summary·reasons diff(`created` 제외) 0건.
- 멘탈 모델 GET 응답에 `source_query`가 있고 detail 의 질의와 같다(사본 실측, `--share-pages` 판정 재료).

## 5. 머지본 rescore (final-review 반영 + origin/main 머지 뒤, HEAD 09128e8)

명령: `rescore --answer-key <세트 A> --answer-key <세트 B> --key-split <r2-key-split> --detail <score-*/detail.json>`(질의 0회). 여섯 summary 모두 score summary 와 `set`·`paths`·`breakdown`·`a_run`이 같고, judge-arms 기준선 값도 (a) 1.67(범위 3)·(b) 2.50(차 1)·(c) 5, invalid 0 그대로다. #326 manifest 입력은 아래 rescore summary 를 쓴다.

| 경로·run | rescore summary(`~/.local/share/hindsight-bench/arch-v2/` 아래) | sha256 |
|---|---|---|
| a #1 | `r2-baseline-v110-rescore/070315/rescore-20261031-075053/summary.json` | `ba7d4e811dfac6e728bea9fa6a28d68b53f6814ffc31a064ee7d51fe03e08d8f` |
| a #2 | `r2-baseline-v110-rescore/070614/rescore-20261031-075053/summary.json` | `b47b054899f9969724155c29033d1751450ec7acab127a309adaf228e230ab70` |
| a #3 | `r2-baseline-v110-rescore/070945/rescore-20261031-075053/summary.json` | `48953a0a0531e11fea733026e78256af179b0e541e8611d8140503ee9a249fbd` |
| b full | `r2-baseline-v110-rescore/071303/rescore-20261031-075053/summary.json` | `14e4a1b3159b24329dfc705242546df4cc7311259c09fb1e01f96aeebd9e9819` |
| b full -g2 | `r2-baseline-v110-rescore/071551/rescore-20261031-075053/summary.json` | `1a53a209e89205df2f5ab22d74e72604ad3861ee20150af5b629c2ee0e8184e6` |
| c | `r2-baseline-v110-rescore/071843/rescore-20261031-075053/summary.json` | `aec4c98a6e8ad2f185bed1b03f17c4706bf38650f80b2494bed112b61450ce75` |
