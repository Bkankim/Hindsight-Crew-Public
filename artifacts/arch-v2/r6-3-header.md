# R6-3 S3 기본 머리 C7 교체 (issue #391)

상위: #386. 근거 측정: #388(`artifacts/arch-v2/r6-report.md` 6절).
오프라인 상수 교체다(사본·벤치 복제본·d-llm 없음, 지출 0).
G3는 다시 재지 않았다(코디네이터 결정).

## 1. 바꾼 것

- `gateway/item_open_table.py` `HEADER_DEFAULT`: R6-1 머리 첫 문단 끝에 표 밖 키 지시 한 문장(C7)을 더했다.
- 옛 기본 머리는 `HEADER_R6_1` 상수로 남겼다(R6-2 측정 재현용, 기본값 아님).
- `bench/r6_endtoend.py knobs` 기본(비 C7) 경로는 게이트웨이 기본값 대신 `HEADER_R6_1`을 명시해 R6-2 기록값을 그대로 만든다.
- 플래그 `HC_ITEM_OPEN_TABLE` 기본 꺼짐 유지.
꺼짐 응답 바이트 동일 테스트(`tests/test_item_open_table.py` 기존 고정 sha 테스트)는 그대로 통과한다.
- 머리 원문은 리포 코드에만 있고 이 문서에는 sha256만 적는다.

## 2. sha256

| 항목 | 옛(R6-1) | 새(R6-3, C7) |
|---|---|---|
| 머리 텍스트 sha256(끝 줄바꿈 없음) | `82149001b5936b923e378761c001d5f1463330810beafc5b301108505825f3cb` | `9155cd04ccf246252113ebecd2be77c7a04c7d809ae5e9311c5160b35e11db58` |
| 기본 손잡이 정규 sha256(키 정렬·압축 JSON) | `0ca334da8e68e695b1248da30df7616e7818c97ead3041e4d8cb9026c01883b7` | `24044afd737184e2303e35a75e94d4aee7a2702eb32999c1c79d855c0627dfb8` |

- 새 머리 텍스트 sha256 = #388 하네스 `C7_HEADER_SHA256`.
- 새 기본 손잡이 정규 sha256 = #388 C7 측정 감사 줄 `knobs_sha256`(`r6-report.md` 2절 `24044afd...`, 94/94).
같다(머리만 다르고 나머지 손잡이는 R6-1 기본값 그대로).
- 옛 머리 텍스트 sha256은 `bench/r5_tune.py` `C1_HEADER`(#365 기본 머리) sha256과도 같다.
- `bench/r6_endtoend.py knobs`(C7 옵션 없음)는 계속 `0ca334da...`를 만든다(테스트 `test_knobs_default_file_matches_r6_1_canonical_sha`, `test_knobs_default_uses_r6_1_header_not_gateway_default`).
`--c7-header-file` 경로는 새 기본값 `24044afd...`와 같다(`test_knobs_c7_path_equals_new_gateway_default`).

## 3. 머리 토큰 차(cl100k_base, tiktoken 0.14.0, 표 하나당)

| 머리 | 템플릿 | 채운 머리 fmt a | 채운 머리 fmt b |
|---|---|---|---|
| R6-1 | 84 | 90 | 95 |
| C7(새 기본) | 110 | 116 | 121 |
| 차 | +26 | +26 | +26 |

- 채운 머리는 `{t}`=기준일 10자, `{cols}`=`COLS[fmt]`로 채운 값이다.
- 붙는 표 하나당 +26토큰이다.
G3(+0.16%, 문턱 +5%)는 다시 재지 않았다(코디네이터 결정: 머리 한 문장 차는 응답 토큰 비율 1% 미만).

## 4. 근거 수치(`r6-report.md` 6절 인용, 재계산 없음)

| 지표 | C7 r1 | C7 r2 | R6-1 머리(측정 1 켬) |
|---|---|---|---|
| P_bank | 100%(37/37) | 100%(36/36) | 94.6%(35/37) |
| R_bank(S4) | 63.8%(37/58) | 62.1%(36/58) | 60.3%(35/58) |
| P 오답 (iv) 소비자 오류 | 0 | 0 | 2 |

- #388 자격: 두 회 모두 P_bank >= 95%, R_bank(S4) 평균 62.9% >= 60.3% -> `replace-recommended`.
- C7은 원래 증상 경로에서 재지 않았다(`r6-report.md` 6절).

## 5. 한계

- `bench/r5_tune.py` C2(머리 미지정) 재계산은 이제 C7 머리를 받는다. 런 5 기록값은 그대로 둔다(#387의 C4 어휘 한계와 같은 유형).
테스트 `test_combo_knobs_fixed_space`의 C2 행은 게이트웨이 기본 머리를 확인하도록 바꿨다(코디네이터 결정).
- `bench/r6_payload.py`(R6-1 오프라인 토큰 판정): 고침(#391-1). 손잡이 머리를 `HEADER_R6_1`로 명시해(`r6_1_knobs`) 정규 sha256 `0ca334da...`로 R6-1 기록값(`r6-1-payload.md`)을 재현한다.

## 6. 범위 밖

- 운영 적용(운영자 승인 창), 머리 문안 추가 튜닝.
