# 멘탈 모델 서버 cron 갱신 사실 메모 (upstream v1.1.0)

이슈 #428, #251 전환 준비.
#251 본문의 upstream 사실은 v1.0.0(2026-10-14) 소스 기준이었다.
이 메모는 운영 이미지 v1.1.0을 다시 읽어 확정한 것이다.

## 출처와 읽은 방법

- 이미지: `ghcr.io/vectorize-io/hindsight@sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06`(이미지 ID `50311d02b4d5`).
  `fixtures/mcp/NOTES.md`의 v1.1.0 재핀(#285)과 같은 digest다.
- 버전: `/app/api/pyproject.toml:7` `version = "1.1.0"`.
- 읽은 방법(2026-11-09, 읽기 전용): `docker exec hindsight-crew-hindsight-1 grep -nF ... <파일>`, `sed -n <범위>p <파일>`.
  운영 컨테이너 재기동·쓰기 없음.
  운영 API 라이브 호출 없음.
- 아래 경로는 컨테이너 안 `/app/api/hindsight_api/` 기준 상대 경로다.
  줄 번호는 이 이미지에서만 맞다.
  이미지를 바꾸면 다시 확인한다.

## 요약

1. 성공한 갱신은 `last_refreshed_at`을 지금으로, `last_memory_seen_at`을 그 갱신이 읽은 최신 기억으로 옮기고 `last_refresh_failed_at`을 NULL로 지운다.
   실패한 갱신은 앞의 둘을 그대로 두고, 일시 오류가 아닐 때만 `last_refresh_failed_at`을 찍는다.
2. `last_memory_seen_at`은 "범위 안 최신 기억 시각"이 아니다.
   마지막 성공 갱신이 읽은 데이터 워터마크라서 성공 갱신 때만 움직인다.
   새 기억이 있는지는 `is_stale`(범위 안 기억의 `updated_at` > 워터마크)로 본다.
3. cron은 UTC로 해석한다.
   매일 03:30 KST는 `30 18 * * *`이다.
4. 서버 cron 경로도 `HINDSIGHT_API_REFLECT_WALL_TIMEOUT`(`reflect_wall_timeout`)을 그대로 쓴다.
5. 일시 오류(429·5xx·연결·타임아웃, reflect wall-clock 초과 포함)는 `last_refresh_failed_at`을 남기지 않는다.
   그래서 실패 필드만으로는 이 실패를 볼 수 없다.

## 항목별 사실

### 1. 성공·실패 뒤 시각 필드 갱신 규칙

| 필드 | 성공 | 실패 | 근거 |
|---|---|---|---|
| `last_refreshed_at` | `NOW()`(내용을 바꾸지 않은 delta 완료, 손으로 고친 content도 포함) | 그대로 | `engine/memory_engine.py:19551`, 주석 19540~19549 |
| `last_refresh_failed_at` | `NULL` | 일시 오류가 아니면 `now()`, 일시 오류면 그대로 | 성공 `engine/memory_engine.py:19555`, 실패 스탬프 `engine/memory_engine.py:19776`(`if pause_automatic:` 아래) |
| `last_memory_seen_at` | 그 갱신이 본 범위 안 최신 기억으로 올림(뒤로 가지 않음) | 그대로 | `engine/memory_engine.py:19563`, 실패 시 보존 `engine/memory_engine.py:19101`·`19104`(`_preserve_and_fail`) |
| `last_full_refreshed_at` | full 모드 완료 때 `NOW()` | 그대로 | `engine/memory_engine.py:19557` |

- 일시 오류 판정: `_handle_refresh_mental_model`의 일반 예외 경로가 `pause_automatic=not is_transient_remote_error(e)`로 기록한다(`engine/memory_engine.py:4937`).
  `TimeoutError`·`ConnectionError`는 일시 오류다(`engine/remote_retry.py:147`).
  reflect wall-clock 초과는 `TimeoutError`로 올라온다(`engine/memory_engine.py:16205` `timeout=wall_timeout`, `16226` `raise TimeoutError(`).
  따라서 timeout으로 실패한 갱신은 실패 필드를 남기지 않는다.
- 워커 wall-clock 상한으로 끊긴 갱신은 `on_task_wall_timeout`이 기록하고, 재시도 대기 중 끊긴 경우(`stage`가 `.backoff`로 끝남)만 스탬프를 생략한다(`engine/memory_engine.py:5237`).
- `clear`(`POST .../clear`)도 `last_refresh_failed_at = NULL`을 쓴다(`engine/memory_engine.py:19867`).
  `last_refreshed_at`은 건드리지 않는다.
- 응답 노출: 세 필드는 `detail=metadata`를 포함한 모든 상세 수준에 실린다(`engine/memory_engine.py:21326`, metadata 분기 `21338`).

### 2. refresh_cron 설정 경로

- REST: `PATCH /v1/default/banks/{bank}/mental-models/{id}` 본문 `{"trigger": {...}}`(`api/http.py:7428`).
  API가 보낸 필드만 넘기고(`api/http.py:7449` `model_dump(exclude_unset=True)`), 엔진이 저장된 trigger 위에 병합한다(`engine/memory_engine.py` `_merge_trigger`, 병합 줄 `merged = {**(...base), **supplied}` 바로 아래 `20002`).
  즉 trigger는 PATCH다.
  빠진 필드는 현재 값을 유지한다.
- 상호 배타: `refresh_after_consolidation`과 `refresh_cron`을 함께 참으로 보내면 거부한다(`api/http.py:3504`).
  `refresh_cron`만 보내면 저장된 `refresh_after_consolidation` 키를 뺀다(`engine/memory_engine.py:20002`).
  그래서 `scripts/model_cron.py`는 적용 패치에 `mode`와 `refresh_after_consolidation: false`를 함께 적는다.
- `mode` 기본값은 `"full"`이다(`api/http.py:3311`).
- MCP: `update_mental_model`의 `trigger` 인자도 PATCH다(`mcp_tools.py:2205` 정의, 설명 `mcp_tools.py:2228~2232`, 필드 `mcp_tools.py:120`, 상호 배타 검사 `mcp_tools.py:297`).
  리포 사본 `fixtures/mcp/tools_list.raw`(같은 digest)의 설명도 같다.
- 게이트웨이: REST 프록시는 `PATCH mental-models/<id>`를 MCP `update_mental_model`과 같은 판정으로 본다. 그래서 개인 뱅크 소유자에게만 허용한다(`gateway/policy.py` `REST_OPERATIONS`·`decide_rest`, 팀 뱅크 거부 사유 `corrective-on-nonpersonal:update_mental_model`).
  `scripts/model_cron.py`는 REST 경로를 쓴다.

### 3. stale 판정

- 기준 시각은 `last_memory_seen_at`이고, 없으면 `last_refreshed_at`이다(`engine/memory_engine.py:2109`).
  둘 다 없으면 stale로 본다(`engine/memory_engine.py:21063` `compute_mental_model_is_stale`, `scope is None` 분기 `21086`).
- 범위 안 기억의 `updated_at`이 기준 시각보다 늦으면 stale이다(`engine/memories/pg/reads.py:559` `updated_at > $2`).
- 범위: 모델 tags에 trigger의 `tags_match`(없으면 tags가 있을 때 `all_strict`, 없으면 `any`, `engine/memory_engine.py:1941`)를 strict로 바꿔 적용한다(`engine/memory_engine.py:2139`).
  `trigger.fact_types`가 있으면 그 종류만 본다.
  `fact_types`가 없으면 종류 조건이 없다(`engine/memories/pg/reads.py:577~579`, 대상 테이블 `memory_units` `591`).
  `memories/list`도 `type`을 줄 때만 종류 조건을 걸고(`api/http.py:6063` -> `engine/memories/pg/curation.py:121`) 같은 `memory_units`를 읽는다(`curation.py:109`, 기본 state).
  그래서 감시의 기억 수 범위는 upstream stale 범위와 종류 면에서 같다(#428 final-review C3).
  `tag_groups`가 있으면 그것이 tags를 대신한다.
- 목록과 단건 응답의 `is_stale`이 같은 판정이다(`api/http.py:7158` `with_staleness=True`).
- cron 회차는 stale일 때만 갱신을 제출한다(`engine/maintenance.py:648`).
- 기억 삭제는 stale을 만들지 않는다(`api/http.py` `MentalModelResponse.is_stale` 설명).

### 4. 실행 중 상태 표시

- 모델 응답에는 "갱신 중" 필드가 없다(`api/http.py:3524`~`3601` `MentalModelResponse`).
- 실행 중 여부는 operations 목록으로 본다: `GET /v1/default/banks/{bank}/operations?type=refresh_mental_model&status=pending|processing`(`api/http.py:8467`, `limit` 최대 100 `api/http.py:8484`).
  항목에 `mental_model_id`가 실린다(`engine/memory_engine.py:22022`).
- cron 발견 루틴은 `pending`·`processing` 갱신이 있는 모델을 뺀다(`alembic/versions/d4f7a2c9e1b5_scheduled_full_refresh.py:118`, 처음 정의 `alembic/versions/f4d1c2b3a5e6_add_scheduled_mental_model_refresh_routine.py:84`).
  제출도 `skip_if_in_flight=True`라 중복 제출이 없다(`engine/maintenance.py:662`).
- min interval로 미뤄진 자동 갱신은 `pending`으로 남는다(`engine/memory_engine.py:4776` `_defer_refresh_within_min_interval`).
  기본 min interval은 0이다(`config.py:1898`).

### 5. 실패 시 동작·재개

- 자동 경로(`refresh_after_consolidation`, `refresh_cron`, `full_refresh_cron`)는 `automatic=True`로 제출한다(`engine/maintenance.py:663`).
  `last_refresh_failed_at`이 `last_refreshed_at`보다 늦으면(또는 성공 이력이 없으면) 제출하지 않고 `paused`를 돌려준다(`engine/memory_engine.py:24146`, 판정 `24278`).
  cron 루프는 이것을 `skipped_paused`로 센다(`engine/maintenance.py:667`).
- 재개: 명시 갱신(API·MCP·control plane)이 성공해야 풀린다.
  성공하면 `last_refresh_failed_at`이 NULL이 되기 때문이다(`engine/memory_engine.py:19555`, `_automatic_refresh_paused` 설명 `24256`).
  `clear`도 실패 필드를 지운다(`19867`).
- 일시 오류는 pause하지 않는다(1절).
  그런 실패 뒤에는 `last_refreshed_at`이 회차보다 이른 채로 남아 다음 tick(기본 300초, `config.py:2197`)에도 다시 기한이 차고, stale이면 다시 제출된다(`engine/maintenance.py:587`·`595`).
  즉 일시 오류가 계속되면 tick마다 재시도한다.
- 워커 재시도: 일시 오류는 `worker_max_retries`까지 재시도한다(`engine/memory_engine.py:5200`).

### 6. cron 시간대

- UTC다.
  `now = datetime.now(timezone.utc)`(`engine/maintenance.py:574`)에서 `croniter(cron, now).get_prev(datetime)`(`engine/maintenance.py:580`)로 직전 경계를 구한다.
- 필드 설명도 "Cron expression (UTC, standard 5-field syntax ...)"이다(`api/http.py:3328`).
- 기한 판정: 직전 경계가 `last_refreshed_at`보다 늦으면 기한이 찬 것이다(`engine/maintenance.py:587`, 적용 `595`).
- 해상도: 루프 tick이 기본 300초라(`config.py:2197`) 실제 제출은 경계 뒤 최대 tick 간격(+ 시작 jitter)만큼 늦다.
- 운영 매일 03:30 KST = `30 18 * * *`.

### 7. wall-clock 상한 적용 여부

- 적용된다.
  cron 회차도 같은 `refresh_mental_model` 작업이다.
  이 작업은 `_handle_refresh_mental_model`(`engine/memory_engine.py:4839`)에서 `refresh_mental_model`(`4894`)과 `_execute_mental_model_refresh`(`19006`)를 거쳐 `reflect_async`(`18312`)로 간다.
  `reflect_async`는 `wall_timeout = config.reflect_wall_timeout`(`engine/memory_engine.py:15928`)으로 감싼다(`16205`).
- 워커 바깥 상한도 같은 값이다(`worker/poller.py:111` `"refresh_mental_model": _WallCeiling(config_attr="reflect_wall_timeout", ...)`).
- 값의 출처는 `HINDSIGHT_API_REFLECT_WALL_TIMEOUT`(`config.py:5584`, 기본 300 `config.py:2101`)이다.
  뱅크별 설정이 아니라 전역 `get_config()`다.
  따라서 #272에서 올린 값이 cron 경로에도 그대로 적용된다.
- 운영 값 자체는 이 런에서 읽지 않았다(`.env.local` 열람 금지).

### 8. 목록 API 페이지 처리

- `GET /v1/default/banks/{bank}/mental-models`(`api/http.py:7116`): `limit` 기본 100·최대 1000(`api/http.py:7128`), `offset`.
- 응답 `total`은 필터에 맞는 전체 수다(`api/http.py:3606`).
- `trigger`는 `detail=content` 이상에서만 실린다(기본 `metadata`, `api/http.py:7123`·`engine/memory_engine.py:21338`).
- 정렬이 `last_refreshed_at DESC, id DESC`라(`engine/memory_engine.py:17283`) 페이지 사이에 갱신이 끝나면 행이 밀릴 수 있다.
  그래서 도구는 페이지를 `total`까지 읽고, 고유 id 수가 `total`과 다르거나 중복이 있거나 `total`이 바뀌면 실패로 본다.
- operations 목록은 `limit` 최대 100이다(`api/http.py:8484`).
- `memories/list`는 `time_field=updated_at`과 `start_date`(포함)·`end_date`(제외)를 받는다(`api/http.py:6019`·`6027`·`6028`, 값 목록 `engine/time_filter.py:39`).
  감시는 이것으로 "워터마크 뒤, 회차 앞" 범위 기억 수를 센다.

## 이슈 전제 보정

- 이슈 #428 목표 3의 "새 기억(`last_memory_seen_at` > 마지막 성공 갱신)"은 성립하지 않는다.
  `last_memory_seen_at`은 성공 갱신 때만 움직이는 워터마크다(1절, 3절).
  감시는 대신 "마감 회차 이후 성공 갱신이 없고(`last_refreshed_at` < 회차), `is_stale`이고, 워터마크 뒤에 쓰인 범위 안 기억 중 마감 회차보다 앞선 것이 있음"을 쓴다.
  워터마크는 `last_memory_seen_at`, null이면 `last_refreshed_at`이다(upstream stale 기준, 3절).
  마감 회차는 `지금 - 유예` 이전의 마지막 cron 경계다.
  회차 뒤 기억만 있으면 정상이다.
  회차 앞 기억이 남아 있으면 회차 뒤 기억이 함께 있어도 알린다.
- 실패 필드는 복구(성공) 뒤 NULL로 지워진다.
  그래서 감시는 "값이 있으면 미해결"로 판정한다.
- 일시 오류와 timeout 실패는 실패 필드를 남기지 않는다.
  cron 모델은 위 미갱신 조건이 이것을 잡는다.
  cron 없는 모델(전환 전 launchd 드라이버)은 `refresh.log`의 `MODEL_REFRESH_FAIL`이 정본이다.

## 감시·도구가 감당하지 않는 범위

- `tag_groups`나 `fact_types` 2개 이상인 범위는 `memories/list` 한 번으로 표현할 수 없다.
  감시는 이것을 관측 실패로 알린다.
- `scripts/model_cron.py`와 감시의 cron 해석기는 숫자·`*`·`,`·`-`·`/`만 받는다.
  날짜(DOM)와 요일(DOW)을 동시에 제한한 식은 거부한다(구현마다 OR 규칙이 달라서).
- launchd `StartCalendarInterval`은 호스트 시간대(운영 Asia/Seoul)로 돈다.
  감시 잡의 09:30은 KST다.
