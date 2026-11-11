# fixtures 채집 노트

## v1.1.0 재핀 (issue #285)

- 채집 시각: 2026-10-29 (UTC 10:13:42~10:14:33, `probedAt`은 각 REST fixture·`seed_cases.json`에 있음)
- upstream 이미지: `ghcr.io/vectorize-io/hindsight@sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06`
  (ghcr 태그 `1.1.0`의 image index digest. `docker inspect -f '{{.Config.Image}}' hc-next-hindsight-1`로 컨테이너 이미지 확인,
  이미지 라벨 `org.opencontainers.image.version` = `1.1.0`, initialize `serverInfo.version` = `1.1.0`)
- LLM 모드: `--llm opus`. 사본은 hc-next, 복원원은 D10(`backups/keep-arch-v2-20261027-043001/pg0.tar.gz`, sha256 `8a4900ea...613f4`):
  `HC_NEXT_BACKUP_SHA256=<D10 sha256> HC_NEXT_BACKUP_DIR=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001 HC_NEXT_REQUIRED_BANK=<개인 뱅크> bash scripts/next-stack.sh up --llm opus`
- 명령: `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"; bash scripts/mcp-probe.sh`
  (뱅크 `probe-n0`, **upstream 직결**; 게이트웨이 경유 채집 아님). `probe-n0`은 D10 복원으로 같이 온 뱅크다.
- 파일 구성과 시드는 아래 v1.0.0 절과 같다. 바이트가 그대로인 파일: `sync_retain_fail.raw`, `sync_retain_error_frame.raw`(헤더만 바뀜).

### v1.1.0 실측 사실 (v1.0.0 대비, 테스트가 의존하는 것만)

