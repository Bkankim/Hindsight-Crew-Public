# 런 2 C arm: main 고정 빌드에서 작업 키 backfill 뒤 범위 (iv') 이전·완전 재통합 2회 (issue #325)

상위: #318. 바탕 절차는 #324(A arm), 무효화 도구는 #322 `scripts/item-backfill.py`(머지본 c794bb9), 규칙은 #320 `gateway/item_keys.py`, 채점 하네스는 #323 머지본(`bench/arch_v2_regression.py`).
이 문서에는 id·판정·해시·수만 쓴다(항목 이름·질의·응답 원문·기억 본문·뱅크 실명 없음, 공개 전환 가드 #4). 뱅크는 `<bank>`, 운영 체크아웃은 `<운영 체크아웃>`이다.
arm 판정은 #326이 `judge-arms`로 낸다. 이 문서는 값만 적는다.

## 1. 반복 수와 근거

- 반복 2회. 코디네이터 사본 차례 메시지(2026-10-31 17:48): #324 1회차 재통합 2시간 19분 39초·생성형 통합 654건이 1회 상한의 70%(2시간 48분·1,400건)에 못 미쳐 스펙 #318 상한 규칙대로 C 2회.
- 예산: 시작 때 남은 완전 재통합 2회, 누적 사용 5시간 34분 35초(스펙 정의 "설정 적용부터" 기준, 코디네이터 정정 메시지), 남은 9시간 25분 25초. r2 시작 전 남은 시간은 r1을 뺀 6시간 56분 12초로 1회 상한 4시간 이상이었다.
- 예비 완전 재통합은 #324 3g OOM 중단으로 이미 썼다. 그래서 C 사본은 `HC_NEXT_MEM_LIMIT=5g`로 띄웠다(#324와 같은 조건, 6g 미만).

## 2. 공통 입력

| 항목 | 값 |
|---|---|
| 워크트리 HEAD | `c794bb9`(#322 머지, #323 머지 포함. 사본 차례 메시지의 머지 커밋과 같음, merge 없음) |
| 사본 이미지 | `hc-upstream-main:b801b694d060`, ID `sha256:7707479b02f9b2008a3e1cb7fda308bc579b95732cf7cc25630daaf3c4124c02`(두 반복 `docker inspect -f '{{.Config.Image}} {{.Image}}' hc-next-hindsight-1` 같음), `--llm opus`, mem_limit 5g(5368709120) |
| `RULE_VERSION` | `item-keys-v1`(두 반복 같음, backfill summary 줄). `artifacts/arch-v2/r2-b-arm.md` 1절 값 `item-keys-v1`과 같다. 규칙 모듈 커밋 `ee9aadd` |
| 별칭 표 | `~/.local/share/hindsight-bench/arch-v2/r2-aliases.json` sha256 `21d1b75bae4c66f23f28061d6f1e9fae35ea4bfa931e57b63875aa6de606cdf6`. `r2-b-arm.md` 1절 값 `21d1b75bae4c66f23f28061d6f1e9fae35ea4bfa931e57b63875aa6de606cdf6`과 같다. apply 는 `--expect-aliases-sha256`로 고정 |
| 적용 설정 | 오버레이 `templates/bank-config/local/<bank>.json`(gitignore, 커밋 안 함) = 운영 오버레이(`memory_defense` `{"enabled": false, "rules": []}`) + `default_observation_scopes` (iv') `[["project:*"],["topic:demo-style*"],["topic:demo-brand*"]]`. #324와 같은 값 |
| 세트 A 고정 세트 | `r1-fixed-set.json` sha256 `3fbe114b588af11e154b343639b8d5dcd9efe4b3c49343fb91559afbdd9d904d`(채점 전 대조) |
| 키 유무 표 | `r2-key-split.json` sha256 `3450a44fb682ca1d3137cb20620939aecf673e5774a12fdd60e53b17e89bcc33`(#322 9절 K02 확정 뒤 표. 이 티켓 채점 summary 는 이 표로 냈다, 미정 0) |
| 세트 합치기 조건 | 두 정답표 `query_date`·`ignore_mentioned_at_from`·`query_instruction` 셋 다 `True` -> 페이지 공유, 접미사 기본값 |
| 질의 시각 | `query_timestamp=2026-10-28T14:59:59Z` |

정답표·고정 세트는 backfill 입력이 아니다(`--aliases`만 받는다, `r2-b-arm.md` 1절 grep 과 같은 도구).

명령(워크트리, `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"` 뒤):
```bash
HC_NEXT_IMAGE=hc-upstream-main:b801b694d060 HC_NEXT_MEM_LIMIT=5g HC_NEXT_BACKUP_SHA256=8a4900ea... \
  bash scripts/next-stack.sh up --llm opus --backup "$PROD/backups/keep-arch-v2-20261027-043001"
python3 -B scripts/bank-config.py apply --bank "$BANK"                                   # (iv') 오버레이
python3 -B -m bench.arch_v2_regression score "${A2[@]}" --paths b --page-suffix -pre
python3 -B scripts/item-backfill.py --bank "$BANK" --aliases "$ALIASES" --expect-aliases-sha256 21d1b75b... \
  --out <리포 밖>/r<r>-targets-dryrun.jsonl --dry-run
python3 -B scripts/item-backfill.py --bank "$BANK" --aliases "$ALIASES" --expect-aliases-sha256 21d1b75b... \
  --out <리포 밖>/r<r>-targets-apply.jsonl --apply --targets <리포 밖>/r<r>-targets-dryrun.jsonl --batch-size 20
python3 -B scripts/item-backfill.py --bank "$BANK" --check <리포 밖>/r<r>-targets-dryrun.jsonl     # apply 직후, drain 뒤
python3 -B scripts/scope-migrate.py migrate --bank "$BANK" --scopes '[["project:*"],["topic:demo-style*"],["topic:demo-brand*"]]' --apply
python3 -B scripts/scope-migrate.py drain --bank "$BANK" --since <apply 시작> --max-llm 2000 --timeout 14400 --timeseries <리포 밖>
python3 -B -m bench.arch_v2_regression stats --wait 5000 --wait-pages
```

- drain exit 1(상태 합 일시 불일치, "the five operation states add up to N but the operations total is M")은 상한이 아니라 같은 `--since`로 다시 불렀다(r1 24회, r2 28회). 상한 시계는 `--since`부터라 재호출로 바뀌지 않는다.

## 3. 반복별 처치와 재통합 비용

| 항목 | r1 | r2 |
|---|---|---|
| `restore:` 줄 | `restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4` | 같은 줄(sha256 같음) |
| 사본 up(KST) | 2026-10-31 17:49:48 | 2026-10-31 21:38:19 |
| 실패·취소 기준선(up 직후) | failed 40, cancelled 0 | failed 40, cancelled 0 |
| 설정 적용(bank-config apply) | 2026-10-31T08:50:39Z, 30초 동안 pending_consolidation 0·llm-requests 0(통합 시작 안 함) | 2026-10-31T12:38:47Z, 같음(0·0) |
| `-pre` 생성 | 17:51:15~17:55:12 KST | 21:39:17~21:42:33 KST |
| dry-run 집계 | 기억 2,635, 완료 844, 키 있는 완료 254, 쌍 65, 대상 12, `over_cap` 0 | 같음 |
| 대상 수·정렬 쌍 집합 sha256 | 12, `4df913372f58a25a2f518db4ce9b9d590f68c56ef44b8f737cd50f9eff90c77e`(#322 1회차 `targets-r1.jsonl`에서 같은 방식으로 뜬 값과 같음, 파일 sha256 도 `6bda62b8...226b`로 같음) | 같음 |
| apply 목록 = dry-run 목록 | `--targets` 대조 통과, `cmp` 동일 | 같음 |
| `--apply` 시작(= 처치 시작, drain `--since`) | 2026-10-31T08:55:33Z | 2026-10-31T12:42:53Z |
| invalidated 수, backfill 직후 / 범위 PATCH drain 뒤 | 12 / 12(일치, 유효) | 12 / 12(일치, 유효) |
| 아카이브 행 확인 | 4절(통과) | 하지 않음(1회차만) |
| 문서 PATCH(`migrate --apply`) | 2,633문서, 08:57:41Z~09:02:17Z(4절 사전 PATCH 3문서는 이 2,633에 포함, 같은 값 재전송) | 2,633문서, 12:42:55Z~12:47:21Z |
| drain 직전 상한 확인 | 따로 실행하지 않음(부분 이행). 첫 drain poll(09:02:25Z)로 대신: 처치 시작부터 412초, 생성형 통합 47건. 사유: 이슈 목표 6의 요건을 drain 시작 전에 따로 실행하지 않았고, 이를 짚은 코디네이터 정정 메시지는 r1 drain 시작(09:02:25Z) 뒤에 수신했다. 값이 1회 상한(4시간·2,000건)과 멀어 결과 영향 없음(final-review 20261101-012220 Spec (a)) | 12:47:35Z 따로 실행: backfill 기준 282초·생성형 통합 39건(설정 기준 528초·39건) |
| drain 완료(= 재통합 끝) | 2026-10-31T11:19:52Z(exit 0) | 2026-10-31T15:01:38Z(exit 0) |
| 소요, backfill 시작 기준 | 2시간 24분 19초 | 2시간 18분 45초 |
| 소요, 스펙 정의 시작(설정 적용) 기준 | 2시간 29분 13초 | 2시간 22분 51초 |
| `llm-requests`(backfill 시작~drain 완료) | 2,096: 생성형 717(통합 659, 그 밖 58), 임베딩 1,346, 리랭크 33 | 2,056: 생성형 702(통합 650, 그 밖 52), 임베딩 1,325, 리랭크 29 |
| 관찰 수 전 -> 후 | 4,924 -> 15,566 | 4,924 -> 15,447 |
| 관찰 범위 수 전 -> 후 | 1,290 -> 309 | 1,290 -> 309 |
| drain 뒤 실패·취소 | failed 40, cancelled 0(기준선과 같음) | failed 40, cancelled 0(기준선과 같음) |
| `stats --wait-pages` | 11:51:04Z 2회 연속 stale 0·refresh 0 | 15:04:32Z 같음 |
| 1회 상한(4시간·생성형 통합 2,000건) | 닿지 않음(62%, 33%) | 닿지 않음(60%, 33%) |
| 중단·무효 | 없음 | 없음 |
| drain 시계열(리포 밖 `r2-c/`) | `r1-drain.jsonl` sha256 `9c306080...a4c6`, 아카이브 확인 구간 `r1-drain-arch.jsonl` `497730c7...0b7c` | `r2-drain.jsonl` sha256 `b9f9e7e9...58f3` |

- drain 과 별개로 #324 사본 OOM 재발을 보려고 10분 간격 `docker stats`를 기록했다: 사본 hindsight 최대 2.848GiB / 5GiB, 운영 `<운영 컨테이너>` 최대 1.412GiB(6GiB 문턱 아래, ALERT 0, OOM 없음).
- r1 의 drain 은 사용량 한도로 감시 세션이 20:19~20:50 KST 동안 멈췄다. drain 프로세스(백그라운드)는 계속 돌았고 완료 시각은 drain 자신의 마지막 poll 이다. 채점 시작만 30분 늦어졌다.
- 누적 예산(코디네이터 정정 기준, 설정 적용부터): 시작 5시간 34분 35초 + r1 2시간 29분 13초 + r2 2시간 22분 51초 = **10시간 26분 39초 / 15시간**, 완전 재통합 남은 횟수 0.

## 4. 아카이브 행 확인 (1회차, 전수 PATCH 전)

backfill(08:55:33Z) 직후 무효화된 대상 가운데 문서가 서로 다른 3개를 골라 그 문서만 upstream `PATCH .../documents/<id>` `{"observation_scopes": <(iv')>}`로 먼저 보냈다(08:55:42Z~08:55:43Z). 그다음 `drain --since 2026-10-31T08:55:33Z`(exit 0, 08:57:23Z, 61.2초, 생성형 통합 2) 뒤 확인했다.

| 표본 기억 id | 문서 id | `memories/list?state=invalidated`에 있음 | `GET .../memories/{id}` state |
|---|---|---|---|
| 182cb955-6c3f-4532-ba52-9dd0189cc7db | e1317020-f883-48fb-9701-024db781c036 | 예 | invalidated |
| 44b86c05-1088-4b8d-a136-634a75a894bf | 042b8513-1229-4ed8-9800-912d0cb421f7 | 예 | invalidated |
| 47e0a3d3-3c5e-4ffe-afe3-f224c506c5fc | f35263ab-bcb8-4b38-bf7f-20e8ee842c4b | 예 | invalidated |

- `GET .../memories/list?type=observation&time_field=updated_at&start_date=2026-10-31T08:55:42Z`: PATCH 뒤 바뀐 관찰 8개(listing total 8). 각 관찰을 `GET .../memories/{id}`로 읽어 `source_memory_ids`를 대조했다. **표본 기억을 근거로 가진 관찰 0개**(기대 0).
- 판정: 문서 `PATCH observation_scopes`는 아카이브(무효화) 행을 되살리지도 재통합에 넣지도 않았다. C 설계 전제가 맞아 전수 PATCH 로 넘어갔다. 두 반복의 drain 뒤 invalidated 12/12 도 같은 결론이다.
- 쓴 도구: 리포 밖 일회용 헬퍼(`bank-config.py`의 `upstream`으로 사본 hindsight 컨테이너 안에서 호출, 키는 stdin). 리포에 넣지 않았다.

## 5. 채점 (반복마다, #323 머지 하네스, 합친 세트 A∪B)

순서: `-pre` 생성(처치 전) -> 처치 -> b-delta(`--paths b --read-pages --page-suffix -pre`) -> (a) 유효 run 3개까지(시도 상한 6) -> b-full(`--paths b`) -> c(`--paths c`).
출력 디렉터리는 `~/.local/share/hindsight-bench/arch-v2/` 아래다(#326 manifest 입력). `deleted_evidence` 합계는 b-delta·b-full 모두 두 반복 0 이다.
키 분리는 K02 확정 뒤 표(`3450a44f...`)라 미정 열이 없다(키 있음 16, 키 없음 14).

### 5.1 r1

| 경로·run | 출력 디렉터리 | summary.json sha256 | detail.json sha256 |
|---|---|---|---|
| b-pre | `score-20261031-175115` | `478bf137d3a4833bcfe52664c38f75f9512c992ca9b47b757819d61eaeef1790` | `7723b5852aa505a5f09b5f4bbcf526e44e4d4cb7233992676833877ca6bfa2ca` |
| b-delta | `score-20261031-205121` | `4c157b8f1f8629d149be0a578e19c2e56e13441e13eead92b5145e97baea386e` | `e8b2d5239c8e8fc0f8f43179806c6b0f1af63ecd5ac247a32b7588c73bacf502` |
| a1 | `score-20261031-205225` | `38e551baf7eb4469bf0fa59cd222ff476a1af9b40fae835aafeea6223862cb33` | `00805347b63f141b8b9862e22c029679487d87b9447a7a96b9b6a20738ba1380` |
| a2 | `score-20261031-205345` | `8f1a1df3483450061c7a0ae639994b498d19eca1280b7571259a1f542872faec` | `6f279c6b708ada16396b251b2215977f15a9a8b33de8275b9840553f7d32941a` |
| a3 | `score-20261031-205540` | `90e720fe7f3a432940c6b8c4c941f1d38948b842c117662f28c5d8e2caf9f368` | `fe91184d809fc3cab7eeb235cbc762ed2a609521ef38481e30f8497c4463640f` |
| a4 | `score-20261031-205745` | `310c5482c8e953c5a97cfcb27005862c4065c26b4423a66276c185ffac9e81ff` | `da59996173d769bcc0e55f0df49ea08f7f4058ba5d9eb9ad78cc27f53fea5707` |
| a5 | `score-20261031-205912` | `4bca5fa6eb148ff244917155b220de2487b550c7de4d5c6f37ab33b17639c9a1` | `d29d376ff13f5bcacdeff626f7990b35ab963a3c22f83636f0730ff267ff19b0` |
| b-full | `score-20261031-210030` | `4546304b67bbcf6bc4b3748b31226eba8e2d34b98759ee3ffd36e2f0723c6c1e` | `4de4d590649cf0a2c852b058cc1aba2d9cf75751a93b8353ef0a3cc7f2a68157` |
| c | `score-20261031-210421` | `510e6b43ab6f114ae304f69c01db6e6872b1c1480356307dd4b4627e5a892151` | `896cc467546bef2cabca74e90e975f5540266488bdb6da8427bbb90db64b2d19` |

| 경로·run | 끝난 open 합친 (30) | 세트 A | 세트 B | 키 있음 | 키 없음 | 대조군 open 합친 (6) | 세트 A 대조군 (3) | 세트 B 대조군 (6) | invalid | (a) 상태 |
|---|---|---|---|---|---|---|---|---|---|---|
| b-pre | 3 | 2 | 1 | 3 | 0 | 3 | 3 | 3 | 0 | - |
| b-delta | 2 | 1 | 1 | 2 | 0 | 3 | 3 | 3 | 0 | - |
| a1 | 1 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | undecidable |
| a2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | undecidable |
| a3 | 1 | 1 | 0 | 1 | 0 | 3 | 3 | 3 | 0 | valid |
| a4 | 1 | 1 | 0 | 1 | 0 | 3 | 3 | 3 | 0 | valid |
| a5 | 1 | 1 | 0 | 1 | 0 | 3 | 3 | 3 | 0 | valid |
| b-full | 1 | 1 | 0 | 1 | 0 | 3 | 3 | 3 | 0 | - |
| c | 4 | 4 | 0 | 2 | 2 | 2 | 2 | 2 | 0 | - |

- (a): 시도 5회, 유효 run 3개(a3·a4·a5), 판정 불가 2개(a1·a2, 대조군 open 0), invalid 0. run별 (끝난, 대조군, 판정 가능) = (1, 0, 아니오), (0, 0, 아니오), (1, 3, 예), (1, 3, 예), (1, 3, 예).

| id | b-pre | b-delta | a1 | a2 | a3 | a4 | a5 | b-full | c |
|---|---|---|---|---|---|---|---|---|---|
| K01 | not-open | not-open | not-open | not-open | open | open | open | not-open | open |
| K02 | open | open | open | not-open | not-open | not-open | not-open | open | not-open |
| K03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K04 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K05 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | open |
| K06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K08 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | open |
| K09 | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K10 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K12 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K15 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K16 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | open |
| B02 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B04 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B05 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B12 | open | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B13 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B15 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B16 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B17 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B18 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B19 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| C01 | open | open | not-open | not-open | open | open | open | open | not-open |
| C02 | open | open | not-open | not-open | open | open | open | open | open |
| C03 | open | open | not-open | not-open | open | open | open | open | open |
| BC01 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC02 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |

### 5.2 r2

| 경로·run | 출력 디렉터리 | summary.json sha256 | detail.json sha256 |
|---|---|---|---|
| b-pre | `score-20261031-213917` | `138e6466f778ddfdb90ba76276b6f39e750c1ec1b0838f6c88bed8de9b285ab2` | `086b02fea53e5b27b985693e433565e77b198831614b551dc96d814cca7a63df` |
| b-delta | `score-20261101-000441` | `3d45135986982a2d207c3e39ecedeef7b412231bab2faed1a3a77315216f7574` | `9a79f7d3e74aca6d7ac861080f6d2fa64f4d85e7d8bb6f780b0efec131ed2f51` |
| a1 | `score-20261101-000524` | `c9c19d27e6c100c8fd7fa93446dc9db1cd0b069dca635d7b79ed27d431871fd5` | `2adc9bbb0e74ccd9b2ef6702639efd836f41c9af7d3d70f2a20eab7a98611c97` |
| a2 | `score-20261101-000700` | `93f7a1bc4bd08af708884e4f9f090c3dbb170cedccda82e25b7c38ecfc7adac2` | `bd229422e46dbd93c75b448f938bcbac695ef31eeaa97ad11b315a4a063b6299` |
| a3 | `score-20261101-000821` | `d44bf24197b51dcb0e1912a2c53e7041bf7302ed1a59f83549d37ee2eeda9e4c` | `a21f1bedcbb236fd6a2c3d870027a41b47ec7f3d66cc1ebc0019bfa436860a98` |
| b-full | `score-20261101-000959` | `9fb1667a1909ff597b3719cb2d0e990ef5c2b519e00bc24142fd279521067a89` | `0a0768b3d466182c1b6cde2736a69355750aecc6c9a6470bacfa258252db301f` |
| c | `score-20261101-001320` | `e011f1b2da7f592577f431de4b9de1d273dd9fb3e4f8c17fac2cd363c896e68e` | `a176d468d85a1141d2b3b7a8ff6dfe8a23f12954267fdd6f9491ed62ff318a78` |

| 경로·run | 끝난 open 합친 (30) | 세트 A | 세트 B | 키 있음 | 키 없음 | 대조군 open 합친 (6) | 세트 A 대조군 (3) | 세트 B 대조군 (6) | invalid | (a) 상태 |
|---|---|---|---|---|---|---|---|---|---|---|
| b-pre | 3 | 2 | 1 | 3 | 0 | 3 | 3 | 3 | 0 | - |
| b-delta | 3 | 2 | 1 | 3 | 0 | 3 | 3 | 3 | 0 | - |
| a1 | 1 | 1 | 0 | 0 | 1 | 1 | 1 | 1 | 0 | valid |
| a2 | 1 | 1 | 0 | 1 | 0 | 3 | 3 | 3 | 0 | valid |
| a3 | 1 | 1 | 0 | 1 | 0 | 3 | 3 | 3 | 0 | valid |
| b-full | 2 | 1 | 1 | 2 | 0 | 3 | 3 | 3 | 0 | - |
| c | 4 | 3 | 1 | 3 | 1 | 2 | 2 | 2 | 0 | - |

- (a): 시도 3회, 유효 run 3개(a1·a2·a3), 판정 불가 0, invalid 0. run별 (끝난, 대조군, 판정 가능) = (1, 1, 예), (1, 3, 예), (1, 3, 예).

| id | b-pre | b-delta | a1 | a2 | a3 | b-full | c |
|---|---|---|---|---|---|---|---|
| K01 | open | open | not-open | open | open | not-open | open |
| K02 | open | open | not-open | not-open | not-open | not-open | not-open |
| K03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K04 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K05 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| K06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K08 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K09 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K10 | not-open | not-open | not-open | not-open | not-open | open | not-open |
| K11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K12 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K15 | not-open | not-open | open | not-open | not-open | not-open | not-open |
| K16 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| B02 | not-open | not-open | not-open | not-open | not-open | not-open | open |
| B03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B04 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B05 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B06 | not-open | not-open | not-open | not-open | not-open | open | not-open |
| B07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B11 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B12 | open | open | not-open | not-open | not-open | not-open | not-open |
| B13 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B15 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B16 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B17 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B18 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| B19 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| C01 | open | open | open | open | open | open | not-open |
| C02 | open | open | not-open | open | open | open | open |
| C03 | open | open | not-open | open | open | open | open |
| BC01 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC02 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| BC03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open |

### 5.3 경로별 값 요약 (판정은 #326)

| 경로 | r1 | r2 | 차(재통합 사이 흔들림) |
|---|---|---|---|
| (a) 유효 run 3개 끝난 open 평균 | 1.00(1, 1, 1) | 1.00(1, 1, 1) | 0 |
| (b) full 끝난 open | 1 | 2 | 1 |
| (b) delta 끝난 open(보고만) | 2 | 3 | 1 |
| (c) 끝난 open | 4 | 4 | 0 |
| 대조군 open (a) 유효 run / b-full / c | 3, 3, 3 / 3 / 2 | 1, 3, 3 / 3 / 2 | - |
| (a) 대조군 0 run 수 | 2 | 0 | - |
| invalid | 0 | 0 | 0 |

- 세트별(끝난 open): b-full r1 세트 A 1·세트 B 0, r2 세트 A 1·세트 B 1. c 는 r1 A 4·B 0, r2 A 3·B 1. (a) 유효 run 은 두 반복 모두 세트 A 1·세트 B 0.
- 항목 수준 흔들림(끝난 항목만): b-full 은 r1 K02, r2 B06·K10 으로 겹치는 항목이 없다. c 는 r1 K01·K05·K08·K16, r2 B02·K01·K05·K16 으로 K01·K05·K16 셋을 공유한다(5.1·5.2 id 표).
- 관찰 수 흔들림: 15,566 vs 15,447(차 119, 0.8%), 관찰 범위 수 309 vs 309. 벤치 토큰(cand) 322,787 vs 321,813(차 974).

### 5.4 경로별 비용

| 반복 | 경로·run | 질의 수 | 소요(초) | llm_calls(score `metrics.json`) |
|---|---|---|---|---|
| r1 | b-pre | 7 | 237.0 | 136 |
| r1 | b-delta | 7 | 63.5 | 0 |
| r1 | a1~a5 | 1씩 | 79.6 / 115.2 / 124.7 / 86.4 / 77.6 | 9 / 13 / 15 / 9 / 9 |
| r1 | b-full | 7 | 230.8 | 127 |
| r1 | c | 36 | 150.5 | 71 |
| r2 | b-pre | 7 | 195.3 | 142 |
| r2 | b-delta | 7 | 42.3 | 0 |
| r2 | a1~a3 | 1씩 | 96.2 / 80.4 / 97.8 | 12 / 12 / 15 |
| r2 | b-full | 7 | 201.1 | 129 |
| r2 | c | 36 | 148.2 | 71 |

- `llm_calls`는 경로 구간의 사본 `llm-requests` 행 수(생성형·임베딩·리랭크 합)다. 경로별 생성형 분리는 사본을 내리기 전에 뜨지 않아 없다. 재통합 구간의 분리는 3절 표다.

## 6. 벤치(데이터 효과)

`bench/README.md` "1단계 arm 벤치" 명령 그대로(`PROD=` 줄만 common 형태), s1 = D10, s2 = 그 반복 arm 백업(`--expected-overrides <LINEAGE>`), 게이트웨이 팔 ref `270242e`, `--clock-from-set s1`, 1 run(dev 40쿼리 x 3라운드). 벤치 전 `test -L audit` 통과.

| 반복 | run | `arm_compare.py` exit | 판정 | 속도 비(95% CI) | 섞은 p95 비(95% CI) | 토큰 base / cand(변화) | 결과 바뀐 쿼리 |
|---|---|---|---|---|---|---|---|
| r1 | r2-c-1-r1 | 1 | FAIL(cost regressed, 속도·p95 ok) | 0.9213(0.8913~0.9490) | 0.9544(0.9185~0.9809) | 295,475 / 322,787(+9.24%) | 40/40 |
| r2 | r2-c-2-r1 | 1 | FAIL(cost regressed, 속도·p95 ok) | 0.9250(0.8920~0.9578) | 0.9521(0.9427~0.9710) | 295,475 / 321,813(+8.91%) | 40/40 |

- 두 run 모두 `valid: true`, `invalid_reasons` 없음. verify s1·s2 `pass: true`. 복원 sha256: s1 `8a4900ea...613f4`(D10), s2 각 LINEAGE `backup.pg0_sha256`과 같음(`--expect-sha` 통과).
- bench 복제본은 운영 이미지(v1.1.0)로 떴고 main 사본이 만든 백업을 복원·verify 했다(오류 없음).
- 출력: `~/.local/share/hindsight-bench/arch-v2/r2-c-<r>-bench/`(compare·verify·up·gw json), run 디렉터리 `bench/.data/runs/r2-c-<r>-r1`(운영 체크아웃 `bench/.data` 아래).

## 7. arm 백업과 LINEAGE

| 반복 | 경로(리포 밖) | pg0.tar.gz sha256 | LINEAGE sha256 |
|---|---|---|---|
| r1 | `~/.local/share/hindsight-bench/arm-backups/r2-c-1-20261031-210951/` | `8ba46cb79d39635d2eaca6b29cb059a11fd90e101352105df102e7690bc249c0` | `a56849aaa229bd93de1ee01c7c0fedaa1a81133c3ff17e813b748fcdc5471734` |
| r2 | `~/.local/share/hindsight-bench/arm-backups/r2-c-2-20261101-002258/` | `7dae29a051cabaa3f3f4f5eae0c5ecfe0b6c15d4b6a82dbadd76d3955d5a4ef8` | `8e156f55803549fc43f7b6fe89aa14739bb1c98606f4db8d78d72657912ea23d` |

- LINEAGE 는 `schema_version` 2: `image` = `hc-upstream-main:b801b694d060` / `sha256:7707479b...4c02`, `invalidations` = rule_version `item-keys-v1`·rule_module_commit `ee9aadd9...`·aliases_sha256 `21d1b75b...`·targets_file_sha256 `6bda62b8...`·targets 12·applied 12·started_at(apply 시작)·invalidated_after_backfill 12·invalidated_after_drain 12. `settings`는 (iv')와 `memory_defense`, `document_patches` 2,633, `reconsolidation`에 `settings_applied_at`·`started_at`·`drained_at`·`llm_requests`·생성형·생성형 통합·`mem_limit`(#324 LINEAGE 와 같은 확장 키), `expected_bank_overrides`는 백업 직전 사본 override(두 반복 같음).
- `scripts/backup.sh`는 워크트리 `backups/<TS>/`에 썼고 곧바로 위 경로로 옮긴 뒤 빈 `backups/`를 지웠다. 운영 체크아웃 `backups/`에는 쓰지 않았다(전후 최신 디렉터리 `20261031-051012` 그대로).

## 8. 사본·복제본 구간 (canary-exclude 형식)

```
2026-10-31 17:49:48 2026-10-31 21:15:17  # #325 C r1 hc-next D10 사본(up 17:49:48 ~ down 확인 21:10:17 +300s)
2026-10-31 21:10:37 2026-10-31 21:43:09  # #325 C r1 bench 복제본(up 21:10:37 ~ 정리 확인 21:38:09 +300s)
2026-10-31 21:38:19 2026-11-01 00:28:12  # #325 C r2 hc-next D10 사본(up 21:38:19 ~ down 확인 00:23:12 +300s)
2026-11-01 00:23:15 2026-11-01 00:56:25  # #325 C r2 bench 복제본(up 00:23:15 ~ 정리 확인 00:51:25 +300s)
```

끝 상태: `bash scripts/next-stack.sh status` exit 3, `docker ps -aq --filter label=hc-bench=1`·`docker volume ls -q --filter label=hc-bench=1`·`docker network ls -q --filter name=hc-bench`·`lsof -nP -iTCP:18780-18799 -sTCP:LISTEN` 빈 출력(2026-11-01 00:51:30).
