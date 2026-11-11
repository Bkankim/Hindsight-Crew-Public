# R5-B4 S3 열린 키 표 - 저장 역할 기반 결정적 원장, 사본 확인 (issue #377, 상위 #360)

**D10 사본(S4 #376 백필 저장소)에서 플래그를 켜면 dev 표본 t 3개 recall 30회 모두에 표가 붙었다.**
**지연 p95는 비열화(off 3.60s, on 3.59s)이고, 응답 payload 토큰 증가는 t별 +0.95% / +1.49% / +5.64%(전체 +2.69%)다.**
**저장소를 치우면 표 없이 off와 같은 응답(fail-open)이고 감사 사유는 `no-store`다.**

## 1. 구현 요약

- `gateway/item_open_table.py`(새): 플래그 `HC_ITEM_OPEN_TABLE`(1/true/yes/on만 켬), 손잡이 `HC_ITEM_OPEN_TABLE_KNOBS` > `HC_ITEM_OPEN_TABLE_CONFIG`(기본 `/app/policies/item-open-table.json`) > 기본값, 저장소 `HC_ITEM_ROLES_DB`(기본 `/app/policies/item-roles.sqlite`)를 recall마다 `file:...?mode=ro`로 새로 연다.
- 키 상태 `role_states`: 기준 시각(요청 `query_timestamp` 그 시각, 없으면 지금) 이하 `mentioned_at`, state=valid 기억의 마지막 비참조 언급 역할. 같은 시각은 열림. 참조만이면 미정(표 밖).
- 응답 반영: recall payload 최상위 키 `gateway_item_open_table` = `{t, fmt, rows, truncated, text}`를 `structuredContent.result`·`content[].text` 두 JSON 표면에 넣는다(`item_status.rewrite_tool_result`). `rows`의 날짜는 fmt와 무관하게 늘 싣는다.
- `gateway/app.py`: S1 블록 바로 뒤에 같은 조건(recall, 2xx, buffered)으로 S3 블록. `docker-compose.yml` gateway environment 4줄(기본 꺼짐), `tests/conftest.py` 기본 꺼짐 목록 1줄.
- 별칭 sha256(#376과 맞춘 계약): 소문자 정규화한 뱅크 `item_repo_aliases`의 `json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"))` sha256. meta와 다르면 감사 줄 `aliases_mismatch: true`(경고만).
- fail-open 사유: `no-store`, `open-failed:<예외>`, `schema-mismatch`, `timeout`, `failed:<예외>`. 붙이면 `attached`, 붙일 요소가 없으면 `not-attached`(저장소를 열지 않는다).

## 2. 테스트

- `tests/test_item_open_table.py` 117개(네트워크 0, 임시 sqlite): 꺼짐 7값 x 손잡이 유무 바이트 sha 고정·감사 0·저장소 열기 0, `role_states` 9경우, 손잡이 각각(fmt a/b, header, attach status_query, status_terms, recent_done_days, max_rows, scope 9경우, 잘못된 값), fail-open 6경우(저장소 없음, 데이터베이스 아님, 버전 2, 표 없음, 버전 없음, 시간 초과) + 스레드 상한 + 예외, 읽기 전용 연결 쓰기 불가·recall 뒤 저장소 바이트·디렉터리 불변·`os.replace` 교체 반영, 두 표면 x 응답 모양 3종, 뒤 패스(prune·label·unescape, X1·S1 함께) 키 보존, compose 배선, bench 문자열 없음, 별칭 sha 계약 상수.
- red(수정 전): 모듈 없는 origin/main 트리에서 수집 ImportError, 모듈만 있고 app.py 블록 없는 트리에서 `72 failed, 45 passed`. green `117 passed`.
- CI 동등 pytest: Python 3.11 `8353 passed, 2 skipped`(exit 0), 3.12 `8353 passed, 2 skipped`(exit 0). 기존 `tests/test_item_ledger.py`·`tests/test_item_status.py` 고정 sha 테스트 그대로 통과.

## 3. 사본 확인 (D10, #376 사본 공유)

- 사본: #376이 띄운 hc-next D10 사본(restore D10 일치는 #376 기록). 이 티켓은 up·down 없이 게이트웨이만 워크트리 코드(커밋 2588a94)로 `bash scripts/next-stack.sh rebuild`.
- 이번 사본 측정은 뱅크 한정(final-review #377-1, `HC_ITEM_OPEN_TABLE_BANK`) 수정 전 코드(2588a94)로 했다.
- 저장소: `<state>/policies/item-roles.sqlite` sha256 `dbd67cb741f7b673a313c3ee674ef78cc8ed254751807e259a0a0948e4d67f6d`(전후 동일), schema_version 1, 읽힌 비참조 언급(state=valid) 1167.
- 별칭: S3가 읽은 대상 뱅크 표(99항목) 정규화 sha256 `821e7076049f6b91e7da9ab3d3ce1cf75515b90574af6381a16d386e3870fa90` = meta `aliases_sha256`. 감사 줄 `aliases_mismatch` 0/30.
- 플래그 켜는 방법(실측): `HC_ITEM_OPEN_TABLE=1 bash scripts/next-stack.sh rebuild`(셸 env 보간). final-review #377-1 뒤 코드에서는 뱅크 한정 env도 함께 넣어야 한다: `HC_ITEM_OPEN_TABLE=1 HC_ITEM_OPEN_TABLE_BANK=<bank> bash scripts/next-stack.sh rebuild`(`<bank>` = 저장소가 판정한 뱅크 id, 없으면 감사 사유 `bank-unset`으로 표가 붙지 않는다). `next-stack.sh`는 `<state>/env`에 적힌 키만 셸에서 지우므로, 그 파일에 없는 이 키는 셸 값이 compose 보간에 들어간다. 컨테이너 안 `printenv HC_ITEM_OPEN_TABLE` = `1`. 원복은 플래그 없이 다시 `rebuild`(= `0`). `<state>/env`는 고치지 않았다.
- 질의: 고정 상태 질의 1개(sha256 앞 16 `6bc60199369e0911`), tags `project:hindsight-crew`, `max_tokens=16384`, `query_timestamp` = t 23:59:59 KST, t = 2026-09-22 / 2026-09-28 / 2026-10-04(dev, 봉인 구간 전). arm마다 t 3개 x 10회 = 30회. 원문은 리포 밖 `~/.local/share/hindsight-bench/arch-v2/r5-b4-*.json`(0600)에 수치·sha만.

| t | 결과 행(off/on) | 표 줄(모두 열림) | 표 text 토큰 | payload 토큰 off | payload 토큰 on | 증가 |
|---|---|---|---|---|---|---|
| 2026-09-22 | 78 / 78 | 6 | 174 | 33,238 | 33,553 | +0.95% |
| 2026-09-28 | 78 / 78 | 12 | 246 | 33,241 | 33,736 | +1.49% |
| 2026-10-04 | 78 / 78 | 58 | 798 | 33,244 | 35,119 | +5.64% |

- 토큰: tiktoken cl100k_base, payload 전체 압축 JSON 기준(구조 필드 `rows`와 `text` 둘 다 포함). 표를 뺀 on payload는 off와 토큰 수가 같다(결과 행 불변). 반복 10회 사이 t별 `rows` sha256은 하나씩(결정적).
- 지연(30회, 클라이언트 벽시계): off p50 3.507s / p95 3.600s / 최대 4.138s, on p50 3.534s / p95 3.590s / 최대 3.596s. 게이트웨이 감사 줄의 S3 처리 시간(저장소 열기·계산·재직렬화) 약 18ms.
- 감사 줄: `attached` 30/30, `knobs_source` default, `knobs_sha256` `f7fcf6261d2e6a5cc3ffcd08eaa5906cdbacaca3e9703e7a16266240b5bd5f7b`.
- fail-open: 저장소를 같은 디렉터리 안 다른 이름으로 옮긴 상태에서 t 3개 recall -> 표 0/3, payload 토큰이 off와 같음(33,238 / 33,241 / 33,244), 감사 사유 `no-store` 3/3. 원래 이름으로 되돌린 뒤 sha256 `dbd67cb7...` 동일, 다음 recall 3/3 표 부착.
- 사본 구간(canary-exclude 형식, #376 공유):
  `2026-11-02 04:53:43 2026-11-02 05:19:49  # #376 hc-next D10 사본 공유(S4 백필 + #377 S3 확인 05:09:06~05:14:10, up 04:53:43 ~ down 확인 05:14:49 +300s)`

## 4. 한계

- 응답 토큰 증가는 표 줄 수에 비례한다(t=2026-10-04에서 열린 키 58개, +5.64%). 열린 키가 많은 t와 `table_fmt` b·`recent_done_days`의 토큰은 R5-C(#365)에서 손잡이로 고른다.
- `rows`와 `text`가 같은 정보를 두 번 싣는다(하네스 인식용 구조 필드 + 소비자용 문안). payload 증가분은 표 text 토큰의 약 1.8~2.4배다.
- project 태그 중 glob(`*`, `?`, `[`)이 든 것은 범위로 치지 않는다(전체 범위로 동작).
- 저장소는 한 뱅크 전용이다(meta에 뱅크 식별자가 없다). 요청 뱅크가 `HC_ITEM_OPEN_TABLE_BANK`와 정확히 같을 때만 붙인다(아니면 `bank-unset`/`other-bank`, 원본 바이트).
