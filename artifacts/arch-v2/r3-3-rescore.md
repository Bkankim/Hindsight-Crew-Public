# 런 3 R3-3: 런 2 재채점(판정 v2), 사전 등록 예측 대조 (issue #340)

상위: #336. 질의 0회, 운영 뱅크·운영 스택 접근 0회, 판정 규칙 변경 0건.
이 문서에는 id·판정·해시·수치만 쓴다(정답표 항목 이름, 질의·응답 원문, 기억 본문, 개인 경로, 뱅크 실명 없음. 공개 리포 가드 #4).
리포 밖 파일은 `~/.local/share/hindsight-bench/arch-v2/` 아래(이하 `<bench>`), 사전 등록은 리포 밖 `prereg.md`(1부, 0600)다.

## 요약

- **판정기 v2 커밋 sha(동결, R3-1 #338 머지 커밋)**: `d586ea5aee3702623a64c61cdacad235802a8411`. 이 커밋의 코드로 런 2의 모든 저장 원문(manifest 54개 summary의 원본 `detail.json` 54개)을 `rescore --judge v2`로 다시 판정했다(질의 0회).
- **예측 7개 = 판정 7개**: P1 맞음, P2 맞음, P3 맞음(경계: 0.67을 2/3로 읽음, 코디네이터 답 `msg_1e243a058c55`, 엄밀 비교면 틀림), P4 맞음, **P5 틀림**, P6 맞음, P7 맞음(= "H3 확인", 10절 한계와 함께 읽는다). 근거 수치는 6절.
- **H3(A·C가 진짜 오답 기준 (a)를 줄였다): 확인.** T 기준 open이 A·C 네 반복 모두 0, 기준선 main 1.67, 기준선 v1.1.0 1.67이다(P1·P2). 단 이 재채점은 표본 안 일관성 확인이라 효과 증거가 아니고, P1·P2의 차이는 사실상 끝난 항목 하나(K09)가 닫혔느냐에 기대고 있다(7절).
- 동결 확인: `git diff d586ea5 origin/main -- bench/arch_v2_regression.py`가 빈 출력(origin/main = `d586ea5`)이고, `git diff origin/main -- 'artifacts/arch-v2/r2-*'`도 빈 출력(9절).
- 이 재채점은 **표본 안 일관성 확인**이다: 라벨(진단)과 판정 대상 원문이 같은 런 2 데이터다. 예측 적중을 H1·H3의 독립 증거로 쓰지 않는다. P7(H3) 판정은 #342 후보 선정의 입력일 뿐 효과 증거가 아니다.

| 예측 | 내용 | 판정 | 핵심 수치(6절) |
|---|---|---|---|
| P1 | A-r1, A-r2, C-r1, C-r2의 (a) T 기준 open이 모두 0 | **맞음** | 0 / 0 / 0 / 0 (전체 open 0 / 1 / 1 / 1) |
| P2 | 기준선 v1.1.0 (a) >= 1.0, 기준선 main (a) >= 0.67 | **맞음** | 1.67 / 1.67 (전체 open 3 / 2.33) |
| P3 | B 두 반복 (a)가 모두 >= 0.67 | **맞음** (0.67 = 2/3 판정, 엄밀 소수 비교면 B-r2 0.6667로 틀림) | 1 / 0.67 (전체 open 1 / 0.67) |
| P4 | (a) 30 run 가운데 v2 판정 불가(대조군 open 0)가 0개 | **맞음** | v2 0개 (v1 6개) |
| P5 | 모든 arm·반복 (b) full <= 1.5, 처치 arm과 기준선의 차 <= 1 | **틀림** | A 반복 2 = 2 (> 1.5). 차 조건은 기준선 첫 생성 읽기로만 충족(두 생성 평균 읽기는 A-r1·C-r1·C-r2가 1.5) |
| P6 | 모든 run (c) 끝난 항목 open <= 2 | **맞음** | 최댓값 2 (기준선 v1.1.0, 기준선 main, C 반복 2) |
| P7 | P1과 P2가 함께 맞으면 "H3 확인" | **맞음** (H3 확인) | P1 맞음 + P2 맞음 |

## 1. 실행과 입력

- 판정기 코드: 워크트리 HEAD가 `d586ea5`의 후손(`git merge-base --is-ancestor d586ea5 HEAD` 참)이고 `bench/arch_v2_regression.py`는 `d586ea5`와 바이트가 같다(9절).
- 재판정: 런 2 manifest(`<bench>/r2-report/manifest.json`)의 summary 54개 각각의 `source`(= 원본 score `detail.json`)를 입력으로 한다. 명령(54회, 질의 0회, 모두 exit 0):

```bash
python3 -B -m bench.arch_v2_regression rescore --judge v2 \
  --answer-key <운영 체크아웃>/bench/.data/regression-arch-v2.json --answer-key <bench>/r2-setb-answer-key.json \
  --key-split <bench>/r2-key-split.json --detail <bench>/score-<시각>/detail.json --out <bench>/r3-3-rescore/score-<시각>
```

- 새 manifest: 런 2 manifest를 복사해 summary 경로만 v2 summary(`<bench>/r3-3-rescore/score-<시각>/rescore-<시각>/summary.json`)로 바꿨다(`<bench>/r3-3-rescore/manifest-v2.json`). 원본 manifest·summary·detail은 건드리지 않았다.
- `judge-pairs --judge v2 --manifest <bench>/r3-3-rescore/manifest-v2.json --out <bench>/r3-3-rescore/pairs`, exit 0 (4절 글자 그대로). 영분포(`--null`)는 R3-2 측정 전이라 주지 않았다.
- `judge-arms --manifest <bench>/r3-3-rescore/manifest-v2.json --out <bench>/r3-3-rescore/arms`, exit 0 (5절, 참고용).
- (a) 유효 run 선택은 사전 등록 1부 1.1과 judge-pairs와 같다: 앞 6시도 중 invalid 0이고 대조군 open 1건 이상인 run을 앞에서부터 3개. v1 열은 v1 판정의 대조군 open, v2 열은 v2 판정의 대조군 open으로 고른다. 그래서 같은 반복에서 v1과 v2가 쓴 run이 다를 수 있다(표의 "쓴 run" 열).
- (b) full: 기준선은 첫 생성 2개의 평균, 처치는 첫 생성 1개(judge-arms와 같다). (c)(b-delta)는 1회 값. 끝난 항목 30건, 대조군 6건(세트 A의 C01~C03 + 세트 B의 BC01~BC03). v2 invalid는 54개 summary 전부에서 0.
- 정답표·키 표: 세트 A는 운영 체크아웃 `bench/.data/regression-arch-v2.json`, 세트 B는 `<bench>/r2-setb-answer-key.json`, 키 표는 `<bench>/r2-key-split.json`(읽기만).

| 입력 | sha256 |
|---|---|
| 런 2 manifest | `900a5d63a1206167daa5ed4bfb49d749c22d4abea132e1e5200ec441428e7423` |
| 세트 A 정답표 | `99af2387dc7a74cdb299645677c7c027d6ea5b189ff340c201ceea44eaf93d20` |
| 세트 B 정답표 | `6c50a56b42cf67b31b5d9592bb0ea4a63ac78e1fa891edcedcce6591e0566e6b` |
| 키 표 | `3450a44fb682ca1d3137cb20620939aecf673e5774a12fdd60e53b17e89bcc33` |
| 사전 등록 1부 `prereg.md` | `bd60ac85e2f99aec07173acb569461e5fab6da81d98475132ad84e4bbc81c49e` |
| v2 manifest(새로 만든 복사본) | `b4f9eed31d1532c72b1d75685b821458252ec08e76c0f0ffa08e0e9c107d412c` |
| judge-pairs stdout | `c96bac521a5acb239662a9357053bf14b16a9e83fd2b283f9ec501828cabfb65` |
| judge-arms stdout | `1601bbe62c6e9592c44d885a5d7bc75b2dd6648ce81b5493c17dc3abf95001ef` |

`detail.json` 54개와 v2 summary 54개의 sha256은 부록 A.

## 2. arm·반복·경로별 v1 값과 v2 값

- 읽는 법: v1 열은 기존 판정(`judge-arms`와 같은 값), v2 열은 이번 재판정. "v2 T 기준 open" = v2 끝난 open에서 P 우세 항목(K01, K02, B02)의 open을 뺀 값(사전 등록 1부 1.3). 대조군은 6건 중 open·done·unknown 수(대조군은 open이 올바른 답이다).
- v2 "끝난 unknown (absent)": unknown 안에서 응답·결과에 항목이 아예 없는 수가 괄호다. 목록 경로 (a)(b)는 끝난 항목이 언급되지 않는 것이 올바른 답이고 (c)는 unknown이 검색 실패다(사전 등록 1부 1.1).
- 반복 번호는 완료 반복의 순번이다. A는 manifest의 첫 반복이 중단이라 두 완료 반복을 1, 2로 부른다(사전 등록의 A-r1, A-r2).
- 값은 쓴 run의 평균이다(소수 둘째 자리). (a)의 "시도/쓴 run (v1/v2)"는 summary 수 / v1이 쓴 run 수 / v2가 쓴 run 수.
- 빈 칸 사유: A 첫 반복은 런 2에서 중단돼 summary가 없다. 기준선은 delta 갱신 측정이 없어 (b-delta)가 없다. 그 밖의 칸은 모두 값이 있다.

| arm | 반복 | 경로 | 시도/쓴 run (v1/v2) | v1 끝난 open | v1 대조군 open | v2 끝난 open | v2 끝난 done | v2 끝난 unknown (absent) | v2 T 기준 open | v2 대조군 open | v2 대조군 done | v2 대조군 unknown |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 기준선 v1.1.0 | 1 | (a) | 3/3/3 | 1.67 | 1.67 | 3 | 4.67 | 22.33 (21) | 1.67 | 3 | 0.67 | 2.33 |
| 기준선 v1.1.0 | 1 | (b-full) | 2/2/2 | 2.50 | 3 | 0.50 | 9.50 | 20 (17) | 0.50 | 3 | 1 | 2 |
| 기준선 v1.1.0 | 1 | (b-delta) | - | 판정 불가 (기준선은 delta 갱신 측정이 없다) | - | 판정 불가 | - | - | - | - | - | - |
| 기준선 v1.1.0 | 1 | (c) | 1/1/1 | 5 | 2 | 2 | 24 | 4 (1) | 2 | 2 | 1 | 3 |
| 기준선 main | 1 | (a) | 5/3/3 | 2 | 2 | 2.33 | 5 | 22.67 (20) | 1.67 | 3 | 0.33 | 2.67 |
| 기준선 main | 1 | (b-full) | 2/2/2 | 2.50 | 3 | 1.50 | 9.50 | 19 (14.50) | 1 | 3 | 0.50 | 2.50 |
| 기준선 main | 1 | (b-delta) | - | 판정 불가 (기준선은 delta 갱신 측정이 없다) | - | 판정 불가 | - | - | - | - | - | - |
| 기준선 main | 1 | (c) | 1/1/1 | 5 | 2 | 2 | 24 | 4 (1) | 2 | 2 | 1 | 3 |
| A | 1 (중단) | (a) | - | 판정 불가 (반복 중단, summary 없음) | - | 판정 불가 | - | - | - | - | - | - |
| A | 1 (중단) | (b-full) | - | 판정 불가 (반복 중단, summary 없음) | - | 판정 불가 | - | - | - | - | - | - |
| A | 1 (중단) | (b-delta) | - | 판정 불가 (반복 중단, summary 없음) | - | 판정 불가 | - | - | - | - | - | - |
| A | 1 (중단) | (c) | - | 판정 불가 (반복 중단, summary 없음) | - | 판정 불가 | - | - | - | - | - | - |
| A | 1 | (a) | 3/3/3 | 0 | 2.33 | 0 | 7.67 | 22.33 (19.67) | 0 | 3 | 1 | 2 |
| A | 1 | (b-full) | 1/1/1 | 2 | 3 | 0 | 11 | 19 (17) | 0 | 3 | 1 | 2 |
| A | 1 | (b-delta) | 1/1/1 | 2 | 3 | 1 | 13 | 16 (14) | 1 | 3 | 1 | 2 |
| A | 1 | (c) | 1/1/1 | 4 | 2 | 0 | 23 | 7 (1) | 0 | 2 | 1 | 3 |
| A | 2 | (a) | 3/3/3 | 1 | 2 | 1 | 5.67 | 23.33 (19.33) | 0 | 4 | 0 | 2 |
| A | 2 | (b-full) | 1/1/1 | 4 | 3 | 2 | 12 | 16 (16) | 2 | 3 | 1 | 2 |
| A | 2 | (b-delta) | 1/1/1 | 2 | 3 | 0 | 15 | 15 (14) | 0 | 3 | 1 | 2 |
| A | 2 | (c) | 1/1/1 | 4 | 2 | 0 | 25 | 5 (0) | 0 | 2 | 1 | 3 |
| B | 1 | (a) | 3/3/3 | 1 | 1.67 | 1 | 5.67 | 23.33 (21) | 1 | 3 | 1 | 2 |
| B | 1 | (b-full) | 1/1/1 | 2 | 3 | 1 | 12 | 17 (16) | 0 | 3 | 1 | 2 |
| B | 1 | (b-delta) | 1/1/1 | 2 | 3 | 1 | 10 | 19 (17) | 0 | 3 | 1 | 2 |
| B | 1 | (c) | 1/1/1 | 3 | 3 | 1 | 26 | 3 (1) | 1 | 3 | 0 | 3 |
| B | 2 | (a) | 5/3/3 | 0.33 | 3 | 0.67 | 6 | 23.33 (19) | 0.67 | 3 | 1 | 2 |
| B | 2 | (b-full) | 1/1/1 | 1 | 3 | 1 | 8 | 21 (19) | 0 | 3 | 1 | 2 |
| B | 2 | (b-delta) | 1/1/1 | 2 | 3 | 1 | 4 | 25 (16) | 1 | 3 | 1 | 2 |
| B | 2 | (c) | 1/1/1 | 2 | 3 | 1 | 26 | 3 (1) | 1 | 3 | 0 | 3 |
| C | 1 | (a) | 5/3/3 | 1 | 3 | 1 | 7 | 22 (19) | 0 | 3 | 1 | 2 |
| C | 1 | (b-full) | 1/1/1 | 1 | 3 | 0 | 12 | 18 (16) | 0 | 3 | 1 | 2 |
| C | 1 | (b-delta) | 1/1/1 | 2 | 3 | 0 | 17 | 13 (13) | 0 | 3 | 1 | 2 |
| C | 1 | (c) | 1/1/1 | 4 | 2 | 0 | 24 | 6 (1) | 0 | 2 | 1 | 3 |
| C | 2 | (a) | 3/3/3 | 1 | 2.33 | 1 | 7.67 | 21.33 (19.67) | 0 | 3 | 0.67 | 2.33 |
| C | 2 | (b-full) | 1/1/1 | 2 | 3 | 0 | 11 | 19 (18) | 0 | 3 | 1 | 2 |
| C | 2 | (b-delta) | 1/1/1 | 3 | 3 | 1 | 13 | 16 (15) | 0 | 3 | 1 | 2 |
| C | 2 | (c) | 1/1/1 | 4 | 2 | 2 | 23 | 5 (1) | 1 | 2 | 1 | 3 |

## 3. 경로 (a)(b)(c) 끝난 항목 open 값 정리(예측 판정 입력)

`v2 끝난 open`과 `T 기준 open`만 뽑은 표다(2절 표와 같은 값). (b-full) 기준선 칸은 `평균 (첫 생성 x, 둘째 y) / T 기준 평균` 형식이다.

| arm | 반복 | (a) 전체 / T 기준 | (b-full) 전체 / T 기준 | (c) 전체 |
|---|---|---|---|---|
| 기준선 v1.1.0 | 1 | 3 / 1.67 | 0.50 (첫 생성 1, 둘째 0) / 0.50 | 2 |
| 기준선 main | 1 | 2.33 / 1.67 | 1.50 (첫 생성 1, 둘째 2) / 1 | 2 |
| A | 1 | 0 / 0 | 0 / 0 | 0 |
| A | 2 | 1 / 0 | 2 / 2 | 0 |
| B | 1 | 1 / 1 | 1 / 0 | 1 |
| B | 2 | 0.67 / 0.67 | 1 / 0 | 1 |
| C | 1 | 1 / 0 | 0 / 0 | 0 |
| C | 2 | 1 / 0 | 0 / 0 | 2 |

## 4. `judge-pairs --judge v2` 출력 (글자 그대로)

- 영분포를 주지 않아(R3-2 측정 전) 모든 줄의 이득이 `no-null`이고 효과 칸은 `-`다. 아래는 전이 수의 원표일 뿐 "효과"의 판정이 아니다.
- (b-delta)는 기준선에 delta 갱신 측정이 없어 `baseline-missing`(판정 불가)이다.
- (b-full)의 기준선은 반복 안의 두 생성을 `judge-pairs`(`pair_repeat_state`)가 한 번에 다수결(동률은 open)로 합친다. 한 생성에서만 open인 항목도 기준선 open이 되는 합집합이다(기준선 main b-full 끝난 open 3칸). 사전 등록 1.5의 "(b)는 full 첫 생성 1개"와 2절·3절의 기준선 값(두 생성 평균)과 읽기가 다르다. 그래서 아래 b-full 줄의 끝난 open -> done 3(A·C)은 합집합 기준이다. 생성을 따로 읽으면 두 생성에서 엇갈린 항목은 불안정이 되어 이득에서 빠진다. 이 동작은 동결된 판정기 v2의 것이라 바꾸지 않았고, 영분포 비교(R3-4b·#342)는 이 차이를 알고 읽어야 한다.
- 아래 블록은 저장한 stdout(103줄)에서 마지막 `입력 summary sha256:` 목록(값은 부록 A의 v2 summary 열과 같다)과 맨 끝 pairs.json 경로 줄만 뺀 것이다. 표에는 어떤 치환도 하지 않았다.

<!-- judge-pairs-begin -->
```text
## A vs baseline-main (기준선 반복 1, 처치 반복 2)

| 경로 | 반복 | 끝난 open->done | open->unknown | not-open->open | 불안정 | invalid | 대조군 손실 | 대조군 not-open->open | partial open->done/unknown | 이득 | 영분포 | 효과 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| a | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 1) | 영분포 없음 | - |
| a | 2 | 1 | 0 | 1 | 0 | 0 | 0 | 1 | 0/0 | - (no-null, 합 1) | 영분포 없음 | - |
| b-delta | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (baseline-missing, 합 0) | 영분포 없음 | - |
| b-delta | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (baseline-missing, 합 0) | 영분포 없음 | - |
| b-full | 1 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 3) | 영분포 없음 | - |
| b-full | 2 | 3 | 0 | 2 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 3) | 영분포 없음 | - |
| c | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 1) | 영분포 없음 | - |
| c | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 2) | 영분포 없음 | - |

## B vs baseline-v1.1.0 (기준선 반복 1, 처치 반복 2)

| 경로 | 반복 | 끝난 open->done | open->unknown | not-open->open | 불안정 | invalid | 대조군 손실 | 대조군 not-open->open | partial open->done/unknown | 이득 | 영분포 | 효과 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| a | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 2) | 영분포 없음 | - |
| a | 2 | 1 | 2 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 3) | 영분포 없음 | - |
| b-delta | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (baseline-missing, 합 0) | 영분포 없음 | - |
| b-delta | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (baseline-missing, 합 0) | 영분포 없음 | - |
| b-full | 1 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 1) | 영분포 없음 | - |
| b-full | 2 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 1) | 영분포 없음 | - |
| c | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0/0 | - (no-null, 합 1) | 영분포 없음 | - |
| c | 2 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0/0 | - (no-null, 합 1) | 영분포 없음 | - |

## C vs baseline-main (기준선 반복 1, 처치 반복 2)

| 경로 | 반복 | 끝난 open->done | open->unknown | not-open->open | 불안정 | invalid | 대조군 손실 | 대조군 not-open->open | partial open->done/unknown | 이득 | 영분포 | 효과 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| a | 1 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 1) | 영분포 없음 | - |
| a | 2 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 1) | 영분포 없음 | - |
| b-delta | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (baseline-missing, 합 0) | 영분포 없음 | - |
| b-delta | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (baseline-missing, 합 0) | 영분포 없음 | - |
| b-full | 1 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 3) | 영분포 없음 | - |
| b-full | 2 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 3) | 영분포 없음 | - |
| c | 1 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 2) | 영분포 없음 | - |
| c | 2 | 1 | 1 | 2 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 1) | 영분포 없음 | - |

## baseline-main vs baseline-v1.1.0 (기준선 반복 1, 처치 반복 1)

| 경로 | 반복 | 끝난 open->done | open->unknown | not-open->open | 불안정 | invalid | 대조군 손실 | 대조군 not-open->open | partial open->done/unknown | 이득 | 영분포 | 효과 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| a | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 2) | 영분포 없음 | - |
| b-full | 1 | 0 | 1 | 3 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 1) | 영분포 없음 | - |
| c | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0/0 | - (no-null, 합 0) | 영분포 없음 | - |
```
<!-- judge-pairs-end -->

## 5. `judge-arms` (v2 summary 입력, 참고용)

- `judge-arms`는 v1 값(summary의 `paths`)만 읽는다. v2 summary를 입력으로 줘도 v1 판정 부분이 런 2의 `judge-arms` 출력과 같아야 한다. 확인: 이번 stdout의 1~87행이 런 2 보고서가 쓴 stdout(`<bench>/r2-report/judge-arms-stdout.txt`)의 1~87행과 바이트 단위로 같다(`diff` 출력 없음). 입력 목록과 `judgement.json` 경로 줄만 다르다.
- 판정 머리줄: A 효과 없음, B 효과 없음, C 효과 없음, 읽기 경로만(기준선 main 대 기준선 v1.1.0) 효과 없음(`[no-effect]` 모두). 표 전문은 위 파일과 `artifacts/arch-v2/r2-report.md` 2절에 있고 v1이므로 여기서 다시 싣지 않는다. 이 값이 런 2 판정(v1, W 비교)이고 이번 재채점이 바꾸지 않는다.

## 6. 사전 등록 1부 1.3 예측별 판정

판정 기준은 사전 등록 1부 1.3 문장 그대로다. P1~P3·P7은 "T 기준 open", 전체 값을 나란히 적는다.

### P1: A-r1, A-r2, C-r1, C-r2의 (a) T 기준 open이 모두 0 - **맞음**

| 반복 | T 기준 open | 전체 open | 쓴 run별 open 항목 id |
|---|---|---|---|
| A-r1 | 0 | 0 | (없음) / (없음) / (없음) |
| A-r2 | 0 | 1 | B02 / K01 / K01 |
| C-r1 | 0 | 1 | K01 / K01 / K01 |
| C-r2 | 0 | 1 | K01 / K01 / K01 |

전체 open의 남은 항목은 모두 P 우세 항목(K01, B02)이라 T 기준에서 빠진다. 네 값이 모두 0이므로 맞음.

### P2: 기준선 v1.1.0 (a) >= 1.0, 기준선 main (a) >= 0.67 - **맞음**

| 기준선 | T 기준 open | 전체 open | 쓴 run별 open 항목 id |
|---|---|---|---|
| v1.1.0 | 1.67 (>= 1.0) | 3 | K01 K09 / K01 K02 K04 K09 / K01 K04 K09 |
| main | 1.67 (>= 0.67) | 2.33 | B06 K02 K04 K09 / K08 K09 / K01 |

### P3: B 두 반복의 (a)가 모두 >= 0.67 - **맞음**

| 반복 | T 기준 open | 전체 open | 쓴 run별 open 항목 id |
|---|---|---|---|
| B-r1 | 1 (>= 0.67) | 1 | K09 / K09 / K09 |
| B-r2 | 0.67 (>= 0.67) | 0.67 | (없음) / B06 K09 / (없음) |

B-r2는 문턱 경계 값이다: (a) 값은 유효 run 3개의 평균이라 1/3 단위(0, 0.33, 0.67, 1)로만 나오고, 쓴 run 3개의 T 기준 open 합이 2(평균 2/3 = 0.6667)다.
**0.67 = 2/3로 판정**했다(코디네이터 답 `msg_1e243a058c55`: 사전 등록 1.3의 0.67은 같은 문단 P2와 진단 0b 표 값 0.67·1.00 표기에서 2/3 문턱으로 쓰였고, 결과를 보고 정한 새 해석이 아니라 표기 단위 해석이다. P2의 기준선 main 문턱 0.67에도 같은 읽기를 적용했다. P2 main 값은 1.67이라 어느 읽기로도 맞음이다).
엄밀한 소수 비교로 읽으면 B-r2 0.6667 < 0.67이라 P3은 틀림이 된다. 이 경우에도 P7(P1·P2)과 H3 판정은 그대로다(P3은 P7의 입력이 아니다).
검증 세트(세트 B 항목)만 보면 0과 0.33이라 이 문턱에 못 미친다(7절).

### P4: (a) 30 run 가운데 v2 판정 불가(대조군 open 0)가 0개 - **맞음**

(a) summary 30개 가운데 v2 대조군 open 0인 run은 0개, v2 invalid도 0이다. v1은 6개(기준선 main 2, B 반복 2 2, C 반복 1 2)로 런 2 보고와 같다.

### P5: 모든 arm·반복의 (b) full 값이 <= 1.5, 처치 arm과 그 기준선의 차 <= 1 - **틀림**

| 반복 | (b) full 값(전체 open) | T 기준 | 비교 기준선 | 기준선 첫 생성 / 두 생성 평균 |
|---|---|---|---|---|
| 기준선 v1.1.0 | 첫 생성 1, 둘째 0 | 첫 생성 1, 둘째 0 | - | - |
| 기준선 main | 첫 생성 1, 둘째 2 | 첫 생성 1, 둘째 1 | - | - |
| A-r1 | 0 | 0 | main | 1 / 1.5 |
| A-r2 | **2** | 2 | main | 1 / 1.5 |
| B-r1 | 1 | 0 | v1.1.0 | 1 / 0.5 |
| B-r2 | 1 | 0 | v1.1.0 | 1 / 0.5 |
| C-r1 | 0 | 0 | main | 1 / 1.5 |
| C-r2 | 0 | 0 | main | 1 / 1.5 |

- 틀린 조건은 상한 1.5다: A-r2의 (b) full open이 2다(P 우세 항목이 아니라 T 기준에서도 2). 기준선 main의 둘째 생성도 2(두 생성 평균 1.5)다.
- 차 조건(처치와 기준선의 차의 절댓값 <= 1)은 기준선 값을 어떻게 읽느냐에 따라 값이 달라서 두 읽기를 나란히 적는다(둘 중 하나를 정본으로 선언하지 않는다. 사전 등록 1.1은 기준선 생성의 읽기를 정하지 않는다).

| 읽기 | 기준선 값 | A-r1 | A-r2 | B-r1 | B-r2 | C-r1 | C-r2 | 충족 여부 |
|---|---|---|---|---|---|---|---|---|
| 첫 생성 읽기 | main 1, v1.1.0 1 | 1 | 1 | 0 | 0 | 1 | 1 | 모든 반복 충족 |
| 두 생성 평균 읽기 | main 1.5, v1.1.0 0.5 | 1.5 | 0.5 | 0.5 | 0.5 | 1.5 | 1.5 | A-r1·C-r1·C-r2 불충족(1.5 > 1) |

- 어느 읽기로도 상한 조건(A-r2 (b) full 2 > 1.5)이 깨져 P5의 판정은 같다(틀림).

### P6: 모든 run의 (c) 끝난 항목 open 값이 <= 2 - **맞음**

(c) run 8개의 v2 끝난 open: 기준선 v1.1.0 2, 기준선 main 2, A-r1 0, A-r2 0, B-r1 1, B-r2 1, C-r1 0, C-r2 2. 최댓값 2 <= 2. v1은 2~5(런 2 보고와 같다, 사전 등록 1.3의 "v1은 2~5").

- **(c) 오탐 한계(코디네이터 결정, 판정기 v2 알려진 한계 절 참고)**: R3-1 검증에서 (c) 오탐이 검증 세트 4건(β 2, α 1, δ 1)과 맞춤 세트 K16 1건 남았다. 이 5건은 라벨 파일의 (c) 음성 행 중 v2가 open인 칸 5개와 정확히 일치한다: 기준선 v1.1.0의 두 칸(B16, K16), 기준선 main의 두 칸(B16, K16), C 반복 2의 한 칸(B02). 즉 open 2인 기준선 두 값은 두 칸 모두 알려진 오탐이고, C-r2의 2는 오탐 B02 한 칸과 라벨에 없는 K08 한 칸이다. B-r1·B-r2의 1은 둘 다 K16(라벨 행 없음)이다.
- 따라서 P6의 맞음은 오탐을 그대로 센 값으로 한 판정이다(오탐을 빼면 값이 더 작아져 판정은 같다). (c)에서 v2 open이 가리키는 것이 진짜 열림이라고 읽으면 안 된다: 사전 등록 1부 1.2의 진단 0(기준 시각 열림 진술 0)이 이 (c) 칸들에도 적용된다.

### P7(H3): P1과 P2가 함께 맞으면 "H3 확인" - **맞음 (H3 확인)**

P1 맞음, P2 맞음. 사전 등록 규칙대로 "H3 확인"이다. 이 판정이 하는 일은 A·C 조합을 #342 후보에 넣을지 정하는 입력이다(사전 등록 1.3, #336 "A·C 재사용"). 효과 증거가 아니다(7절).

## 7. 표본 안 일관성과 검증 세트 부분

- 이 재채점은 표본 안 일관성 확인이다. 라벨(진단 diag-ab·diag-c의 T·누락 T·P·M)과 판정 대상 원문이 같은 런 2 데이터이고, 판정기 v2는 그 라벨로 맞춘 규칙이다(R3-1 맞춤 세트). 예측 적중을 H1(측정기 오차가 처치 신호를 가렸다)·H3(A·C가 진짜 오답 기준 (a)를 줄였다)의 독립 증거로 쓰지 않는다. H3의 증거 가치는 #338 검증 세트에 든 부분과 #342 새 측정으로만 센다(사전 등록 1.4).
- R3-1 README 정의: **맞춤 세트** = 기준선 v1.1.0 x 세트 A 전용 항목, **검증 세트** = 세트 B 항목(공유 대조군 포함) 또는 기준선 main run(그 run의 세트 A 항목까지 전부), **그 밖(참고)** = arm 반복의 세트 A 전용 항목. (a) 예측 값을 `K*`(세트 A 전용 끝난 항목)와 `B*`(세트 B 끝난 항목)로 나누고 각 칸에 분할 이름을 붙인 표(T 기준 open, 쓴 run 평균):

| 반복 | T 기준 open 전체 | K* (분할) | B* (분할) | 전체 open의 K* / B* |
|---|---|---|---|---|
| 기준선 v1.1.0 | 1.67 | 1.67 (맞춤) | 0 (검증) | 3 / 0 |
| 기준선 main | 1.67 | 1.33 (검증) | 0.33 (검증) | 2 / 0.33 |
| A-r1 | 0 | 0 (그 밖) | 0 (검증) | 0 / 0 |
| A-r2 | 0 | 0 (그 밖) | 0 (검증) | 0.67 / 0.33 |
| B-r1 | 1 | 1 (그 밖) | 0 (검증) | 1 / 0 |
| B-r2 | 0.67 | 0.33 (그 밖) | 0.33 (검증) | 0.33 / 0.33 |
| C-r1 | 0 | 0 (그 밖) | 0 (검증) | 1 / 0 |
| C-r2 | 0 | 0 (그 밖) | 0 (검증) | 1 / 0 |

- P1: A·C 네 반복의 T 기준 open은 K*(그 밖, 참고) 0, 세트 B 항목(B*, 검증) 0이라 검증 부분도 같은 방향(0)이다. 단 세트 B 항목 쪽 기준선 신호가 작다(기준선 main B* 0.33, 기준선 v1.1.0 B* 0). 기준선 main run 전체(1.67)가 검증 세트다.
- P2: 기준선 main은 run 전체(K* 1.33 + B* 0.33)가 검증 세트이고 1.67이다. 기준선 v1.1.0의 값은 맞춤 분할(K*)에서 나왔고 세트 B 항목(B*, 검증)은 0이다.
- P3: B 두 반복의 T 기준 open은 세트 B 항목(B*, 검증)만 보면 0과 0.33이라 문턱 0.67에 못 미친다. 문턱은 K09 등 세트 A 항목(K*, 그 밖 분할, 참고)이 채웠다.
- 표본이 작다: (a)의 진짜 오답 항목이 세트 안에 몇 개뿐이라 신호가 약하다(#336 요약의 "항목 3개라 약한 신호"와 같은 취지).
- judge-pairs 4절의 (a) 전이를 항목 id로 풀면(같은 규칙으로 판정기 함수 `pair_repeat_state`·`pair_baseline`을 불러 항목별로 낸 값, 질의 0회): 기준선 main 대비 A·C의 끝난 항목 open -> not-open은 모든 반복에서 K09 한 건이다. not-open -> open은 K01(A 반복 2, C 두 반복)이고 K01은 P 우세 항목이다(A 반복 2에는 대조군 BC02의 not-open -> open이 더 있다).
  기준선 main (a)에서 다수결로 open인 끝난 항목은 K09 하나이고, 나머지 기준선 open(B06, K02, K04, K08, K01)은 run마다 달랐다. 그래서 P1·P2의 차이는 사실상 항목 하나(K09)가 닫혔느냐에 기대고, 항목 3개라 약한 신호라는 #336의 말이 그대로 해당한다.
- 영분포 없이는 이득·효과를 정하지 않는다(4절).

## 8. H3 확인 여부

**확인.** 근거: (a) T 기준 open이 A·C 네 반복 모두 0(P1 맞음), 기준선 main 1.67, 기준선 v1.1.0 1.67(P2 맞음). 사전 등록 1.3 P7 규칙(P1·P2가 함께 맞으면 "H3 확인")에 따른 판정이다.

- 이것은 표본 안 일관성 확인이다. 독립 증거가 아니다. 판정이 #342 후보 선정에 넘기는 것은 "A·C 조합(main 고정 빌드 + 범위 + 재통합 + 읽기 층 처치)을 후보에 넣는다"는 결정 입력 하나다.
- 같이 읽을 것: P5가 틀렸다(A-r2 (b) full 2). (c)에서 A·C의 끝난 항목 open은 0~2로 기준선(2)보다 낮거나 같지만, (c)의 open 값은 알려진 오탐을 포함한다(6절 P6). (a) 외 경로의 개선은 이 재채점이 주장하지 않는다.

## 9. 동결 확인

판정기 v2 동결 커밋은 R3-1 머지 커밋 `d586ea5aee3702623a64c61cdacad235802a8411`다. `bench/README.md` "판정 v2 (런 3)" 절 "동결 대상" 목록의 이름 그대로 - 상수 `V2_VERDICTS`·`V2_REF_LEFT`·`V2_REF_RIGHT`·`V2_QUOTED`·`V2_PAREN`·`V2_ISSUE_NO`·`V2_ALIAS_FILLER`·`V2_QUALIFIER`·`V2_BULLET`·`V2_GROUP`·`PAIR_FIELDS`, 함수 `v2_known_names`·`v2_live_hits`·`v2_strip_parens`·`v2_section_lines`·`v2_line_hit`·`judge_sections_v2`·`v2_item_state`·`judge_recall_v2`·`_v2_judge_a`·`_v2_judge_b`·`_v2_judge_c`·`judge_detail_v2`·`v2_block`·`pair_path_of`·`pair_repeat_state`·`pair_baseline`·`pair_transitions`·`pair_gain`·`judge_pairs`, 그리고 이들이 부르는 v1 함수 `line_subject`·`item_matches`·`item_keywords`·`classify_memory`·`contamination`·`iter_facts`와 단서 정규식 - 의 본문 변경 여부:

```text
$ git fetch origin main
$ git rev-parse origin/main
d586ea5aee3702623a64c61cdacad235802a8411
$ git diff d586ea5 origin/main -- bench/arch_v2_regression.py | wc -l
0
$ git diff d586ea5 HEAD -- bench/arch_v2_regression.py | wc -l
0
$ git diff origin/main -- 'artifacts/arch-v2/r2-*' | wc -l
0
```

origin/main이 `d586ea5` 그대로라 `git diff d586ea5 origin/main -- bench/arch_v2_regression.py`는 전체가 빈 출력이다(동결 대상 본문 변경 0). 런 2 산출물 diff도 빈 출력이다. `register_v2_path`로 경로를 더하는 것(`V2_REGISTRY` 항목 추가)은 README가 정한 확장 지점이라 동결 위반이 아니다.

## 10. 판정기 v2 알려진 한계

코디네이터가 결정해 싣는 한계다. 이 한계 때문에 문턱·예측을 바꾸지 않는다(목표 이동 금지). 한계로만 싣는다. P6·P7 해석에 같이 쓴다.

- **끝난 항목 행 정밀도·재현율**: R3-1 검증 세트 지표는 이분 전체(대조군 open 줄 141행 포함) 0.962 / 0.981이다. 대조군 줄을 빼고 끝난 항목 행만 보면 정밀도 0.667, 재현율 0.800이다(문턱 정의 밖 참고값). 끝난 항목 open 값에는 오탐·누락이 섞일 수 있다. 이 문서의 P1~P3·P5·P7은 모두 끝난 항목 open 값이므로 이 한계 아래에서 읽는다.
- **(c) 오탐이 남았다**: 검증 세트 4건(β 2, α 1, δ 1), 맞춤 세트 K16 1건. P6((c) open <= 2) 판정에는 이 수를 함께 적었다(6절 P6): 5건 모두 이번 (c) run 8개 안의 칸이고, P6 판정은 오탐을 센 값으로도 맞다.
- **"그 밖(참고)" 분할의 재현율**: R3-1 README 표의 "그 밖(참고)"(arm 반복의 세트 A 전용 항목)는 양성 27행 중 TP 19, FN 8로 재현율 0.704다. 이 누락이 이 문서의 값에 닿는 곳이 있다. 라벨상 열려 있어야 하는 칸을 v2가 not-open으로 읽은 예: B-r2 (a)의 K09(라벨 누락 T, 두 run에서 v2 unknown), A-r1·C-r1 (b) full의 K04(라벨 누락 T, v2 unknown). 그래서 B-r2 (a) 0.67(P3 경계)과 A-r1·C-r1 (b) full 0은 라벨 기준으로는 더 높을 수 있다. 이 한계로 판정을 바꾸지 않았다(P3은 맞음 그대로이고 엄밀 소수 비교에서 틀림일 가능성만 줄어든다. P5 판정은 상한 조건 하나로 정해져 영향 없음).
- **열람 run**: R3-1 워커가 규칙 설계 전에 검증 run 1개(기준선 main (a) 첫 run)의 응답 모양을 열람했다. 그 run을 뺀 검증 값은 0.961 / 0.980이다. 이번 P2의 기준선 main (a) 값에는 그 run이 들어 있다(첫 3개 run).
- **R3-1 README 기재 한계(코드 변경 없이 기록됨)**: (c) recall 결과가 빈 목록이면 invalid(no-provenance), 결과가 있고 keyword가 없으면 unknown + absent다. `V2_REF_RIGHT`가 항목 자신의 다음 단계 서술을 참조로 뺄 수 있어(런 2 (a)(b) 46 run에서 1건) 목록 경로에서 거짓 open -> unknown 이득이 가능하다. (a) 유효 run 선택은 invalid run을 건너뛰고 뒤 run을 쓴다.
- **검증 세트 오염**: R3-1 README가 검증 세트를 "오염됨"으로 적었다(설계 전 열람 1건, 검증 뒤 final-review 규칙 수정 3건). 검증 세트 부분 값(7절)도 그 한계 아래에 있다.
- **이 재채점 자체의 한계**: 표본 안 일관성 확인이고(7절), 영분포가 없어 `judge-pairs`의 이득·효과를 정하지 않았으며(4절), P7 판정은 #342 후보 선정의 입력일 뿐 효과 증거가 아니다.

## 11. 참고: R3-1 워커 산출물과의 차이

R3-1 워커가 머지 전 코드(`9c9a241`)로 낸 재채점(리포 밖 `r3-1-rescore/`)은 결과로 재사용하지 않았다. 머지 커밋 코드로 다시 낸 이번 54개 summary를 그 v2 블록과 비교하면 **3개 summary에서 값이 다르다**: C arm의 `b-delta` 1개와 `a` 2개에서 K01 한 항목이 done -> open이다. 원인은 R3-1 final-review 규칙 수정 3건(`bench/README.md` "검증 세트: 오염됨" 항목에 기록된 변화, K01 3행 done -> open)이고, 그 밖의 51개 summary와 모든 v1 부분은 같다. P1~P7 판정에는 영향이 없다(K01은 P 우세 항목이라 T 기준에서 빠지고, 이 세 summary가 속한 C 반복의 (a)는 T 기준 0 그대로다. 전체 open 1은 K01 포함 값).

## 부록 A. 입력·출력 sha256

- 아래 표가 `rescore` 입력(원본 `detail.json`, 읽기만 했다)과 출력(v2 summary) 54쌍이다. `judge-pairs`와 `judge-arms`의 "입력 summary sha256" 목록은 이 표의 v2 summary 열과 같은 값이다(두 도구의 출력 파일에 같은 목록이 있다).
- `detail.json`의 위치는 `<bench>/<score id>/detail.json`, v2 summary는 `<bench>/r3-3-rescore/<score id>/rescore-<시각>/summary.json`이다.

| arm | 반복 | 경로 키 | score id | detail.json sha256 | v2 summary sha256 |
|---|---|---|---|---|---|
| 기준선 v1.1.0 | 1 | a | score-20261031-070315 | `99f83dbbce66b19e374c4a80224fdfcd9846de4945c587f2bc733a8d67329bf3` | `b921d6d782ce303f951d37e5be717b9cdf425c0c60f97a81fb076a515cf39ca1` |
| 기준선 v1.1.0 | 1 | a | score-20261031-070614 | `a2a4984ebb5d8398dd38082359ce2577f8a4313d0189269407792b4ffe65de4c` | `a4b3afdfd752925f1f893526b3d02b83e086ddabddba4cebb63225e934629117` |
| 기준선 v1.1.0 | 1 | a | score-20261031-070945 | `d39a4c17f86047bef6f22e28dc0102a1b9fbb11145d32b7a5fa07c4d84c24eb8` | `35f7dbc1e9dbfd3a8a1bbd365e9e3d30dce93098f0bf370a7ed90b5a4ee06278` |
| 기준선 v1.1.0 | 1 | b-full | score-20261031-071303 | `ae3ff95b94a1a2eaf9dc83e1b6c99b42cb4ae85534b64c30530fc96e9169ad11` | `9d8db06aeefbaba8683a9b5c97619eb4580bf96e302af2ab6bbeca38220ed934` |
| 기준선 v1.1.0 | 1 | b-full | score-20261031-071551 | `ed753665ed3d2ff126f2d7bc3b9670c66dfc71f3302dda4b2b688d7a445f2181` | `8cf63eccca6bf04fd255939eb80a1d8ccdc0ee6e6d6f2071501668d4f1b77e57` |
| 기준선 v1.1.0 | 1 | c | score-20261031-071843 | `b1d8209970012f69fe3cadc90f0226390788a09b9d57a5868f296e0bf1c0012d` | `79b6b75abeac4dd93d56e70b7b8b38d80d792863bc75f7ca60a5cdb4fb51b9da` |
| 기준선 main | 1 | a | score-20261031-081425 | `207ccbd955c5c01e4e719f165ed6c9081f580fd94cb34eb49fffc0d0e9f59c33` | `36f73358307d18423620c34133aa27fabcaf969a92603ffcc1b9a9b9793c44ac` |
| 기준선 main | 1 | a | score-20261031-081622 | `7aaddb14a4422165235cda9ae2e9c36164af7848171ee9875436c6207fbbb457` | `9103c4f843aac23c224eb7c3d790759b180285649f75279a56dab678db8fa896` |
| 기준선 main | 1 | a | score-20261031-081838 | `54787dfcbf4c5fd8de055b8030248365d920a227bc3594ca30ceb23a6e699b4f` | `f9d1a968af200c81a92f8f8850fcde707356b2581bf1361a465c9b1351409149` |
| 기준선 main | 1 | a | score-20261031-082107 | `313a2df88e61afd6e2bcc0371d8faa5e7cc158c68b8305df95b2148d7917e4b6` | `383cd42602834ba862a5b0f881d8ff4916959c1ae50e7d45b7f65c808aa574a5` |
| 기준선 main | 1 | a | score-20261031-082322 | `d42fa53850fa0fb936b62b0fb7fd6765960bd249fa0c1312b1696596157a8ed8` | `d564503a6943d454ab29d6866c3c597b4919c189cd43c81090f71f8da6e6b9ce` |
| 기준선 main | 1 | b-full | score-20261031-082513 | `c1a8b8f3fdd4e21d8c6ef2eb0e23eacd895879a578e0a148e4731edae9acfd5f` | `227aec268cd6c0afeb665047beee1e1e55a57ff35ef43fb92613d491dfa9041f` |
| 기준선 main | 1 | b-full | score-20261031-082818 | `2533ad080e4d4551de79282ac63fcc75d69b37ca595325bb1b9e9c8e3ac06cc1` | `d44e2e0a23305861d1866a40839c67a6a7fd223e476f1d4a388bad3af5d4b694` |
| 기준선 main | 1 | c | score-20261031-083124 | `767310716af0a06725976fdb8a1adcd1c43c06e9f90109ebe47028fa13b78b73` | `6d0ce2310a0932a08f3b6f42ec0587a79f3b75dbcace33c3fccdfd80b603960a` |
| A | 1 | b-delta | score-20261031-135654 | `e825fc2cf895f7299e3c0956ee3853ba0600b970fe20818f7ede61a62806982d` | `0c58871949d36b0f8545f4d1c523b1cbeb6cce3d10f2743898b32f0777ce44d3` |
| A | 1 | a | score-20261031-135725 | `88b34fdf0aa49fb4295d970653b3db46b6c6bffb7c1ce97d2b7269d5976ded4c` | `fffc7e5b0e8eb87189381a6c2787ea951b3878a4cdd77e7a8f51feb01309a7bd` |
| A | 1 | a | score-20261031-135938 | `7014f88ff0490d61bc4b5d8b9f0881aaa17eb51514d1860539f91c71eb5d0c04` | `c9799c09c54aa633e4bb55fa4edafcf472c5dff85f048ad200f309addd7065e3` |
| A | 1 | a | score-20261031-140111 | `fedee91b19cf994ca2ca939268920838a5fe0c99e76e6d6a11accc7e3431139a` | `64f8e00cf2cdba0575f0661f7403d07b1b9e76eeaa98aaea6b919f5085287a31` |
| A | 1 | b-full | score-20261031-140302 | `948bbc6e26b16244846d2944ffd67ee58b3d9782b4319007276c9e8263c7c37a` | `c22d32c4f66b7aa9701e67c52354caa17b87352c72c2d577e6e2b8e2e90957ac` |
| A | 1 | c | score-20261031-140619 | `8fd12bf1bdcbd5d50ee07d7f7b1137452b30507e2f409105c218156ca05c8526` | `872e1e595bc25bc7b0cb0089775c25b32ee94387cfdc672db7992af3e94cffc4` |
| A | 2 | b-delta | score-20261031-170615 | `43bc10cdfafae0aab84374e132dc09309908ac4f2b2bc9f0a52e5aefa2a4203d` | `9316795ce26edd9ccc57de4f6d29c11772b661e405a7337f8b02e3234c555d32` |
| A | 2 | a | score-20261031-170703 | `0b591084f9388472c489030ac3f4f312aa766b36dc8aeba27652b257f6451f2d` | `2488661be52f392a998bbfa0af9e0b7c740b9abaf92291c8f085028ecff0cea8` |
| A | 2 | a | score-20261031-170839 | `efc9587095c3857e72d5d0db441a901ca3fe43ccb100f8e4b0486b6fb2cbffec` | `cfad32660d30589124152e5ac429c96bc791a454c2b3c4ffb0edbeb2a551d30e` |
| A | 2 | a | score-20261031-171112 | `03143277755256755cc307987899e4d86fca6e83eb03217c278c4d5ab668006c` | `cb46c8ed0caabf68dbacfb092eb8b38e9b8f0c3e3bd9608fa1a2dfe56964f9b3` |
| A | 2 | b-full | score-20261031-171251 | `c1cb1e95729a4033d8deee89dad14c61cea54bd84bc25fdf285ea2cffc9275f0` | `54e62c66d0aa9af8501193b428f408b4b0abd00e7abf15c3a1366baea93fb41f` |
| A | 2 | c | score-20261031-171540 | `44088a065ed4d06398ad0669d2db23a991860685c5a33b2153ee989f3c9ae9ac` | `2a72c7e2d65124c9eff68f7cae412e1e189fc860c3e0c6908b0344a54377a677` |
| B | 1 | b-delta | score-20261031-085925 | `7dcdd05e416c6ec9b38ff78ab2c927476a1cedf611f45c67d274712909e423f4` | `9067d5788a99f5e7c24a59989311b8b6b700330fed512597591e58ef3e2f4f99` |
| B | 1 | a | score-20261031-085943 | `b312ccfada527aba810a9ed838614c74564cd4f4cdd6ea83e290e21e8c1ed9d3` | `2fa466c2c83af61c4a9982d291ee6ed4c3e5d2539762d5bcba4792da569658df` |
| B | 1 | a | score-20261031-090159 | `10ac9251f2f555fa81650876d3c79b43f90f733e6ab9bc3eaafade835fbf6705` | `283deba63be3642b6089f260db30a0a4f39de4cf40b575d5f62fc1ae37db6ef2` |
| B | 1 | a | score-20261031-090355 | `b6614a445f64ae78db9dbfa6f4516e80eb522ae8d850ec9eb2e3d2d627d389ac` | `6f8bd1f51f1a50331e92ab16cd77619d0bee070e626934d473b8522a053d067b` |
| B | 1 | b-full | score-20261031-090624 | `73e65733c8ba5ce1fe623b6eeca032aff9f84a40aaa4a6a2a9cc3de2392be487` | `96dd72204adec7924a07f5b5b1f12d541e39401b633a1a01adb7ce5ca7e07f4b` |
| B | 1 | c | score-20261031-090915 | `a97688647c50ed9e1241a45c6d02989a18420e8105c2f629ef7a7d08ae51c4de` | `c6daaaaca02a2b66bedea644db44ba77605af186aa2f5e881df83f46c6dfd26f` |
| B | 2 | b-delta | score-20261031-100543 | `cb549ba48e30a135effdac0e2d1fe1a108db88b1736ae7b2f1890ad1f7741ca0` | `2e50337327ee2cd5b4ddb750baaeca17e84f6ea4dbaca0e941bae74897aa3100` |
| B | 2 | a | score-20261031-100559 | `66d0c2a439b0c40ee1e4af9d6000becb2e47e07bf16818fd874098b0bce0b301` | `506f5a210a5dee43f7b3a750b4d91cd2927d099d32c27d30214c1e229137d2b9` |
| B | 2 | a | score-20261031-100840 | `1a5cea08e27f5453362542b026c8b32c1d7c89ca1cac4a40f132621916b07d00` | `81a7bf2f1b8672bf59177dd44fb0e836fe9f897fc38f00e7e2f93eb1df73fe32` |
| B | 2 | a | score-20261031-101036 | `6ed8e405d4838b4ad7de8ff9faed18f61bb090658ac216869eb70e7b2e8ff560` | `3f01fea553ede1e12fa7d1d271bdb621f6e37196d39492854680ed05f9482919` |
| B | 2 | a | score-20261031-101324 | `4bab567f92ab029522188daf173799daffd76382a2fcea62bd69371084737ee6` | `98324707728c9750c4a4d51909667c309de6bc90fefda997d612596730fae36b` |
| B | 2 | a | score-20261031-101519 | `6c6414e1320e0a3405495f2d86dc09b4cf6044a36593e599b23fe968681e14e0` | `1bdae3a1ee8b627026746b908f23e5766ca46952f55686be1d0cab7f9ec77717` |
| B | 2 | b-full | score-20261031-101715 | `54ccfe8278a03e9895f997e9464d8af97b6969d386ea33de2515238c7bb40d2b` | `5b04e45e1e72a30d547842b46e1ecf4d4cb794e366aac3d7205a043656a6aca3` |
| B | 2 | c | score-20261031-102016 | `645162f446e5c11911fc368f037283fa1f2808eb5319d55d7a7405c3dd44162d` | `9ecaa45fdf8076aa74a0a621fc5f2c46cbe87b41ef1b869936ab54257f547ab2` |
| C | 1 | b-delta | score-20261031-205121 | `e8b2d5239c8e8fc0f8f43179806c6b0f1af63ecd5ac247a32b7588c73bacf502` | `3a44d0f5d0bb8f637866cf16a4a24e38e729cff0bf7f94b1ad473071814e8a56` |
| C | 1 | a | score-20261031-205225 | `00805347b63f141b8b9862e22c029679487d87b9447a7a96b9b6a20738ba1380` | `d3110c85b58c3a1bb5e8e5c3cdc0ec3211fd375e7177d24c69b43b7e25430582` |
| C | 1 | a | score-20261031-205345 | `6f279c6b708ada16396b251b2215977f15a9a8b33de8275b9840553f7d32941a` | `7c1fd67ccab1221cb8f995903e06adf5ac2a3edf570bf15c08931001c706616f` |
| C | 1 | a | score-20261031-205540 | `fe91184d809fc3cab7eeb235cbc762ed2a609521ef38481e30f8497c4463640f` | `55d945d8c31f0e45eeeaec51d3925b9176f01802d28547bf33fc5ca277c5bd4e` |
| C | 1 | a | score-20261031-205745 | `da59996173d769bcc0e55f0df49ea08f7f4058ba5d9eb9ad78cc27f53fea5707` | `43e78418896de32a48bf6d6a78dbd7994b4679525476c9516cd8ef06ca260bf8` |
| C | 1 | a | score-20261031-205912 | `d29d376ff13f5bcacdeff626f7990b35ab963a3c22f83636f0730ff267ff19b0` | `bee0d5d492d58372c04c3152a56221c12a70155e370157a15da2bb84459f4989` |
| C | 1 | b-full | score-20261031-210030 | `4de4d590649cf0a2c852b058cc1aba2d9cf75751a93b8353ef0a3cc7f2a68157` | `610c9a8ae90541d5965504f17209cf86340aab9f6d19576db3f667e72632d1dc` |
| C | 1 | c | score-20261031-210421 | `896cc467546bef2cabca74e90e975f5540266488bdb6da8427bbb90db64b2d19` | `e7abe723cc290cb8510c7b4aaaad980ddf41535900d29393f3ae4d978446d411` |
| C | 2 | b-delta | score-20261101-000441 | `9a79f7d3e74aca6d7ac861080f6d2fa64f4d85e7d8bb6f780b0efec131ed2f51` | `a8a0a8ac9959e60b35d052b613ecdecd3bed9ee939a4dc045cea115bb673d9d3` |
| C | 2 | a | score-20261101-000524 | `2adc9bbb0e74ccd9b2ef6702639efd836f41c9af7d3d70f2a20eab7a98611c97` | `c371423f56d2a29a53f59ecc04b5c267be075699ebaf6750a410dc05a909de88` |
| C | 2 | a | score-20261101-000700 | `bd229422e46dbd93c75b448f938bcbac695ef31eeaa97ad11b315a4a063b6299` | `c132657177e39812dc9599b57f09c7b72b121e7170ee3808579fe281a2c9a46d` |
| C | 2 | a | score-20261101-000821 | `a21f1bedcbb236fd6a2c3d870027a41b47ec7f3d66cc1ebc0019bfa436860a98` | `1eb4f0060b6291cc2f399e1a1bff479aada792ebb839a0d2ca57bf353a5a670f` |
| C | 2 | b-full | score-20261101-000959 | `0a0768b3d466182c1b6cde2736a69355750aecc6c9a6470bacfa258252db301f` | `66ea9e1cc665d44c81f18af61ac9eb07ab47f9574fbefe8c2da39ebf07d61a9e` |
| C | 2 | c | score-20261101-001320 | `a176d468d85a1141d2b3b7a8ff6dfe8a23f12954267fdd6f9491ed62ff318a78` | `128c3c09bf6ad054294e2a0efb6910f650023eba22224e59257403fbf9b83a26` |
