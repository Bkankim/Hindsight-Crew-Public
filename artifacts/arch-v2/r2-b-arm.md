# 런 2 B arm: v1.1.0 D10 작업 키 무효화 backfill, 부분 재통합 2회, memory_defense 사본 확인 (issue #322)

상위: #318. 도구는 `scripts/item-backfill.py`(이 티켓), 규칙은 #320 `gateway/item_keys.py`, 채점 하네스는 #323 머지본(`bench/arch_v2_regression.py`).
이 문서에는 id·판정·해시·수만 쓴다(항목 이름·질의·응답 원문·기억 본문·뱅크 실명 없음, 공개 전환 가드 #4). 뱅크는 `<bank>`, 운영 체크아웃은 `<운영 체크아웃>`이다.
arm 판정은 #326이 `judge-arms`로 낸다. 이 문서는 값만 적는다.

## 1. 공통 입력

| 항목 | 값 |
|---|---|
| 워크트리 HEAD(채점 시) | `c883f83`(#323 머지 2e24245 를 merge 한 커밋) |
| 사본 이미지 | v1.1.0 `ghcr.io/vectorize-io/hindsight@sha256:50311d02...2aa06`(`HC_NEXT_IMAGE` 없음), `--llm opus` |
| `RULE_VERSION` | `item-keys-v1`(두 반복 같음), 규칙 모듈 커밋 `ee9aadd` |
| 별칭 표 | `~/.local/share/hindsight-bench/arch-v2/r2-aliases.json`(#321 작성, 99항목) sha256 `21d1b75bae4c66f23f28061d6f1e9fae35ea4bfa931e57b63875aa6de606cdf6`(코디네이터 사본 차례 메시지 값과 같음, 두 반복 같은 파일) |
| 세트 A 고정 세트 | `r1-fixed-set.json` sha256 `3fbe114b588af11e154b343639b8d5dcd9efe4b3c49343fb91559afbdd9d904d` |
| 세트 B 정답표·고정 세트 | `6c50a56b...6e6b`, `6798f3eb...51a3`(`r2-t4-setb-baseline.md` 정본과 같음) |
| 키 유무 표(채점 때) | `r2-key-split.json` sha256 `3575b1c02e5baaca34c2e54f9dbb57ba87b8fe460ed636e0f1eef9d3d604138a`(K02 null). 채점 뒤 K02 확정으로 바뀜(9절): 전 `3575b1c0...` -> 후 `3450a44f...`. 4절 채점 summary 는 전 표로 냈고 #326 이 후 표로 rescore 한다 |
| 질의 시각 | `query_timestamp=2026-10-28T14:59:59Z` |

정답표·고정 세트는 backfill 입력이 아니다: `grep -nE 'regression-arch-v2|fixed-set|answer-key' scripts/item-backfill.py` 빈 출력(exit 1).

## 2. 반복별 처치

| 항목 | r1 | r2 |
|---|---|---|
| `restore:` 줄 | `restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4` | 같은 줄(sha256 같음) |
| 사본 up(KST) | 2026-10-31 08:50:08 | 2026-10-31 09:59:21 |
| 실패·취소 기준선(up 직후) | failed 40, cancelled 0 | failed 40, cancelled 0 |
| dry-run 집계 | 기억 2,635(world 1,916 + experience 719), 완료 844, 키 있는 완료 254, 대상 쌍 65, 대상 12, `over_cap` 완료 0 | 같음(대상 목록 파일 바이트 동일, sha256 `6bda62b8...226b`) |
| apply 목록 = 검토한 dry-run 목록 | 같음(`cmp targets-r1.jsonl targets-r1-apply.jsonl` 동일) | 같음(`cmp targets-r2.jsonl targets-r2-apply.jsonl` 동일, 두 반복 apply 파일 sha256 `6bda62b8...`) |
| `--apply` 시작(= 부분 재통합 시작) | 2026-10-30T23:55:21Z | 2026-10-31T01:02:59Z |
| invalidated 수, apply 직후 / drain 뒤 | 12/12, 12/12 | 12/12, 12/12 |
| drain 완료 | 2026-10-30T23:58:40Z(91.3초, exit 0) | 2026-10-31T01:05:03Z(61.2초, exit 0) |
| `llm-requests`(처치 시작~drain 완료) | 39: 생성형 18(통합 2 = consolidation 1 + consolidation_dedup 1, 페이지 갱신 16 = refresh_mental_model 13 + mental_model_delta_ops 3), 임베딩 13, 리랭크 8. 생성형 토큰 166,265/10,314 | 43: 생성형 20(통합 2, 페이지 갱신 18 = 15 + 3), 임베딩 14, 리랭크 9. 생성형 토큰 172,048/6,889 |
| 관찰 수 전 -> 후 | 4,924 -> 4,891 | 4,924 -> 4,891 |
| drain 뒤 실패·취소 | failed 40, cancelled 0(기준선과 같음) | failed 40, cancelled 0(기준선과 같음) |
| 1회 상한(4시간·생성형 통합 2,000건) | 닿지 않음(3분 19초, 통합 2건) | 닿지 않음(2분 4초, 통합 2건) |
| 중단 | 없음 | 없음 |
| drain 시계열(리포 밖) | `r2-b/drain-r1.jsonl` sha256 `8f8b3c00...c581` | `r2-b/drain-r2.jsonl` sha256 `ee4d3d2f...9c83` |

명령(워크트리, `next_env` 뒤):
```bash
python3 -B scripts/item-backfill.py --bank "$BANK" --aliases "$ALIASES" --out <리포 밖>/targets-r<r>.jsonl --dry-run
python3 -B scripts/item-backfill.py --bank "$BANK" --aliases "$ALIASES" --out <리포 밖>/targets-r<r>-apply.jsonl --apply --batch-size 20
# 런 뒤 final-review 반영으로 더한 인자(이 런은 이 인자 없이 돌렸다. 위 표의 cmp 로 같은 효과를 확인):
#   --targets <리포 밖>/targets-r<r>.jsonl          검토한 dry-run 목록과 다시 계산한 (완료, 대상) 쌍이 다르면 PATCH 전 exit 1
#   --expect-aliases-sha256 <별칭 표 sha256>        별칭 파일 sha256 이 다르면 exit 1
python3 -B scripts/item-backfill.py --bank "$BANK" --check <리포 밖>/targets-r<r>.jsonl          # apply 직후, drain 뒤
python3 -B scripts/scope-migrate.py drain --bank "$BANK" --since <apply 시작> --max-llm 2000 --timeout 14400 --timeseries <리포 밖>
python3 -B -m bench.arch_v2_regression stats --wait 12000 --wait-pages
```

- drain 첫 시도 fail-closed: r1 1회, r2 2회 drain 이 "the five operation states add up to 6277 but the operations total is 628x"로 exit 1 이었다. 무효화 직후 통합·페이지 갱신 잡이 쌓이는 동안 `stats`의 `operations_by_status` 합이 operations 목록 `total`보다 작았다(재확인 3회 x 2초 안에 맞지 않음). 몇 초 뒤 같은 `--since`로 다시 돌린 drain 이 exit 0 으로 끝났고, 위 표 값은 그 drain 이다. 상한 시계는 `--since`(apply 시작)부터라 재시도로 바뀌지 않는다.

## 3. 오탐 표본(1회차만)

대상 12건(40건 이하)이라 전수 검사, seed 없음. 판정 기준: 대상 기억이 말하는 열린 일이 D10 스냅숏 전에 끝났는가(완료 기억·이슈 상태로 확인). 오탐 1/12 = 8.3%(문턱 20% 이하) -> apply 진행.

| memory id | 판정 |
|---|---|
| 44b86c05-1088-4b8d-a136-634a75a894bf | 맞음 |
| 92c97cd2-3753-4cf1-8fa4-71f33b23d288 | 맞음 |
| 182cb955-6c3f-4532-ba52-9dd0189cc7db | 맞음 |
| d3f685c2-52a9-40cf-80a5-47f855a2c73d | 맞음(매칭 키는 같은 인계에 적힌 다른 번호, 인계한 일 자체도 끝남) |
| a97b3ec0-c659-4de3-b6d2-c9e33e1b9fb3 | 맞음 |
| 7adf0bb3-6a7a-41c4-b628-d1837b5bb841 | 오탐(인계에 적힌 이슈 셋 중 하나가 아직 열림, 인계가 일부 유효) |
| 47e0a3d3-3c5e-4ffe-afe3-f224c506c5fc | 맞음 |
| 5384eff8-b68d-4eb7-bfcf-0b1d43cc0e86 | 맞음 |
| 9b70dd19-37a7-4a19-a28c-038fd6b62aaf | 맞음 |
| 630281b4-7664-442c-8395-180fa2dbfeff | 맞음 |
| dc5b924a-fdc4-4b3c-b49e-dbc22407df04 | 맞음 |
| f2667020-f576-42c7-a358-781135e0b641 | 맞음 |

## 4. 채점 (반복마다, #323 머지 하네스, 합친 세트 A∪B)

순서: `-pre` 생성(처치 전) -> 처치 -> b-delta(`--paths b --read-pages --page-suffix -pre`) -> (a) 유효 run 3개까지(시도 상한 6) -> b-full(`--paths b`, 접미사 없음) -> c(`--paths c`).
두 정답표의 `query_date`·`ignore_mentioned_at_from`·`query_instruction`이 같아(#323 확인) 페이지와 (a) 응답을 공유한다.

### 4.1 r1

경로·run별 출력(디렉터리는 `~/.local/share/hindsight-bench/arch-v2/` 아래, #326 manifest 입력):

| 경로·run | 출력 디렉터리 | summary.json sha256 | detail.json sha256 |
|---|---|---|---|
| b-pre | `score-20261031-085055` | `16d5fc6051634812a92996d6d6efd13caf7d6e8f1fe70ac0e4b0563406f2528e` | `112fcfecd3405f1df99885da28ce903ffd2b46b1467100848812d7c3844ce48b` |
| b-delta | `score-20261031-085925` | `c416ebc88fb37df4e8f4ae6f2f84dc4c6739f38fa51b4ccb7edcc85597325af4` | `7dcdd05e416c6ec9b38ff78ab2c927476a1cedf611f45c67d274712909e423f4` |
| a1 | `score-20261031-085943` | `19cdf5bfa072e7295e7f06ec4d71596024ae2c8165710e98d9f9957a29bd63c1` | `b312ccfada527aba810a9ed838614c74564cd4f4cdd6ea83e290e21e8c1ed9d3` |
| a2 | `score-20261031-090159` | `4f1b38cdd3fda0f83b61473c13f90b8e09047df4c692a552d1239f29762d5130` | `10ac9251f2f555fa81650876d3c79b43f90f733e6ab9bc3eaafade835fbf6705` |
| a3 | `score-20261031-090355` | `b25610e35bb8ee980173d2c57e261ee2a497169de99da0f9ebe68c5293786dd4` | `b6614a445f64ae78db9dbfa6f4516e80eb522ae8d850ec9eb2e3d2d627d389ac` |
| b-full | `score-20261031-090624` | `734e4b71752d32baa355ab0bfc0992d9c496d8d67d3c8dd5ad7eb63bc24fd9a8` | `73e65733c8ba5ce1fe623b6eeca032aff9f84a40aaa4a6a2a9cc3de2392be487` |
| c | `score-20261031-090915` | `3a65658d09fd59a68ca1d38d201a910b831411e18606165cf20991699c532985` | `a97688647c50ed9e1241a45c6d02989a18420e8105c2f629ef7a7d08ae51c4de` |

경로·run별 세트·키 분리(키 분리는 채점 때 표, K02 = 미정):

| 경로·run | 끝난 open 합친 (30) | 세트 A (15) | 세트 B (15) | 키 있음 (15) | 키 없음 (14) | 미정 K02 (1) | 대조군 open (6) | 세트 A 대조군 (3) | 세트 B 대조군 (6) | invalid | (a) 판정 가능 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| b-pre | 2 | 1 | 1 | 1 | 0 | 1 | 3 | 3 | 3 | 0 | - |
| b-delta | 2 | 1 | 1 | 1 | 0 | 1 | 3 | 3 | 3 | 0 | - |
| a1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 0 | valid |
| a2 | 3 | 2 | 1 | 2 | 1 | 0 | 3 | 3 | 3 | 0 | valid |
| a3 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 0 | valid |
| b-full | 2 | 2 | 0 | 1 | 0 | 1 | 3 | 3 | 3 | 0 | - |
| c | 3 | 3 | 0 | 2 | 1 | 0 | 3 | 2 | 3 | 0 | - |

- (a): 시도 3회, 유효 run 3개(a1·a2·a3), 판정 불가 0, invalid 0. run별 (끝난, 대조군, 판정 가능) = (0, 1, 예), (3, 3, 예), (0, 1, 예).

항목 id x 경로 판정:

| id | b-pre | b-delta | a1 | a2 | a3 | b-full | c |
|---|---|---|---|---|---|---|---|
| B02 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B04 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B05 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B12 | open | open | not-open | open | not-open | not-open | not-open |
| B13 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B15 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B16 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B17 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B18 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B19 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC01 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC02 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| BC03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| C01 | open | open | open | open | open | open | not-open |
| C02 | open | open | not-open | open | not-open | open | open |
| C03 | open | open | not-open | open | not-open | open | open |
| K01 | not-open | not-open | not-open | not-open | not-open | open | open |
| K02 | open | open | not-open | not-open | not-open | open | not-open |
| K03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K04 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K05 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| K06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K08 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| K09 | not-open | not-open | not-open | open | not-open | not-open | not-open |
| K10 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K12 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K15 | not-open | not-open | not-open | open | not-open | not-open | not-open |
| K16 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |

### 4.2 r2

| 경로·run | 출력 디렉터리 | summary.json sha256 | detail.json sha256 |
|---|---|---|---|
| b-pre | `score-20261031-095953` | `4e58ef4d856709c90f71c49754d1383bc9c2ec97c8db871a982671e7ea09959f` | `8943dc36bbee8601b376cef7c415e0d003570cd34e38135e222d2efdd2d3cc1b` |
| b-delta | `score-20261031-100543` | `43803c84f8d970a938fd6161635ceb17846a14a1342458953425f6e92614fff6` | `cb549ba48e30a135effdac0e2d1fe1a108db88b1736ae7b2f1890ad1f7741ca0` |
| a1 | `score-20261031-100559` | `53840818efbb21299814d161f30c971482bb051fa0d94d36f2b21b3a2facb4a5` | `66d0c2a439b0c40ee1e4af9d6000becb2e47e07bf16818fd874098b0bce0b301` |
| a2 | `score-20261031-100840` | `0aae51039019863b138f01328d2e56cafc802a8f269b92b7debbcbb1edd9d1b1` | `1a5cea08e27f5453362542b026c8b32c1d7c89ca1cac4a40f132621916b07d00` |
| a3 | `score-20261031-101036` | `17522d10ac0e66733533ec28084548c1b872c6cf103c1a3c1ab34cee8e149616` | `6ed8e405d4838b4ad7de8ff9faed18f61bb090658ac216869eb70e7b2e8ff560` |
| a4 | `score-20261031-101324` | `d48d1aee21297a5e09c20bbdb7eb88cb154c14cb574c9b3cac02f26abc7ceb28` | `4bab567f92ab029522188daf173799daffd76382a2fcea62bd69371084737ee6` |
| a5 | `score-20261031-101519` | `fd9f23183aa43d17efaf3c2971c4841cf018abb23efe5d7c4f14744484400601` | `6c6414e1320e0a3405495f2d86dc09b4cf6044a36593e599b23fe968681e14e0` |
| b-full | `score-20261031-101715` | `5e803338bce7246fe17fe0646798f07f7a6a2828d2555e40df308c095105a75b` | `54ccfe8278a03e9895f997e9464d8af97b6969d386ea33de2515238c7bb40d2b` |
| c | `score-20261031-102016` | `8065b6536dcc6bc106ac9165580461bf1bcb3daa5ae51b26a23e5f05715908fb` | `645162f446e5c11911fc368f037283fa1f2808eb5319d55d7a7405c3dd44162d` |

| 경로·run | 끝난 open 합친 (30) | 세트 A (15) | 세트 B (15) | 키 있음 (15) | 키 없음 (14) | 미정 K02 (1) | 대조군 open (6) | 세트 A 대조군 (3) | 세트 B 대조군 (6) | invalid | (a) 판정 가능 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| b-pre | 2 | 2 | 0 | 0 | 1 | 1 | 3 | 3 | 3 | 0 | - |
| b-delta | 2 | 2 | 0 | 0 | 1 | 1 | 3 | 3 | 3 | 0 | - |
| a1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | undecidable |
| a2 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 | 3 | 0 | valid |
| a3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | undecidable |
| a4 | 1 | 1 | 0 | 1 | 0 | 0 | 3 | 3 | 3 | 0 | valid |
| a5 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 | 3 | 0 | valid |
| b-full | 1 | 1 | 0 | 1 | 0 | 0 | 3 | 3 | 3 | 0 | - |
| c | 2 | 2 | 0 | 2 | 0 | 0 | 3 | 2 | 3 | 0 | - |

- (a): 시도 5회, 유효 run 3개(a2·a4·a5), 판정 불가 2개(a1·a3, 대조군 open 0), invalid 0. run별 (끝난, 대조군, 판정 가능) = (0, 0, 아니오), (0, 3, 예), (0, 0, 아니오), (1, 3, 예), (0, 3, 예).

| id | b-pre | b-delta | a1 | a2 | a3 | a4 | a5 | b-full | c |
|---|---|---|---|---|---|---|---|---|---|
| B02 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B04 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B05 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B12 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B13 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B15 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B16 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B17 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B18 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B19 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC01 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC02 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | open |
| BC03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| C01 | open | open | not-open | open | not-open | open | open | open | not-open |
| C02 | open | open | not-open | open | not-open | open | open | open | open |
| C03 | open | open | not-open | open | not-open | open | open | open | open |
| K01 | not-open | not-open | not-open | not-open | not-open | open | not-open | open | open |
| K02 | open | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K04 | open | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K05 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | open |
| K06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K08 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K09 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K10 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K12 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K15 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K16 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |

### 4.3 경로별 비용

| 반복 | 경로·run | 질의 수 | 소요(초) | llm-requests | 생성형(r2만 분리) |
|---|---|---|---|---|---|
| r1 | b-pre | 7 | 182.1 | 122 | - |
| r1 | b-delta | 7 | 18.0 | 0 | - |
| r1 | a1 / a2 / a3 | 1 / 1 / 1 | 135.7 / 115.3 / 149.3 | 8 / 5 / 5 | - |
| r1 | b-full | 7 | 170.9 | 114 | - |
| r1 | c | 36 | 160.8 | 71 | - |
| r2 | b-pre | 7 | 169.6 | 125 | 47(refresh_mental_model), 토큰 1,585,283/411 |
| r2 | b-delta | 7 | 16.3 | 0 | 0 |
| r2 | a1~a5 | 1씩 | 160.9 / 115.7 / 167.4 / 115.1 / 115.8 | 7 / 6 / 7 / 9 / 6 | 6 / 5 / 6 / 6 / 5(reflect) |
| r2 | b-full | 7 | 180.7 | 128 | 48(refresh_mental_model), 토큰 1,615,259/8,357 |
| r2 | c | 36 | 151.5 | 72 | 0 |

- `llm-requests`는 score `metrics.json`의 `llm_calls`(r1)와 사본 `GET .../llm-requests` 구간 행 수(r2)다. r1 은 경로 구간 행을 사본을 내리기 전에 provider 로 나누지 않아 생성형 분리가 없다.
- B 의 부분 재통합과 페이지 생성은 완전 재통합 횟수·누적 시간에 넣지 않는다(스펙 #318 상한).

## 5. full refresh 필요 여부

판정: **필요하다**(무효화 뒤 통합이 기본 트리거로 `-pre` 페이지를 delta 갱신하지만, 갱신이 무효화를 페이지 판정에 반영하지 못한다).

- 두 반복 모두 b-delta 의 항목 판정이 b-pre 와 36/36 같았다(끝난 open 2 -> 2). b-full 은 달랐다: r1 B12 open -> not-open, K01 not-open -> open. r2 K02·K04 open -> not-open, K01 not-open -> open.
- delta 갱신은 일부 페이지에만 일어났다. `-pre` 7개의 `last_refreshed_at`이 apply 뒤인 것은 r1 3개(all·rowan-tide·hindsight-crew), r2 2개(all·rowan-tide)다. r1 에서 무효화 대상이 있던 다른 project 페이지 하나는 갱신되지 않았다. 갱신 비용은 처치 구간 페이지 갱신 생성형 16(r1)·18(r2)건이다.
- 기준선 v1.1.0 의 두 생성 차는 1(`r2-t4-setb-baseline.md`)이라 full 과 pre 의 차이 일부는 생성 흔들림일 수 있다. delta 가 pre 와 완전히 같다는 점은 흔들림으로 설명되지 않는다.

## 6. memory_defense (2회차 arm 백업 뒤)

쓴 방법: 사본 env 파일 수동 추가 뒤 사본 컨테이너 재생성(`scripts/next-stack.sh` 변경 없음).
```bash
next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"
(umask 077; openssl rand -hex 32 > <리포 밖 0600 파일>)                 # 이 시험용 새 비밀, 값은 출력·기록하지 않음, down 뒤 삭제
{ echo HINDSIGHT_API_WEBHOOK_ALLOWED_HOSTS=gateway; echo HC_WEBHOOK_RECEIVER=1; printf 'HC_WEBHOOK_SECRET=%s\n' "$(cat <그 파일>)"; } >> "$HC_ENV_FILE"
docker compose --env-file "$HC_ENV_FILE" --project-directory "$PWD" -p hc-next -f docker-compose.yml -f docker-compose.next.yml \
  up -d --no-build --force-recreate --wait hindsight gateway
# upstream(사본 hindsight 컨테이너 안, 키는 stdin):
PATCH /v1/default/banks/<bank>/config {"updates": {"memory_defense": {"enabled": true, "rules": [{"on": "sensitive_data", "action": "redact"}]}}}
POST  /v1/default/banks/<bank>/webhooks {"url": "http://gateway:8888/webhooks/hindsight", "secret": <그 비밀>, "event_types": ["memory_defense.triggered"]}
# probe: 사본 게이트웨이 127.0.0.1:18888 /mcp/<bank> retain 1건(공개 AWS 문서의 예시 액세스 키 ID와 예시 비밀 키(실제 자격 값 아님), document_id r2-b-md-probe)
```

| 확인 | 결과 |
|---|---|
| 설정 | `memory_defense` override `{"enabled": false, "rules": []}` -> `{"enabled": true, "rules": [{"on": "sensitive_data", "action": "redact"}]}` |
| redact | probe 문서의 저장 기억 1건(world)에 `[REDACTED:` 있음, 예시 키 원문 없음 |
| 웹훅 배달 | `GET .../webhooks/{id}/deliveries` 1건: `memory_defense.triggered`, status completed, attempts 1, 응답 200 |
| 사본 게이트웨이 감사 | `identity=upstream-webhook`, `tool=webhooks`, `reason=memory_defense.triggered:redact` 1줄(2026-10-31T01:23:50Z) |

- 허용 호스트 `gateway`(compose 서비스 이름) 하나로 url_guard 를 통과했다. v1.1.0 `webhooks/url_guard.py` `parse_allowlist`는 IP·CIDR 이 아닌 항목을 호스트 이름으로 맞춘다. E1' 창 절차의 근거다.
- probe retain 응답 본문이 JSON 하나가 아니어서(`json.loads` "Extra data", 112번째 글자 뒤에 내용이 더 있음) 하네스 `Gateway.call`이 "non-JSON result"로 읽지 못했다. 응답 원문은 보지 않았다. 저장·배달·감사는 위와 같이 끝났다.
- 이 시험은 arm 백업 뒤라 백업·벤치에 들지 않는다.

## 7. 벤치(데이터 효과)

`bench/README.md` "1단계 arm 벤치" 명령 그대로, s1 = D10, s2 = 그 반복 arm 백업(`--expected-overrides <LINEAGE>`), 게이트웨이 팔 ref `270242e`, `--clock-from-set s1`(s2 시계가 다르다).

| 반복 | run | `arm_compare.py` exit | 판정 | 속도 비 | 섞은 p95 비 | 토큰 base / cand | 결과 바뀐 쿼리 |
|---|---|---|---|---|---|---|---|
| r1 | r2-b-1-r1 | 2 | INVALID(`no-prod-interference-signal`) | 1.0004 | 1.0144 | 295,475 / 295,422 | 3/40 |
| r1 | r2-b-1-r2(재측정) | 0 | PASS | 0.9975 | 0.9971 | 295,475 / 295,422 | 3/40 (7.5%) |
| r2 | r2-b-2-r1 | 1 | FAIL(cost regressed, 속도·p95 ok) | 0.9987 | 0.9949 | 295,475 / 295,655 | 3/40 (7.5%) |

- r1 첫 run INVALID 원인: 워크트리에 빈 `audit/` 디렉터리가 있어 운영 `audit` 심링크가 달리지 않았고, runner 가 운영 간섭 신호를 읽지 못했다. 디렉터리를 심링크로 바꾸고 같은 세트로 다시 쟀다(README "판정하지 않는 run"은 다시 잰다).
- 복원 sha256: s1 `8a4900ea...613f4`(D10), s2 각 LINEAGE `backup.pg0_sha256`과 같음(`--expect-sha` 통과).

## 8. arm 백업과 LINEAGE

| 반복 | 경로(리포 밖) | pg0.tar.gz sha256 | LINEAGE sha256 |
|---|---|---|---|
| r1 | `~/.local/share/hindsight-bench/arm-backups/r2-b-1-20261031-091208/` | `3ed144b630e10b273d62005063bc74e5f7eb9dc1819ca635cf26bf8cc8891264` | `1dd1c13b7bddcb323453a9672d5a53753323cc98beb0b8b78610ba27c2f4650d` |
| r2 | `~/.local/share/hindsight-bench/arm-backups/r2-b-2-20261031-102309/` | `babeb1774df21644397e4a69a98c63cef952f592cbeb0daa62e9ef91ac36121e` | `7cb3691c5113a62a58356ed053d574d64d595fe2a055930264c064035b372855` |

- LINEAGE 는 `schema_version` 2(`image` = 사본 hindsight 이미지 태그·ID, `invalidations` = rule_version·rule_module_commit·aliases_sha256·targets_file_sha256·targets 12·applied 12·started_at·invalidated_after_backfill 12·invalidated_after_drain 12), `settings` {}, `document_patches` 0, `expected_bank_overrides`는 백업 직전 사본 override(두 반복 같음).
- `scripts/backup.sh`는 워크트리 `backups/<TS>/`에 썼고 곧바로 위 경로로 옮겼다. 운영 체크아웃 `backups/`에는 쓰지 않았다.

## 9. K02 확정과 항목별 무효화 여부 (채점 뒤)

- K02: 세트 A 고정 세트 K02 의 근거 기억 6개(evidence `evidence-20261030-005937`)를 D10 복제본에서 읽어 #320 `extract_keys`(같은 별칭 표)를 돌렸다. 완료 근거 3개 중 2개와 인계 근거 2개가 키를 냈다 -> **K02 = true**.
  `r2-key-split.json`의 `A.K02`만 `null` -> `true`로 고쳤다. sha256 전 `3575b1c02e5baaca34c2e54f9dbb57ba87b8fe460ed636e0f1eef9d3d604138a` -> 후 `3450a44fb682ca1d3137cb20620939aecf673e5774a12fdd60e53b17e89bcc33`.
  위 4절 표는 고치기 전 표(K02 미정)로 채점한 값이다. #326 은 고친 표로 `rescore`한다.
- 항목별 "열림 원문이 무효화됐는가": 항목 근거 기억(세트 A·B evidence)이 backfill 대상 쌍의 완료 쪽이거나(`completion-pair`), 근거 기억의 키가 무효화 대상 쌍의 키와 겹치면(`key`) yes. 두 반복 대상 목록이 같아 판정도 같다.

| id | 근거 기억 키 수 | 열림 원문 무효화 | 경로 |
|---|---|---|---|
| B02 | 2 | no | - |
| B03 | 6 | yes | completion-pair |
| B04 | 7 | yes | completion-pair |
| B05 | 2 | no | - |
| B06 | 24 | yes | key |
| B07 | 7 | no | - |
| B11 | 28 | yes | key |
| B12 | 25 | yes | completion-pair |
| B13 | 0 | no | - |
| B14 | 0 | no | - |
| B15 | 0 | no | - |
| B16 | 0 | no | - |
| B17 | 0 | no | - |
| B18 | 0 | no | - |
| B19 | 0 | no | - |
| K01 | 23 | yes | completion-pair |
| K02 | 2 | no | - |
| K03 | 0 | no | - |
| K04 | 5 | no | - |
| K05 | 3 | no | - |
| K06 | 6 | no | - |
| K07 | 0 | no | - |
| K08 | 0 | no | - |
| K09 | 4 | no | - |
| K10 | 4 | no | - |
| K11 | 7 | yes | completion-pair |
| K12 | 5 | yes | completion-pair |
| K14 | 2 | no | - |
| K15 | 3 | no | - |
| K16 | 5 | no | - |

## 10. 알려진 한계 (final-review 기록)

- `--out`은 리포 밖 경로를 쓰는 운영 규칙에 기댄다. 도구는 경로를 검사하지 않고, 파일을 쓴 뒤 0600 으로 바꾼다.
- `over_cap` 판정은 그 순간 valid 인 대상만 센다. 부분 실패 뒤 같은 사본에서 `--apply`를 다시 돌리면 상한 판정이 바뀔 수 있다. 반복마다 D10 새 복원이 규칙이라 같은 사본 재실행은 없다.

## 11. 사본·복제본 구간 (canary-exclude 형식)

```
2026-10-31 08:50:08 2026-10-31 09:17:39  # #322 B r1 hc-next D10 사본(up 08:50:08 ~ down 확인 09:12:39 +300s)
2026-10-31 09:12:58 2026-10-31 10:04:13  # #322 B r1 bench 복제본(up 09:12:58 ~ 정리 확인 09:59:13 +300s)
2026-10-31 09:59:21 2026-10-31 10:29:53  # #322 B r2 hc-next D10 사본(up 09:59:21 ~ down 확인 10:24:53 +300s)
2026-10-31 10:24:57 2026-10-31 10:54:34  # #322 B r2 bench 복제본(up 10:24:57 ~ 정리 확인 10:49:34 +300s)
```

끝 상태: `bash scripts/next-stack.sh status` exit 3, `docker ps -aq --filter label=hc-bench=1`·`docker volume ls -q --filter label=hc-bench=1`·`docker network ls -q --filter name=hc-bench`·`lsof -nP -iTCP:18780-18799 -sTCP:LISTEN` 빈 출력.
