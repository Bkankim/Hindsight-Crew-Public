# 런 2 T2b: 완료 retain 무효화 훅의 후보 찾기 방식 비교 (issue #321)

상위: #318. 코드는 `gateway/item_invalidate.py`(훅), `gateway/gate_effects.py`(`finalize`의 `invalidations`), 규칙은 `gateway/item_keys.py`(#320, `RULE_VERSION` = `item-keys-v1`).
이 문서에는 memory id·수치·해시만 쓴다(기억·retain 본문, 뱅크 실명, 별칭 표 항목 이름, 토큰 없음. 공개 전환 가드 #4).
읽기 비교 원자료(id만)는 리포 밖 `reports/issue-321-compare.json`(sha256 `4794b6b2563e7e9e6b15feb872f65cf9e1390dc4fb0a18abad553342442b757c`)에 있다.

## 1. 결론

- **기본 찾기 방식 = `list`**(`GET memories/list?q=#<n>&tags=<project 태그>&tags_match=any&state=valid`). 표본 50건에서 최종 대상 재현율 1.0(7/7), 정밀도 1.0(7/7), 지연 중앙값 141 ms. 코드 기본값이 이미 `list`라 확정 때 코드 변경은 없다.
- `recall` 방식(찾기만 recall, 후보마다 `GET memories/{id}`로 state 등을 읽음)은 재현율 0.714(5/7), 정밀도 1.0(5/5), 지연 중앙값 4,426 ms(p95 41,068 ms)였다. 같은 일을 더 느리게, 더 적게 찾는다.
- 플래그는 이 문서의 어떤 단계에서도 운영에 켜지 않았다. 켜는 것은 운영자 승인 뒤 E1' 창이다.

## 2. 사본과 복원

- `restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4` (D10과 같다)
- 기동: `HC_ITEM_INVALIDATE=1 HC_NEXT_BACKUP_SHA256=8a4900e... bash scripts/next-stack.sh up --llm opus --backup <운영 체크아웃>/backups/keep-arch-v2-20261027-043001` (`HC_NEXT_IMAGE` 없음). 출력 `up ok: commit=44241c75150946752db7ce16bf8bb0818cd57bf5`(이 브랜치의 #323 머지 커밋 병합 뒤 HEAD).
- 이미지: v1.1.0 next-stack 기본 이미지 `ghcr.io/vectorize-io/hindsight@sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06`, 이미지 ID `sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06`(`docker image inspect -f '{{.Id}}'`).
- 플래그 확인: `docker exec hc-next-gateway-1 printenv HC_ITEM_INVALIDATE` = `1`(재빌드 불필요, `rebuild` 하지 않음). 정책 파일 수정 뒤 `docker restart hc-next-gateway-1`을 했고 restart는 env를 바꾸지 않으므로 다시 `printenv` = `1`.
- 사본에서 한 일: 읽기(`memories/list`·`memories/recall`·`GET memories/{id}`·`GET tags`), 정책 파일 수정 1회, probe retain 1건(아래 5절, 사본 게이트웨이로만). 운영 8888·운영 뱅크 접근 없음. Hindsight memory MCP 도구 호출 없음.

## 3. 별칭 표

- 파일(리포 밖, 0600): `arch-v2/r2-aliases.json`, sha256 `21d1b75bae4c66f23f28061d6f1e9fae35ea4bfa931e57b63875aa6de606cdf6`. 99개 항목. 구성: 사본 D10 `GET tags`의 `project:` 태그 slug 68개(각각 자기 자신으로), 소유자 GitHub 리포 이름 slug 46개(자기 자신으로, 위와 겹침 16), #320 템플릿 예시 `hc` -> `hindsight-crew`와 `hindsight-crew` -> `hindsight-crew`.
- 넣은 방법: 사본 정책 오버레이 `<사본 상태 디렉터리>/policies/local/bank-policies.json`의 해당 뱅크 항목에 `item_repo_aliases` 키로 병합(수정 전 sha256 `0587ecbb315e5504b019f53daa4a3bc3081478d1d3711e5ebdf1a03735c991ca`, 수정 뒤 `c2116b0b999f886faf7a2e9dbb33dc41b4bdf914ad4e1b2d8fff6b370ed309a5`). 수정 전 이 키가 없었다. `BankPolicies.from_files`로 읽혀 99개가 잡히는 것을 확인했고 게이트웨이를 재시작했다. `down`이 상태 디렉터리째 지웠다. #322는 이 JSON 파일을 같은 방식으로 쓴다.

## 4. 읽기 비교

- 전수 나열: `memories/list?state=valid` 페이지 끝까지 7,559행(보고 total 7,559). `kind:completion`이고 world·experience이며 `extract_keys`가 키를 하나 이상 내는 원문 254건.
- 표본: 254건 중 50건, `random.Random(321).sample`(id 오름차순 목록에서). 정답 = 전수 행에 대한 `item_keys.find_targets`(로컬 매칭, 완료 자기 자신 제외). 정답 대상은 5개 완료에 모두 7건이고 `over_cap`인 완료는 0건이다.
- 두 방식은 같은 코드(`_lookup_list`, `_lookup_recall`)를 호출해 같은 `find_targets`로 판정했다. 실패한 완료는 두 방식 모두 0건.
- 후보 집합 지표(`candidate_*`)는 찾기 단계만의 값이고, 최종 대상 지표(`target_*`)는 `is_target` 뒤 값이다.

| 방식 | 최종 대상 재현율 | 최종 대상 정밀도 | 후보 재현율 | 후보 정밀도 | 후보 수 중앙값 | 지연 중앙값(ms) | 지연 p95(ms) | 찾기 지연 중앙값(ms) | 채우기(GET) 지연 중앙값(ms) |
|---|---|---|---|---|---|---|---|---|---|
| list | 1.0 (7/7) | 1.0 (7/7) | 1.0 | 0.0025 | 40.5 | 141.2 | 984.8 | 139.8 | 0 (필요 없음) |
| recall | 0.714 (5/7) | 1.0 (5/5) | 0.714 | 0.0052 | 17.5 | 4,425.9 | 41,067.9 | 4,392.4 | 41.1 |

- 후보 정밀도가 낮은 것은 방식의 결함이 아니라 설계다. `list`의 `q=#<n>`은 부분 일치라 `#25`가 `#251`에도 걸리고, 거르는 일은 `is_target`이 한다.
- `recall`이 놓친 대상 2건은 각각 recall 적중이 57건, 83건이라 `MAX_HYDRATE`(30건) 밖으로 밀린 완료에서 나왔다. 상한을 올리면 재현율이 오를 수 있지만 채우기 GET이 후보 수만큼 늘고 찾기 지연(중앙 4.4초)은 그대로다.
- 한계: 정답 대상이 7건뿐이라 재현율·정밀도의 신뢰 구간은 넓다. 이 비교는 방식 사이의 순서(list가 recall보다 재현율이 같거나 높고 수십 배 빠르다)와 `list`의 누락 없음(키가 본문에 있을 때)을 보이는 용도이고 운영 정밀도의 보증이 아니다.

## 5. 종단 확인

- probe: 본문 키가 있는 `kind:completion` retain 1건(project 태그 1개, `item:` 태그 없음, 키 `<repo>#62`)을 사본 게이트웨이 컨테이너 안에서 자기 8888(사본 게이트웨이 프로세스)로 REST `POST .../memories`로 보냈다. 토큰은 사본 ACL 파일에서 읽어 출력하지 않았다. 대상 후보는 D10에서 같은 키와 같은 project 태그를 가진 열림 원문 1건으로 미리 골랐다(`find_targets`가 정확히 1건을 내는 것).
- retain 응답: 상태 200, 3,791 ms(동기 저장), 응답 키 `async, bank_id, items_count, operation_id, operation_ids, success, usage`(훅이 없던 때와 같다).
- 무효화: 대상 id `fc279a72-2ffb-48ac-a3ec-7a786f7118f6` 1건. `memories/list?state=invalidated` total 19 -> 20, 새로 생긴 id는 그 하나. 요청 시작부터 확인까지 8.8초(폴링 간격 5초 포함). `GET memories/{id}`의 `state` = `invalidated`, `invalidation_reason` = `item-keys-v1 completion=unknown`.
- 감사 줄(`decision` = `item-invalidate`, `reason` = `invalidated`): 공통 필드 `ts, token_fp, identity, method, tool, attempted_bank, resolved_bank` + `item_invalidate{ candidates: 5, elapsed_ms: 88, hydrate_status: [], invalidated_ids: [<대상 id>], keys: ["<repo>#62"], lookup: "list", lookup_status: [200], operation_id: [], over_cap: false, patch_status: {<대상 id>: 200}, rule_version: "item-keys-v1", target_ids: [<대상 id>], transport: "rest" }`. 본문 필드 없음.
- 사본 `down` 뒤 `status` exit 3, 복제본 정리 4종 빈 출력.

## 6. 응답 모양 확인과 발견

- `memories/list` 항목: `fact_type`, `state`, `tags`, `mentioned_at`, `document_id`, `text`가 모두 있어 그대로 `is_target`에 넘길 수 있다(v1.1.0 openapi `MemoryUnitListItem`과 사본 실측이 같다).
- recall 행 키: `attachments, chunk_id, context, document_id, entities, id, mentioned_at, metadata, occurred_end, occurred_start, scores, source_fact_ids, tags, text, type`. **`state` 없음, `fact_type` 대신 `type`**. 그래서 recall 방식은 코디네이터 결정대로 후보마다 `GET memories/{id}`로 읽는다(`state`를 지어 넣지 않는다).
- `GET memories/{id}` 키: `chunk_id, consolidated_at, consolidation_failed_at, context, date, document_id, edited_at, entities, id, invalidated_at, invalidation_reason, mentioned_at, metadata, observation_scopes, occurred_end, occurred_start, state, tags, text, type`. `state`·`tags`·`mentioned_at`·`document_id`는 있고 **`fact_type`이 아니라 `type`**이다. `item_keys._fact_type`이 `fact_type`·`type`을 둘 다 읽어서 문제없다.
- 무효화 `reason`의 `completion=` 값은 문서 id가 없는 동기 retain에서 `unknown`이다(REST 동기 응답에 operation id·문서 id가 없고, 요청에 `document_id`를 주지 않으면 게이트웨이가 알 수 없다). 감사 줄에는 `operation_id: []`로 남는다. 완료와 무효화의 연결은 감사 줄 시각과 `target_ids`로 한다. 비동기 retain은 `completion=<operation id>`가 들어간다(mock 테스트로 고정).
- list 방식의 `item:` 태그 보완(final-review 반영, **사본 미측정**): 키가 `item:<repo>-<n>` 태그로만 있고 본문에 `#<n>`이 없는 후보는 `q`가 못 찾으므로, 키마다 (repo 별칭 표기 x project 태그)별 `tags=[item:<표기>-<n>, <project>]&tags_match=all_strict&state=valid` 조회를 더해 합집합으로 판정한다. 사본 정책이 `item:` 태그를 거부해 D10에는 그런 기억이 없어 이 비교(4절 수치)에는 들어 있지 않고, 4절의 list 지연은 `q` 조회만의 값이다(호출은 키마다 늘어난다). 동작은 mock 테스트로만 고정했다.
- recall 방식 상한 변경(같은 반영): 위 4절 수치는 적중이 `MAX_HYDRATE`(30건)를 넘으면 앞 30건만 채우던 구현으로 쟀다. 지금 코드는 넘으면 잘린 목록으로 판정하지 않고 `skip:hydrate-cap`으로 아무것도 하지 않는다(대상 0건, 사본 미재측정). 4절의 recall 놓친 2건은 이 경우에 해당한다.
- project 범위 변경(final-review 반영, 사본 미재측정): 4절 수치는 본문 조회를 `tags_match=any`로 쟀고 지금 코드는 `any_strict`다(`any`는 태그 없는 행까지 포함). `is_target`이 같은 project 태그 공유를 요구하므로 최종 대상은 같고 후보 수만 준다.

## 7. 사본 구간(canary-exclude 형식)

`2026-10-31 08:37:09 2026-10-31 08:54:03  # #321 v1.1.0 D10 hc-next 사본(up 08:37:09 ~ down 확인 08:49:03 +300s)`