- tools/list는 여전히 **39도구**(추가·삭제 없음). inputSchema 변화:
  - `recall`: `include_chunks`·`include_entities`·`include_source_facts`(전부 `default: false`)와 `max_chunk_tokens`(8192)·`max_entity_tokens`(500)·`max_source_facts_tokens`(4096)가 생겼다(upstream #5678). required는 `["query"]` 그대로.
    셋을 켠 recall 응답(사본 실측, 저장 안 함)의 최상위 키는 `chunks`·`entities`·`results`·`source_facts`·`source_facts_truncated`·`trace`다. recall_prune은 `results` 행만 다듬고 값이 있는 다른 최상위 키는 그대로 둔다(테스트로 고정).
  - `recall`·`reflect`의 `tags_match`가 `enum ["any","all","any_strict","all_strict","exact"]`가 됐다(upstream #5653, 모르는 값은 거부). 게이트웨이가 스스로 보내는 값(트리거 검증 집합 4개 + 기본 `any`)은 전부 enum 안이다(테스트로 고정).
  - `recall.prefer_observations` 기본값이 `false` -> `null`(뱅크 설정 `recall_prefer_observations`로 떨어진다, upstream #5617). 사본에서 그 뱅크 설정은 `false`라 동작은 같다.
  - `update_memory`에 `mentioned_at`, `create_mental_model`·`update_mental_model`·`create_knowledge_page`·`update_knowledge_node`의 `trigger` 설명·필드가 바뀌었다(게이트웨이는 이 인자를 읽지 않는다).
- REST `GET /config`에 `default_observation_scopes`(null)와 `*_use_decision_model` 키가 생겼다. decision model 제공자는 기본 `none`이라(사본 컨테이너 env에 `DECISION_MODEL` 키 0개) 쓰이지 않는다(upstream #5607).
- v1.0.0에서 잰 사실은 v1.1.0에서도 같다: recall은 한글을 `\uXXXX`로 쓰지 않는다(`get_memory`·`reflect`는 쓴다), MCP recall 행은 `fact_type`·REST recall 행은 `type`(29행, `attachments` 전부 null),
  `sync_retain`의 timestamp 오류는 `isError:false` + `structuredContent {"status":"error",...}`, `list_mental_models`의 미선언 인자(`foo`, `detail`)는 `isError:false`로 무시된다. 사본 hindsight `/dev/shm`은 256M(compose `shm_size`).

## v1.0.0 재핀 (issue #247)

- 채집 시각: 2026-10-14 (UTC 14:36:59~14:38, `probedAt`은 각 REST fixture·`seed_cases.json`에 있음)
- upstream 이미지: `ghcr.io/vectorize-io/hindsight@sha256:a88678c038b0e4340822b02a5efc566c56d8176b5bffa731bc433fd37f978e4c`
  (`docker inspect`로 컨테이너 이미지 확인, initialize `serverInfo.version` = `1.0.0`)
- LLM 모드: `--llm opus` (reflect·sync_retain이 LLM을 쓰는 응답을 실제 모양으로 채집). 사본은 hc-next(`bash scripts/next-stack.sh up --llm opus`).
- 명령: `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"; bash scripts/mcp-probe.sh`
  (뱅크 `probe-n0`, 사본 hc-next, **upstream 직결** `docker compose exec hindsight python`; 게이트웨이 경유 채집 아님).
  `probe-n0`은 사본이 운영 백업(`backups/20261006-224558`)에서 복원할 때 같이 온 뱅크다.
  재채집은 같은 뱅크에 시드를 한 번 더 쌓는다(복원 직후 한 번에 채집한 것이 이 파일들).
- 시드 본문과 쿼리의 원문은 `seed_cases.json`(`seedBody`, `retainBody`, `recallQuery`, `reflectQuery`, `badTimestamp`).
  - 시드 retain: `sync_retain`, 태그 `project:hindsight-crew`·`kind:gotcha`, timestamp `2026-07-26T00:00:00Z`, context `probe-seed`.
  - `seedBody` = `[발견] 프로브 시드 - 헤드리스 브라우저를 재시작하면 로그인 쿠키가 사라져 사용자 데이터 디렉터리를 고정해야 세션이 유지된다`
  - `retainBody`(retain_gates 시험의 retain 입력) = `[발견] 프로브 시드 - 헤드리스 브라우저를 다시 띄우면 로그인 쿠키가 사라진다. 사용자 데이터 디렉터리를 고정해야 세션이 유지된다`.
    시드와 **같은** 본문이면 `recurrence.similar_gotchas`가 "같은 기억(본문 동일)"으로 건너뛰므로(복원 뱅크의 `retain_extraction_mode`가 `verbatim`이라 저장 본문이 입력과 같다), 시드를 다시 쓴 꼴의 다른 문장으로 쓴다.
- 파일(전부 `<이름>.raw` = 응답 본문 바이트 그대로, `<이름>.headers` = 상태줄+헤더, Content-Type은 전부 `text/event-stream`):
  - 기존 재핀: `initialize`, `tools_list`(**39도구**, v0.8.3은 32), `sync_retain_ok`, `sync_retain_fail`(검증 오류, isError true - "무변조" 사례로 유지), `retain_async_ok`, REST `memories_post_ok`·`memories_post_fail`·`bank_config`.
  - 추가: `retain_seed_ok`(시드 결과), `recall_ok`(MCP recall), `get_memory_ok`(unescape 시험 원본), `reflect_default`(`include_based_on` 미지정), `reflect_based_on`(`include_based_on: true`),
    `sync_retain_error_frame`(성공형 프레임 안의 저장 실패), REST `recall_gate.json`.
  - `fixtures/rest/recall_gate.json`: `_rest_fixture` 형식에 `contentType`과 원본 본문 바이트 `bodyBase64`를 더했다(재생용).
    요청은 `gateway/context_triggers.py upstream_recall`이 retain_gates를 위해 보내는 그것과 같다: `POST /v1/default/banks/{bank}/memories/recall`, 본문 `{"query": <retainBody>, "max_tokens": 4096}`.
- 지식베이스 도구 7개(`tools_list.raw`에 새로 생김, 브랜치 시작 시점 fixture는 v0.8.3 32도구): `create_knowledge_folder`, `create_knowledge_page`, `delete_knowledge_node`,
  `get_knowledge_base_tree`, `get_knowledge_page`, `search_knowledge_base`, `update_knowledge_node`. 게이트웨이 허용 목록에 없어 `tool-not-whitelisted`로 거부된다(테스트로 고정).

### v1.0.0 실측 사실 (v0.8.3 대비, 테스트가 의존하는 것만)

- **recall은 한글을 `\uXXXX`로 쓰지 않는다**(MCP `recall_ok.raw`는 UTF-8 그대로, 본문은 압축 JSON). 그래서 이슈가 recall로 하려던 unescape 시험은 공허하다.
  `get_memory`(unescape 허용 목록 도구)는 v1.0.0에서도 indent 2 + `ensure_ascii` 기본으로 `\uXXXX`를 쓰므로 `get_memory_ok.raw`를 unescape 원본으로 쓴다(prune 대상 아님).
  `reflect` 응답도 `\uXXXX`다(`reflect_default.raw`, `reflect_based_on.raw`).
- **MCP recall 결과 행에는 `attachments` 키가 없다.** REST recall 행에는 `"attachments": null`이 있다(`recall_gate.json`, 29행 전부 null).
  recall_prune의 기존 제거 규칙(`_empty`: null/빈 dict/빈 list는 제거, 값이 있으면 그대로 보존)을 바꾸지 않았고, 회귀 테스트는 REST 응답에서 null `attachments`가 제거됨을 단언한다. 비어 있지 않은 값의 보존은 합성 입력으로 고정한다.
- 행 키 이름이 표면마다 다르다: MCP recall은 `fact_type`, REST recall과 `get_memory`는 `type`. (게이트웨이 `recurrence.needs_source_lookup`은 `fact_type`을 읽는다 - 범위 밖 관찰, 이 티켓은 건드리지 않는다.)
- 성공형 프레임 안의 저장 실패는 **재현된다**: `sync_retain`에 `timestamp="not-a-timestamp"`를 주면 `isError:false` + `structuredContent {"status":"error","message":"Invalid timestamp format ..."}`
  (`sync_retain_error_frame.raw`; 원인은 upstream `mcp_tools.py`가 검증 오류와 예외를 `return {"status": "error", ...}`로 돌려주는 것). 같은 모양의 실사고가 이 채집 중에도 한 번 났다: 새로 만든 빈 뱅크에서 첫 retain이
  `could not resize shared memory segment ... No space left on device`(hindsight 컨테이너 `/dev/shm` 64MiB, 운영과 같은 값)를 같은 프레임으로 돌려줬다(이 응답은 저장하지 않았다).
  이 모양은 기존 `honesty.looks_like_failure`가 못 잡았다(structuredContent가 dict면 휴리스틱 생략) - `status == "error"` 구조 신호 1개를 추가해 고쳤다(`gateway/honesty.py`).
- `list_mental_models`는 `detail` 인자를 선언하지 않는다(inputSchema `additionalProperties: false`)지만, 사본 upstream 직결 실측에서 미선언 인자(`foo`, `detail=content|full`)는 `unexpected_keyword_argument`가 아니라
  `isError:false`로 **조용히 무시**된다. 응답은 항상 metadata 목록(`items`, `total`). 이슈 본문이 말한 에러는 이 이미지의 HTTP 경로에서 재현되지 않았다.
  #24(게이트웨이 `detail=metadata` 주입과 "detail=content를 명시하세요" 안내)는 그래도 무시되는 인자와 틀린 안내라 은퇴시켰다.
- REST 목록 확인(`GET /v1/default/banks/{HC_NEXT_REQUIRED_BANK}/mental-models?detail=content&limit=1000`, 대상 뱅크 `personal-demo`, upstream 직결 읽기 전용, 본문 저장 안 함):
  HTTP 200, 최상위 키 `items,limit,offset,total`, items 8건(비어 있지 않음), 항목 키 `bank_id,content,created_at,id,is_stale,last_memory_seen_at,last_refresh_failed_at,last_refreshed_at,max_tokens,name,reflect_response,source_query,tags,trigger`,
  `trigger` 키에 `refresh_after_consolidation` 포함(8건 중 8건). #243 전제의 v1.0.0 실확인.

---

## 아래는 v0.8.3 최초 채집 기록 (역사, 위 절이 현행 기준)

# N0 형상 diff 노트 - 계획 가정 대비 upstream 실측 (v0.8.3)

채집: `bash scripts/mcp-probe.sh` (upstream 직결, `docker compose exec hindsight curl http://localhost:8888`).
뱅크 `probe-n0`. **재채집은 항상 upstream 직결** - 게이트웨이 경유 채집은 패치된 출력을 다시 baseline으로
핀하게 되어 금지.

## ① retain류 인자 형상: MCP는 flat, REST는 items[] - 계획 가정대로 (양면 매핑 분리 필요)

- MCP `retain`/`sync_retain` `arguments`는 **flat**: `content`(required) / `context` / `timestamp` /
  `tags` / `metadata` / `document_id` / `bank_id` / `strategy`. `additionalProperties:false`.
  → N2는 `params.arguments.*`를 직접 보정하면 된다 (계획 가정 유지).
- REST `POST /v1/{tenant}/banks/{bank}/memories`는 **`{items:[MemoryItem], async:bool, document_tags?}`**.
  → N3은 `body.items[]`를 **원소 단위 순회**로 보정·검증해야 한다. MCP의 단일 인자 코드를 그대로 못 쓴다.
  rules.py는 "단일 item dict"를 받는 순수 함수로 두고 MCP는 1회, REST는 N회 적용하는 형태가 맞다.
- MCP에는 배치 인자가 없다(`items` 미지원). 반대로 REST에는 `bank_id` 인자가 없다(경로가 뱅크를 결정).
- `MemoryItem`에만 있는 필드: `entities`, `observation_scopes`, `update_mode`. MCP에만 있는 필드: `bank_id`.

## ② REST items[]의 tags·timestamp 위치: **최상위**(metadata 하위 아님)

- `items[i].tags` = `string[]`, `items[i].timestamp` = `date-time | string | null`.
  `items[i].metadata`는 별개의 `str -> str` 맵이며 태그·타임스탬프를 담지 않는다.
  → N3의 tags 문법 검증·timestamp autofill은 **item 최상위 키**를 대상으로 한다.
- `document_tags`는 deprecated("Use item-level tags instead") - 규칙 강제 대상에서 제외하되 존재 시 경고 여지.
- **`timestamp="unset"` 특수값이 upstream 공식 계약**임을 실측 확인: MemoryItem.timestamp description과
  422 메시지 본문(`fixtures/rest/memories_post_fail.json`) 양쪽이 'unset'을 광고한다.
  → N2 Adv5(`"unset"`을 부재로 간주하고 autofill)는 upstream이 광고하는 경로를 게이트웨이가 덮는 것이므로,
  `timestamp_autofill=false` 프로필이 유일한 무시각 저장 경로라는 점을 정책 문서에 명시해야 한다.

## ③ SSE 프레이밍: MCP 응답은 `text/event-stream`, 단 **notification 응답만 JSON 202**

- `POST /mcp`(initialize·tools/list·tools/call) 응답: `content-type: text/event-stream`,
  본문은 `event: message\ndata: {...}` 1프레임 + 후행 개행. `x-accel-buffering: no`, `transfer-encoding: chunked`.
- `notifications/initialized`: **HTTP 202 + `content-type: application/json` + 본문 0바이트**.
  → N4 SSE 파서는 "SSE 확정"이되 빈 본문/JSON 단발 경로를 통과시켜야 한다(계획 N4 AC의 Adv3 케이스와 동일 형상).
- 세션: initialize 응답 헤더 `mcp-session-id`, 이후 요청에 `Mcp-Session-Id`로 반향. upstream 뱅크 지정은
  **`X-Bank-Id` 헤더**(게이트웨이의 `/mcp/<bank>/` 경로 형식과 다름).

## ④ N2/N3/N5/S1~S3 매핑에 영향 주는 나머지 차이

- **initialize에 `instructions` 없음**(확인): `result`는 protocolVersion/capabilities/serverInfo뿐.
  → S3 주입 시 병합 대상 부재. capabilities에 `extensions."io.modelcontextprotocol/ui"`가 있음(무관하나 리라이팅 시 보존 필요).
- **tools/list = 32도구**(retain, sync_retain, recall, reflect, ... clear_memories). S1 필터의 기준 집합.
- **inputSchema가 `anyOf` 사용**(timestamp/tags/metadata/document_id/bank_id/strategy 전부 `anyOf[T, null]`).
  → 계획이 재설계로 미룬 "flat 술어(anyOf 금지)" 충돌이 실물로 확인됨. 스키마 패치 항목은 계속 보류.
- **MCP 검증 실패 = `result.isError:true`**(JSON-RPC error 아님), 본문은 pydantic 텍스트
  (`1 validation error for call[sync_retain] ... missing_argument`), `structuredContent` **없음**.
  → N5 구조 판정은 "structuredContent 부재 + isError" 와 "structuredContent.status" 를 모두 봐야 한다.
- **MCP 성공 = `isError:false` + `structuredContent`**: sync_retain `{status:"completed", memory_ids:[...]}`,
  retain(비동기) `{status:"accepted", operation_id:"..."}`. → N5 비동기 안내는 `structuredContent.operation_id` 사용 가능.
- **REST 성공 본문에 id 없음**: `{"success":true,"bank_id":...,"items_count":1,"async":false,"usage":{...}}`.
  MCP와 달리 memory_ids를 안 준다. REST 응답측 비범위(Adv1) 판단의 근거는 `success` 플래그 + 진짜 422로 유지됨.
- **REST 실패 = HTTP 422 `{"detail":[{type,loc,msg,input,ctx}]}`**. 게이트웨이 rule-violation이 `detail`을 쓰면
  충돌하므로 계획대로 `rule_detail` 유지가 맞다.
- **`GET /config`**: `{bank_id, config:{...}, overrides:{}}`. `retain_strategies` **키는 존재하나 값은 `null`**
  (`mcp_enabled_tools`도 `null`). → N1 드리프트 게이트는 truthy 검사가 아니라 **키 존재 + 값 대조**로 짜야 한다.
- **`HINDSIGHT_API_MCP_INSTRUCTIONS` 미설정**(`printenv` rc=1) - `env_mcp_instructions.note.md` 참조. S2 착수 전 실측 필요.
