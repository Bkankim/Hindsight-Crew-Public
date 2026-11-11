"""S3 열린 키 표 (issue #377, 아키텍처 v2 런 5 R5-B4, 기능 플래그 `HC_ITEM_OPEN_TABLE` 기본 꺼짐).

왜: S1(`item_ledger`)은 "완료 계열 기억이 있으면 끝남" 규칙으로 행마다 주석을 단다. 이 규칙은 열린 일을 끝남으로
잘못 보고(#362 대조 80쌍 중 79쌍), 행별 주석은 정밀도를 올리지 않고 토큰만 늘렸다(#374). S3 는 S4(#376)가 저장 시점에
기억마다 판정해 둔 키 언급 역할(done/open/ref)만으로 키 상태를 결정적으로 계산하고, recall 응답에 "열린 키만 담은 표"
하나를 붙인다. 게이트웨이 읽기 경로에 LLM·upstream 호출이 없다(D8). R7-2(#396)부터 사이드카(#395)가 저장소에 실어 둔
GitHub 근거가 있으면 표를 3상태로 만든다(아래 "3상태 표", GitHub 를 직접 부르지 않는다).

무엇을(`HC_ITEM_OPEN_TABLE=1` 일 때만, S1·X1 과 독립): 게이트웨이 MCP `tools/call` `recall` 의 **성공 응답**마다
  1. 붙일 recall 요소를 고른다(손잡이 `attach`, 기본 status_query: 질의에 상태 어휘가 없으면 `no-status-term`). 범위(손잡이
     `scope`)를 정한다(기본 request_or_result_project: 결과 행에 키가 없으면 `no-result-keys`, 둘 다 저장소를 열지 않는다).
     기준 시각 t = 요청 `query_timestamp` 그 시각(없으면 지금, S1 과 같은
     규칙, 읽을 수 없으면 그 요소는 건드리지 않는다).
  2. 사이드 저장소(env `HC_ITEM_ROLES_DB`, 기본 `/app/policies/item-roles.sqlite`)를 recall 마다 `file:...?mode=ro` 로 새로
     연다(S4 는 새 파일을 써서 `os.replace` 로 바꿔 넣는다. 쓰기 0, `-wal`/`-shm` 없음).
  3. 키 상태(`role_states`): t 이하 `mentioned_at` 이고 state=valid 인 기억 중 그 키의 마지막 비참조(done/open) 언급의
     역할. 같은 `mentioned_at` 에서 갈리면 open. 비참조 언급이 없으면 미정(표에 넣지 않는다). 날짜 = 그 언급의 KST 날짜.
     (#372 의 `role_states` 와 같은 규칙. 그 모듈은 import 하지 않는다: 게이트웨이 이미지는 `gateway/` 만 담는다.)
  4. 범위 안 키 중 열림 키(+ 손잡이 `recent_done_days` 가 N>0 이면 끝남 날짜가 t-(N-1) 이상인 끝남 키)를 표로 만든다.
  5. recall payload(JSON 객체) 최상위 키 `gateway_item_open_table` = `{t, fmt, keys, truncated, text}` 로 넣는다
     (R6-1 #387: 런 5 의 `rows`(키·상태·날짜)는 `text` 와 같은 정보를 두 번 실어 뺐다. `build_table` 반환은 `rows` 그대로).
     `item_status.rewrite_tool_result` 로 `structuredContent.result`·`content[].text` 두 JSON 표면에 같이 들어간다.
     JSON 밖에 원문을 덧붙이지 않는다(뒤 패스 prune·label·unescape 는 JSON 만 다루고 이 키를 지우거나 바꾸지 않는다).
     R5-C 하네스는 이 구조 필드로만 표를 인식한다(문안 손잡이가 바뀌어도 인식 불변).
       * `t`: 기준일(KST 날짜 `YYYY-MM-DD`), `fmt`: `a`/`b`, `truncated`: 잘린 키 수(정수),
       * `keys`: `["<repo>#<n>", ...]`(열린 키만, 표 줄과 같은 순서). 상태·날짜와 최근 끝남 키는 `text` 만 담는다.
       * `text`: `<item_open_table>` + 줄바꿈 + 렌더한 header + 줄바꿈 + 행들(+ 잘림 줄) + 줄바꿈 + `</item_open_table>`.
  6. 실패는 fail-open: 저장소 없음(`no-store`), 열기 실패(`open-failed:<예외 이름>`), 스키마 버전 불일치
     (`schema-mismatch`), 시간 초과(`timeout`), 그 밖(`failed:<예외 이름>`)이면 표를 붙이지 않고 받은 바이트를 그대로
     돌려준다. 감사 줄(decision `item-open-table`)에 사유 한 줄. 붙이면 `attached`(+ `scope_from`: 범위 출처별 요소 수),
     붙일 요소가 없으면 `not-attached`(+ `not_attached`: `no-status-term` | `no-result-keys`, 요소가 여럿이면 사유별 개수,
     저장소를 열지 않는다). 본문·키 목록은 남기지 않는다(수·해시만).

처리 순서(app.py): X1 -> S1 -> **S3(이 모듈)** -> honesty/prune/label/unescape 합성 패스. 도달 범위는 MCP recall 만
(REST recall·reflect·지식 페이지 밖).

손잡이 전달(S1 과 같은 우선순위·검증·감사 방식)
----------------------------------------------
* `HC_ITEM_OPEN_TABLE_KNOBS`(env 인라인 JSON 객체, 공백뿐이면 없음) > `HC_ITEM_OPEN_TABLE_CONFIG` 파일(기본
  `/app/policies/item-open-table.json`, 정책 바인드 마운트 안, 리포에는 두지 않는다) > 기본값. 병합 없음.
* 파일은 (경로, mtime_ns, 크기, ctime_ns, inode)가 바뀌었을 때만 다시 읽는다(재시작 없이 반영).
* 잘못된 설정: JSON 아님/객체 아님이면 전부 기본값, 허용값 밖 키는 그 키만 기본값, 모르는 키는 무시. 경고 코드는 로그
  1회와 감사 줄 `knob_warnings`. 감사 줄에 적용 값의 `knobs_sha256`(키 정렬·압축 JSON)과 `knobs_source`.

| 키 | 기본값 | 허용값 | 뜻 |
|---|---|---|---|
| `table_fmt` | `"a"` | `a` / `b` | a = `키 | 상태`, b = `키 | 상태 | 날짜` |
| `header` | `HEADER_DEFAULT` | 1~400자, 자리표시 `{t}` `{cols}` 만(`{{` `}}` 는 중괄호) | 표 머리 문안(R6-2 머리는 `HEADER_R6_1`) |
| `attach` | `"status_query"` | `always` / `status_query` | status_query = recall `query` 에 `status_terms` 중 하나가 들어 있을 때만(대소문자 무시). 요청 `tags` 는 판별에 쓰지 않는다(범위만) |
| `status_terms` | `STATUS_TERMS_DEFAULT` | 문자열 1~40개(각 1~40자, 공백만 아님) | status_query 판별 낱말(런 5 목록은 `STATUS_TERMS_V1`) |
| `recent_done_days` | `0` | 정수 0~30 | N>0 이면 끝남 날짜 >= t-(N-1) 인 키도 `끝남` 으로 넣는다 |
| `max_rows` | `300` | 정수 1~1000 | 넘으면 날짜 최신순으로 남기고 잘린 수를 표 끝 한 줄에 |
| `scope` | `"request_or_result_project"` | `request_or_result_project` / `request_project` / `all` | 요청 `tags` 의 `project:<slug>`(뱅크 `item_repo_aliases` 로 정규화)와 같은 repo 의 키만. project 태그가 없으면: request_or_result_project 는 recall 결과 행에서 `item_keys.extract_keys` 로 나온 키의 repo(키가 없으면 붙이지 않음), request_project 는 all |
| `timeout_ms` | `1500` | 정수 100~10000 | 저장소 읽기 벽시계 상한 |
| `github_evidence` | (없음 = `use`) | `use` / `ignore` | #396 선택 키(`DEFAULTS` 밖, 값이 없으면 정규 sha256 에 안 들어감). ignore = 근거 테이블이 있어도 테이블 없는 표(절제 비교용) |
| `done_closed_rows` | (없음 = `result`) | `result` / `off` | #402 선택 키(`DEFAULTS` 밖). result = 3상태 모드에서 결과 행 키 중 뱅크 끝남 + GitHub 종결 근거인 키를 `확인된 종결` 반증 줄로 넣는다 |
| `github_max_age_hours` | (없음 = `2`) | 정수 0~8760 | #402 선택 키(`DEFAULTS` 밖). 3상태 모드에서 리포 근거가 읽는 시각(벽시계)보다 이만큼 넘게 오래되면 그 리포 줄은 모두 `미확인·충돌` stale. 0 = 검사 안 함 |
| `over_max_rows` | (없음 = `drop`) | `drop` / `keep` | #410 선택 키(`DEFAULTS` 밖). drop = 3상태 모드에서 사유 `no-number` 이고 번호가 그 리포 수집 최대 번호보다 큰 줄을 뺀다(감사 `unknown_reasons` 의 `over-max`). keep = #402 렌더 그대로 |

3상태 표(R7-2 #396, 상위 #393 결정)
-----------------------------------
저장소에 #395 GitHub 근거 테이블(`github_items`·`github_repos`)이 있고 `github_evidence` 가 use 일 때만 3상태 모드다.
테이블이 없거나 ignore 면 위 지금 모양 그대로다(줄 상태 열림/끝남, payload `counts` 없음, 잘림 줄 그대로, 기본 머리만 후보형:
코디네이터 결정 A. 이슈 "테이블 없음 = 열림 후보" 를 이 모양의 `열림` 으로 둔다). 4인자 `build_table`(동결 벤치와
R7-0 평가 하네스)도 지금 모양이다. 근거는 기준 시각 t 시점으로 되돌려 쓴다: 종결·미계획 근거는 종결 시각(머지 PR 은
`merged_at`, 그 밖은 `closed_at`)이 t 이하일 때만, t 뒤에 닫혔으면 t 시점에는 열림, `created_at` > t 면 그때 없던 번호.
GitHub 미확인은 종결로 치지 않는다. 게이트웨이는 GitHub 를 부르지 않는다(근거는 저장소에서만, D8).

| 줄 상태 | 조건 | fmt a 칸 | fmt b 칸 | payload `keys` |
|---|---|---|---|---|
| `열림 후보` | 뱅크 열림 + t 시점 GitHub 열림 이슈(재오픈 포함) | 키, 상태, 마지막 열림 언급 KST 날짜 | 키, 상태, 마지막 열림 언급 KST 날짜 | 들어감 |
| `확인된 종결` | 뱅크 열림 + 종결 근거(완료 이슈·사유 없는 닫힘·머지 PR), 또는 반증 줄(아래) | 키, 상태, 종결 날짜 | + 출처 `github`, 종결 날짜 | 안 들어감 |
| `종결(미계획)` | 뱅크 열림 + 미계획·중복 이슈, 머지 없이 닫힌 PR | 키, 상태 | + 출처 `github`, 종결 날짜 | 안 들어감 |
| `미확인·충돌` | 뱅크 열림 + GitHub 미확인, 뱅크 끝남 + t 시점 GitHub 열림, 또는 근거 나이 상한 밖(stale) | 키, 상태, 사유 코드 | 키, 상태, 사유 코드 | 안 들어감 |
| `끝남` | `recent_done_days` 로 넣은 최근 끝남 키(충돌·반증이 아닐 때) | 키, 상태 | + 날짜 | 안 들어감 |

(fmt a 칸은 #402 에서 바뀌었다: 열림 후보에 날짜, 미확인·충돌에 사유 코드. fmt b 는 그대로.)

뱅크 끝남 키는 t 시점 GitHub 열림일 때(`done-but-open`)와 반증 줄일 때만 줄이 생긴다. 반증 줄(#402, 손잡이
`done_closed_rows` result): recall 결과 행에 나온 키(요소의 결과 행 키 `result_keys`: v2 저장소면 v2 규칙의 참조 고정
아닌 키, 그 밖은 v1 `extract_keys`) 중 뱅크 끝남이고 종결 근거(종결 시각 <= t)가 있는 키를 `확인된 종결` 로 넣는다.
결과 행에 없는 끝남 키, 미계획·미확인 근거는 줄이 없다(토큰). `build_table` 은 `result_keys` 를 줄 때만 반증 줄을 만든다.

근거 나이 상한(#402, 손잡이 `github_max_age_hours`): 리포 행 `fetched_at`(없거나 못 읽으면 그 리포 항목 행 `fetched_at`
최댓값, 둘 다 없으면 stale)이 읽는 시각(벽시계 `_wall_clock`, 기준 시각 t 가 아니다: 근거 신선도를 본다)보다 상한 넘게
오래되면 그 리포 키의 줄은 모두 `미확인·충돌`(사유 `stale`, 날짜 = 뱅크 상태 날짜)이다. 리포 행이 아예 없으면 stale 이
아니라 `repo-not-fetched` 그대로다(미수집과 오래된 수집을 가른다). `build_table` 은 `now` 를 줄 때만 나이를 본다.
근거 시각이 읽는 시각보다 미래면(사이드카 호스트와 게이트웨이 VM 시계 어긋남, rework #402-1) 허용 오차
`GITHUB_FUTURE_SKEW`(10분) 안은 나이 0, 넘으면 stale 이다(손잡이 0 이면 이것도 검사하지 않는다).

| 미확인·충돌 사유 코드 | 뜻 |
|---|---|
| `repo-not-fetched` | 근거 테이블에 그 리포 행이 없음(미수집) |
| `repo-missing` | 리포 없음(GitHub 404) |
| `repo-error` | 조회 실패(인증 만료 포함) |
| `no-number` | 그 번호의 이슈·PR 이 없음, 또는 `created_at` > t |
| `pr-open` | 뱅크 열림 키가 t 시점 열린 PR |
| `no-closed-at` | 종결·미계획 근거인데 종결 시각이 없음 |
| `bad-row` / `unknown-reason` | 근거 행 모양 이상 / 모르는 `state_reason` |
| `done-but-open` | 뱅크 끝남인데 t 시점 GitHub 열림 |
| `stale` | 그 리포 근거가 나이 상한 밖(#402) |

over-max(#410, 손잡이 `over_max_rows` drop): 최종 사유가 `no-number` 인 줄(리포 행 ok, stale 아님) 중 번호가 그 리포 최대
번호(사이드카가 저장소에 둔 그 리포 `github_items` 번호 최댓값, 뱅크가 언급하지 않은 번호 포함, `read_store` 가 같은 연결에서
`MAX(number)` 로 읽는다)보다 큰 줄은 표에서 뺀다. 오귀속이나 업스트림 번호다. 감사 `unknown_reasons` 에 `over-max` 로
세고 `counts` 에는 넣지 않는다. 최대 번호 이하 no-number 는 `미확인·충돌` 그대로("없음"을 단정하지 않는다). 그 리포 항목 행이
없으면 최대 번호가 없어 판정하지 않는다. `build_table` 은 `max_numbers` 를 줄 때만 판정한다(4인자·#402 호출은 그대로).
신선도 예외(rework #410-1): 그 키의 뱅크 상태 시각(마지막 비참조 언급)이 리포 행 `fetched_at` 보다 늦으면 마지막 수집 뒤 새로
생긴 실제 번호일 수 있어 빼지 않고 `no-number` 로 둔다(리포 행 fetched_at 을 못 읽으면 예외 없이 판정한다).

* 잘림: `max_rows` 를 넘으면 `열림 후보` 를 먼저 남기고(그 안과 나머지는 뱅크 상태 날짜 최신순), 잘림 줄
  `(상한 N줄: 생략 <상태> n개, ...)` 에 상태별 생략 수를 적는다.
* payload 에 `counts`(상태별 줄 수)와 `fetched_at`(근거 최신 `fetched_at`)을 더한다. 머리 다음 줄에 상태 설명
  `LEGEND[fmt]`(사유 코드 설명 포함) 한 줄.
* 근거 읽기 범위(rework #396-1): ignore 면 근거 테이블을 읽지 않는다. use 면 저장소를 열기 전에 정한 범위
  (`github_read_scope`: 요소 범위 repo 합집합 + 결과 행 v2 repo, 범위가 전체면 언급된 키의 repo 전부)의 리포 행과,
  뱅크가 언급한 (repo, 번호)의 항목 행만 같은 연결·같은 시간 상한 안에서 읽는다(`read_store` 의 `github_repos`).
* 감사 줄: `github_evidence`, `done_closed_rows`, `github_max_age_hours`, `over_max_rows`(#410, 없으면 기본값), `key_rules`(저장소 meta,
  없으면 v1), 3상태 모드면 `counts`·`unknown_reasons`(사유별 줄 수)·
  `github_fetched_at`.
* 키 규칙(#395): 저장소 meta `key_rules` 가 v2 면 `role_states` 동점(서로 다른 기억이 같은 시각에서 갈림)은 끝남이고,
  범위 출처가 `result` 일 때 결과 행 키를 v2 규칙(`result_repos_v2`, 참조 고정 언급 제외)으로 다시 계산한다. 순서는
  v1 `table_scope`(키 없으면 저장소를 열지 않음, `no-result-keys`) -> 저장소 읽기 -> `rescope_for_key_rules`(비면
  `no-result-keys-v2`, 연 뒤 미부착). 벤치 재현용 전체 계산은 `table_scope_v2`.
  v2.1(#410) 저장소는 v2 와 같은 경로를 v2.1 규칙(`item_roles.key_hits_for`)으로 탄다: 동점 끝남, 결과 행 범위
  (`result_repos_v2(rows, aliases, key_rules)`)와 반증 키(`result_keys`)를 v2.1 로 다시 계산한다. 근거 읽기 범위는 저장소를
  열기 전이라 v2 결과 repo 로 정한다(v2.1 결과 repo 는 그 부분집합, gateway/item_keys.py v2.1 주석).

사이드 저장소 계약(S4 #376 이 쓰고 S3 가 읽는다, 스키마 버전 1)
  * `meta(k, v)`: 필수 `schema_version`='1', `model`, `prompt_sha256`, `aliases_sha256`, `updated_at`.
  * `memories(memory_id, document_id, mentioned_at, state, text_sha256, judged_at)`.
  * `mention_roles(memory_id, key, role)`: role 은 done/open/ref, key 는 `<repo>#<n>`.
  * 별칭 표: S3 가 읽은 뱅크 `item_repo_aliases`(키·값 소문자 정규화 뒤)의 sha256(키 정렬·압축 JSON, ensure_ascii=False)이 meta
    `aliases_sha256` 과 다르면 감사 줄 `aliases_mismatch: true` 경고만 남긴다(표는 붙인다).

한계:
  * project 태그 중 glob 문자(`*`, `?`, `[`)가 든 것은 범위로 치지 않는다(접두어 glob 은 정규 repo 가 아니다).
  * 저장소는 한 뱅크 전용이다(meta 에 뱅크 식별자가 없다). 뱅크 한정은 env `HC_ITEM_OPEN_TABLE_BANK`(저장소가 판정한
    뱅크 id 하나)가 한다: 요청 뱅크가 이 값과 정확히 같을 때만 붙인다. 값이 없거나 비면 감사 사유 `bank-unset`, 다르면
    `other-bank`(둘 다 원본 바이트, 저장소를 열지 않는다, final-review #377-1).
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import sqlite3
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple
from urllib.parse import quote

from . import item_keys, item_roles, item_status, reframe
from .audit import Audit
from .retain_gates import _audit

ENV_FLAG = "HC_ITEM_OPEN_TABLE"
ENV_CONFIG = "HC_ITEM_OPEN_TABLE_CONFIG"
ENV_KNOBS = "HC_ITEM_OPEN_TABLE_KNOBS"
ENV_DB = "HC_ITEM_ROLES_DB"
ENV_BANK = "HC_ITEM_OPEN_TABLE_BANK"
DEFAULT_CONFIG_PATH = "/app/policies/item-open-table.json"
DEFAULT_DB_PATH = "/app/policies/item-roles.sqlite"
DECISION = "item-open-table"
FIELD = "gateway_item_open_table"
SCHEMA_VERSION = "1"
ROLE_DONE, ROLE_OPEN, ROLE_REF = "done", "open", "ref"
STATE_OPEN, STATE_DONE = "열림", "끝남"
OPEN_TAG, CLOSE_TAG = "<item_open_table>", "</item_open_table>"
COLS = {"a": "키 | 상태", "b": "키 | 상태 | 날짜"}
# R6-3(#391) 기본 머리(C7): R6-1 머리 첫 문단 끝에 표 밖 키 지시 한 문장을 더했다. R6-2(#388) 같은 세션 2회 측정에서
# P_bank 100%/100%, R_bank(S4) 평균이 R6-1 머리 이상이었다(`artifacts/arch-v2/r6-3-header.md`). R7-2(#396)에서 기본값이
# 아니다: 표가 안 붙거나 잘리면 "표 밖 키 금지" 지시가 열린 일을 숨긴다(상위 #393 결정). 재현용으로만 남긴다.
HEADER_C7 = ("기준일 {t} 현재 뱅크 기억 기준 열린 작업 키다(기준일 이전 기억만). "
             "여기 없는 키는 뱅크 기억 기준 열림이 아니거나 질의 범위 밖이다. "
             "이 표에 없는 작업 키는 열린 목록에 넣지 마라.\n줄: {cols}")
# R7-2(#396) 기본 머리: 후보형. 표 밖 키를 금지하지 않는다. 3상태 모드(GitHub 근거 사용)와 지금 모양(근거 없음) 둘 다에
# 맞는 문구다(상태 이름 설명은 3상태 모드에서만 `LEGEND` 줄로 붙는다).
HEADER_DEFAULT = ("기준일 {t} 현재 뱅크 기억(GitHub 근거가 있으면 함께)으로 정리한 작업 키 후보다(기준일 이전 근거만). "
                  "열린 키도 이미 끝났을 수 있고 여기 없는 키도 열려 있을 수 있으니 다른 기억과 함께 판단하라.\n줄: {cols}")
# R6-1(#387) 기본 머리. R6-2 측정을 재현할 수 있게 손잡이용으로 남긴다(기본값이 아니다).
HEADER_R6_1 = ("기준일 {t} 현재 뱅크 기억 기준 열린 작업 키다(기준일 이전 기억만). "
               "여기 없는 키는 뱅크 기억 기준 열림이 아니거나 질의 범위 밖이다.\n줄: {cols}")
# 런 5(#377) 기본 어휘. 손잡이로 다시 쓸 수 있게 남긴다(R6-1 에서 기본값이 아니다).
STATUS_TERMS_V1 = ["열림", "남은", "미완", "할 일", "진행", "대기",
                   "open", "todo", "pending", "remaining", "backlog", "status"]
# R6-1(#387) 기본 어휘: 할 일·남은·진행·끝·미완·상태·목록 계열. 부분 문자열 일치(대소문자 무시)라 넓은 낱말은 뺐다:
# 영어 `open` 은 `open` 으로 시작하는 도구 이름에 걸리고, 맨 `진행`·`끝`·`완료` 는 일반 질의에도 흔하다. 벤치 dev 질의 부착과
# R5-A2 dev 상태 질의 부착을 오프라인으로 잰 값으로 골랐다(`artifacts/arch-v2/r6-1-payload.md`).
STATUS_TERMS_DEFAULT = ["할 일", "할일", "해야 할", "남은", "남아", "미완", "미착수", "미해결", "미뤄", "밀린", "보류", "대기",
                        "진행 중", "진행중", "진행 상태", "진행 상황", "끝나지", "안 끝", "못 끝", "끝난", "끝낸",
                        "열림", "열린", "열려", "상태", "목록", "현황",
                        "todo", "to-do", "pending", "remaining", "backlog", "in progress", "unfinished", "status"]
SCOPES = ("request_or_result_project", "request_project", "all")
NOT_ATTACHED_NO_TERM, NOT_ATTACHED_NO_KEYS = "no-status-term", "no-result-keys"
NOT_ATTACHED_NO_KEYS_V2 = "no-result-keys-v2"         # v1 결과 키는 있는데 v2 결과 키가 없음(저장소를 연 뒤, #396)
# 3상태 모드 줄 상태(#396). 상태 이름과 사유 코드는 모듈 머리 주석 표가 정본이다.
ROW_CANDIDATE, ROW_CLOSED, ROW_NOT_PLANNED, ROW_UNKNOWN = "열림 후보", "확인된 종결", "종결(미계획)", "미확인·충돌"
ROW_STATES = (ROW_CANDIDATE, ROW_CLOSED, ROW_NOT_PLANNED, ROW_UNKNOWN, STATE_DONE)
SOURCE_GITHUB = "github"
# 미확인·충돌 사유 코드(#395 근거 코드 중 미확인 계열 + 이 모듈이 더하는 시점 판정 코드)
REASON_PR_OPEN, REASON_NO_CLOSED_AT, REASON_DONE_BUT_OPEN, REASON_NO_NUMBER, REASON_STALE = (
    "pr-open", "no-closed-at", "done-but-open", "no-number", "stale")
UNKNOWN_REASONS = ("repo-not-fetched", "repo-missing", "repo-error", REASON_NO_NUMBER, "bad-row", "unknown-reason",
                   REASON_PR_OPEN, REASON_NO_CLOSED_AT, REASON_DONE_BUT_OPEN, REASON_STALE)
GITHUB_USE, GITHUB_IGNORE = "use", "ignore"
DONE_CLOSED_RESULT, DONE_CLOSED_OFF = "result", "off"
OVER_MAX_DROP, OVER_MAX_KEEP = "drop", "keep"            # #410
REASON_OVER_MAX = "over-max"                              # #410 감사 unknown_reasons 코드(표 줄 사유가 아니다)
GITHUB_MAX_AGE_HOURS_DEFAULT = 2
GITHUB_FUTURE_SKEW = timedelta(minutes=10)       # 미래 시각 근거 허용 오차(rework #402-1, 손잡이 아님)
# fmt a(#402): 열림 후보는 날짜, 확인된 종결은 종결 날짜, 미확인·충돌은 사유 코드를 셋째 칸에 싣는다.
COLS_3 = {"a": "키 | 상태 | 날짜 또는 사유 (열림 후보는 마지막 열림 언급 날짜, 확인된 종결은 종결 날짜, "
               "종결(미계획)은 셋째 칸 없음, 미확인·충돌은 사유 코드)",
          "b": "키 | 상태 | 날짜 (확인된 종결·종결(미계획)은 | 출처 | 종결 날짜, 미확인·충돌은 | 사유)"}
# 3상태 모드에서 머리 다음 줄(상태 이름 설명 + 사유 코드 설명, 한 줄).
_LEGEND_STATES = ("상태: 열림 후보=뱅크 열림·GitHub 열림, 확인된 종결=GitHub 종결 근거, "
                  "종결(미계획)=GitHub 미계획·중복·미머지 PR, 미확인·충돌=GitHub 확인 불가이거나 뱅크와 어긋남")
_LEGEND_REASONS = ("(사유: repo-not-fetched 미수집, repo-missing 리포 없음, repo-error 조회 실패, "
                   "no-number 번호 없음, pr-open 열린 PR, no-closed-at 종결 시각 없음, "
                   "done-but-open 뱅크 끝남·GitHub 열림, bad-row·unknown-reason 근거 이상, stale 근거 오래됨)")
LEGEND = {"a": _LEGEND_STATES + _LEGEND_REASONS, "b": _LEGEND_STATES + _LEGEND_REASONS}
_KST = timezone(timedelta(hours=9))
_HEADER_FIELDS = frozenset({"t", "cols"})
_GLOB = frozenset("*?[")
_REQUIRED_TABLES = frozenset({"meta", "memories", "mention_roles"})

log = logging.getLogger(__name__)
_clock: Callable[[], float] = time.monotonic          # 테스트가 바꿔 끼운다(시간 초과 재현)
_wall_clock: Callable[[], float] = time.time          # 근거 나이를 재는 벽시계(#402, 테스트가 바꿔 끼운다)

DEFAULTS: Dict[str, Any] = {
    "table_fmt": "a",
    "header": HEADER_DEFAULT,
    "attach": "status_query",
    "status_terms": list(STATUS_TERMS_DEFAULT),
    "recent_done_days": 0,
    "max_rows": 300,
    "scope": "request_or_result_project",
    "timeout_ms": 1500,
}


def enabled() -> bool:
    """기본 OFF. 1/true/yes/on 만 켠다."""
    return os.environ.get(ENV_FLAG, "0").strip().lower() in {"1", "true", "yes", "on"}


# --- 손잡이 --------------------------------------------------------------------------------------

def _one_of(*allowed: str) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, str) and v in allowed


def _int_between(lo: int, hi: int) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _valid_header(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 400:
        return False
    try:
        for _, name, spec, conv in string.Formatter().parse(value):
            if name is not None and (name not in _HEADER_FIELDS or spec or conv):
                return False
        value.format(t="t", cols="c")
    except (ValueError, KeyError, IndexError):
        return False
    return True


def _valid_terms(value: Any) -> bool:
    return (isinstance(value, list) and 1 <= len(value) <= 40
            and all(isinstance(v, str) and v.strip() and len(v) <= 40 for v in value))


_VALIDATORS: Dict[str, Callable[[Any], bool]] = {
    "table_fmt": _one_of("a", "b"),
    "header": _valid_header,
    "attach": _one_of("always", "status_query"),
    "status_terms": _valid_terms,
    "recent_done_days": _int_between(0, 30),
    "max_rows": _int_between(1, 1000),
    "scope": _one_of(*SCOPES),
    "timeout_ms": _int_between(100, 10000),
}
# DEFAULTS 밖 선택 손잡이(#396): 값이 없으면 적용 값에도 넣지 않는다(정규 sha256 불변, 없으면 `use` 로 읽는다).
_OPTIONAL_VALIDATORS: Dict[str, Callable[[Any], bool]] = {
    "github_evidence": _one_of(GITHUB_USE, GITHUB_IGNORE),
    "done_closed_rows": _one_of(DONE_CLOSED_RESULT, DONE_CLOSED_OFF),          # #402
    "github_max_age_hours": _int_between(0, 8760),                             # #402
    "over_max_rows": _one_of(OVER_MAX_DROP, OVER_MAX_KEEP),                    # #410
}


def github_mode(knobs: Mapping[str, Any]) -> str:
    """손잡이 `github_evidence`(없으면 use)."""
    return knobs.get("github_evidence", GITHUB_USE)


def done_closed_mode(knobs: Mapping[str, Any]) -> str:
    """손잡이 `done_closed_rows`(#402, 없으면 result)."""
    return knobs.get("done_closed_rows", DONE_CLOSED_RESULT)


def github_max_age_hours(knobs: Mapping[str, Any]) -> int:
    """손잡이 `github_max_age_hours`(#402, 없으면 2, 0 = 검사 안 함)."""
    return knobs.get("github_max_age_hours", GITHUB_MAX_AGE_HOURS_DEFAULT)


def over_max_mode(knobs: Mapping[str, Any]) -> str:
    """손잡이 `over_max_rows`(#410, 없으면 drop)."""
    return knobs.get("over_max_rows", OVER_MAX_DROP)


def _canon_sha256(value: Any) -> str:
    canon = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Knobs:
    values: Dict[str, Any]
    source: str                                   # env / file / default
    warnings: List[str] = field(default_factory=list)

    @property
    def sha256(self) -> str:
        return _canon_sha256(self.values)


def _warn_code_for_unknown(key: Any) -> str:
    name = "".join(ch for ch in str(key) if ch.isalnum() or ch in "_-")[:40]
    return f"unknown:{name}"


def parse_knobs(raw: str) -> Tuple[Dict[str, Any], List[str]]:
    """설정 원문 -> (적용 값, 경고 코드). 잘못된 키만 기본값, 전체가 잘못되면 전부 기본값."""
    try:
        data = json.loads(raw)
    except ValueError:
        return _defaults(), ["invalid-json"]
    if not isinstance(data, dict):
        return _defaults(), ["not-an-object"]
    values = _defaults()
    warnings: List[str] = []
    for key, value in data.items():
        check = _VALIDATORS.get(key) or _OPTIONAL_VALIDATORS.get(key)
        if check is None:
            warnings.append(_warn_code_for_unknown(key))
        elif check(value):
            values[key] = list(value) if isinstance(value, list) else value
        else:
            warnings.append(key)
    return values, warnings


def _defaults() -> Dict[str, Any]:
    return {k: list(v) if isinstance(v, list) else v for k, v in DEFAULTS.items()}


_CACHE: Optional[Tuple[Tuple[Any, ...], Knobs]] = None


def _reset_cache() -> None:
    global _CACHE
    _CACHE = None


def _remember(cache_key: Tuple[Any, ...], build: Callable[[], Knobs]) -> Knobs:
    global _CACHE
    if _CACHE is not None and _CACHE[0] == cache_key:
        return _CACHE[1]
    knobs = build()
    if knobs.warnings:
        log.warning("%s 손잡이 설정(%s) 경고 %s: 해당 키는 기본값으로 둔다", ENV_FLAG, knobs.source,
                    ",".join(knobs.warnings))
    _CACHE = (cache_key, knobs)
    return knobs


def load_knobs() -> Knobs:
    """적용할 손잡이. env 인라인 JSON > 설정 파일(바뀔 때만 다시 읽음) > 기본값."""
    inline = os.environ.get(ENV_KNOBS, "")
    if inline.strip():
        def from_env() -> Knobs:
            values, warnings = parse_knobs(inline)
            return Knobs(values, "env", warnings)
        return _remember(("env", inline), from_env)
    path = os.environ.get(ENV_CONFIG, "").strip() or DEFAULT_CONFIG_PATH
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return _remember(("default", path), lambda: Knobs(_defaults(), "default"))
    except OSError:
        return _remember(("unreadable", path), lambda: Knobs(_defaults(), "default", ["unreadable"]))

    def from_file() -> Knobs:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except (OSError, UnicodeDecodeError):
            return Knobs(_defaults(), "default", ["unreadable"])
        values, warnings = parse_knobs(raw)
        return Knobs(values, "file", warnings)
    return _remember(("file", path, st.st_mtime_ns, st.st_size, st.st_ctime_ns, st.st_ino), from_file)


# --- 사이드 저장소(읽기 전용) ----------------------------------------------------------------------

class StoreError(Exception):
    """fail-open 사유를 실은 예외. `reason` 이 감사 줄 사유다."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Mention:
    key: str
    role: str                                     # done / open
    at: datetime
    memory_id: str


@dataclass(frozen=True)
class Store:
    meta: Dict[str, str]
    mentions: Tuple[Mention, ...]                 # state=valid, mentioned_at 읽힘, 비참조 언급만
    github: Optional[Dict[str, Dict[Any, Dict[str, Any]]]] = None   # #395 근거 테이블(`item_roles.load_github` 모양)
    github_max: Optional[Dict[str, int]] = None   # #410 리포별 github_items 번호 최댓값(근거를 읽을 때만, 범위 안 리포)

    @property
    def key_rules(self) -> str:
        """meta `key_rules`(#395, 없으면 v1)."""
        return item_roles.key_rules_of(self.meta)


def db_path() -> str:
    return os.environ.get(ENV_DB, "").strip() or DEFAULT_DB_PATH


def connect_ro(path: str) -> sqlite3.Connection:
    """`file:...?mode=ro` URI 로 연다. 이 연결로는 쓰기·저널 생성이 불가능하다."""
    return sqlite3.connect(f"file:{quote(os.path.abspath(path))}?mode=ro", uri=True, timeout=0,
                           isolation_level=None, check_same_thread=False)


GITHUB_ALL = object()        # read_store 기본값: 언급된 키가 있는 리포 전부의 근거를 읽는다
_UNSET = object()
# 게이트웨이가 요청마다 근거 읽기 범위를 넘기는 통로. `asyncio.to_thread` 는 호출 시점 context 를 복사하므로 그 recall 의
# 읽기 스레드에만 보인다. read_store 의 2인자 호출 모양(벤치·기존 테스트가 바꿔 끼우는 함수 모양)을 유지하려는 것이다.
_GITHUB_SCOPE: contextvars.ContextVar = contextvars.ContextVar("item_open_table_github_scope", default=GITHUB_ALL)
_IN_CHUNK = 400              # sqlite 바인드 변수 상한(999) 아래로 나눠 묻는다


def _read_github_scoped(conn: sqlite3.Connection, tables: Set[str], raw: List[Tuple[Any, ...]],
                        repos: Any) -> Tuple[Optional[Dict[str, Dict[Any, Dict[str, Any]]]], Optional[Dict[str, int]]]:
    """근거 테이블에서 범위 안 리포의 리포 행과, 뱅크가 언급한 (리포, 번호)의 항목 행만 읽는다(rework #396-1).
    repos: 리포 집합 또는 GITHUB_ALL(언급된 키의 리포 전부). -> (근거, 리포별 항목 번호 최댓값(#410, 범위 안 리포의
    모든 항목 행 기준)). 테이블이 없으면 (None, None). 근거는 `item_roles.load_github` 와 같은 모양."""
    if not {"github_items", "github_repos"} <= tables:
        return None, None
    numbers: Dict[str, Set[int]] = {}
    for key, _, _, _ in raw:
        parsed = item_roles.split_key(key) if isinstance(key, str) else None
        if parsed and (repos is GITHUB_ALL or parsed[0] in repos):
            numbers.setdefault(parsed[0], set()).add(parsed[1])
    want = sorted(numbers) if repos is GITHUB_ALL else sorted(repos)
    out: Dict[str, Dict[Any, Dict[str, Any]]] = {"repos": {}, "items": {}}
    repo_cols, item_cols = ", ".join(item_roles.GH_REPO_COLS), ", ".join(item_roles.GH_ITEM_COLS)
    maxes: Dict[str, int] = {}
    for i in range(0, len(want), _IN_CHUNK):
        part = want[i:i + _IN_CHUNK]
        sql = "SELECT %s FROM github_repos WHERE repo IN (%s)" % (repo_cols, ",".join("?" * len(part)))
        for r in conn.execute(sql, part):
            out["repos"][r[0]] = dict(zip(item_roles.GH_REPO_COLS[1:], r[1:]))
        sql = "SELECT repo, MAX(number) FROM github_items WHERE repo IN (%s) GROUP BY repo" % ",".join("?" * len(part))
        for repo, top in conn.execute(sql, part):
            if isinstance(top, int):
                maxes[repo] = top
    for repo in sorted(numbers):
        nums = sorted(numbers[repo])
        for i in range(0, len(nums), _IN_CHUNK):
            part = nums[i:i + _IN_CHUNK]
            sql = "SELECT %s FROM github_items WHERE repo = ? AND number IN (%s)" % (item_cols, ",".join("?" * len(part)))
            for r in conn.execute(sql, [repo, *part]):
                out["items"][(r[0], r[1])] = dict(zip(item_roles.GH_ITEM_COLS[2:], r[2:]))
    return out, maxes


def read_store(path: str, timeout_s: float, github_repos: Any = _UNSET) -> Store:
    """저장소를 읽기 전용으로 열어 비참조 언급을 읽는다. 실패는 `StoreError(사유)`.

    github_repos(rework #396-1): None 이면 근거 테이블을 읽지 않는다(손잡이 ignore), 리포 집합이면 그 리포의 근거만,
    GITHUB_ALL 이면 언급된 키가 있는 리포 전부. 어느 쪽이든 항목 행은 뱅크가 언급한 (리포, 번호)만 읽는다. 인자를 주지
    않으면 `_GITHUB_SCOPE`(게이트웨이가 recall 마다 정한 값, 없으면 GITHUB_ALL)를 쓴다."""
    if github_repos is _UNSET:
        github_repos = _GITHUB_SCOPE.get()
    deadline = _clock() + timeout_s
    if not os.path.isfile(path):
        raise StoreError("no-store")
    try:
        conn = connect_ro(path)
    except sqlite3.Error as exc:
        raise StoreError(f"open-failed:{type(exc).__name__}") from exc
    try:
        conn.set_progress_handler(lambda: 1 if _clock() > deadline else 0, 1000)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.OperationalError as exc:
            if _clock() > deadline:
                raise StoreError("timeout") from exc
            raise StoreError(f"open-failed:{type(exc).__name__}") from exc
        except sqlite3.Error as exc:                       # 데이터베이스가 아닌 파일 등
            raise StoreError(f"open-failed:{type(exc).__name__}") from exc
        if not _REQUIRED_TABLES <= tables:
            raise StoreError("schema-mismatch")
        try:
            meta = {str(k): str(v) for k, v in conn.execute("SELECT k, v FROM meta")}
            if meta.get("schema_version") != SCHEMA_VERSION:
                raise StoreError("schema-mismatch")
            raw = conn.execute(
                "SELECT r.key, r.role, m.mentioned_at, m.memory_id FROM mention_roles r "
                "JOIN memories m ON m.memory_id = r.memory_id "
                "WHERE m.state = 'valid' AND r.role IN ('done', 'open') AND m.mentioned_at IS NOT NULL"
            ).fetchall()
            github, github_max = ((None, None) if github_repos is None     # 같은 연결·시간 상한, 테이블이 없으면 None
                                  else _read_github_scoped(conn, tables, raw, github_repos))
        except sqlite3.OperationalError as exc:
            if _clock() > deadline:
                raise StoreError("timeout") from exc
            raise StoreError("schema-mismatch") from exc
        except sqlite3.Error as exc:
            raise StoreError(f"open-failed:{type(exc).__name__}") from exc
        if _clock() > deadline:
            raise StoreError("timeout")
    finally:
        conn.close()
    mentions = []
    for key, role, at_raw, mid in raw:
        at = item_status._instant(at_raw)
        if at is None or not isinstance(key, str) or not isinstance(mid, str):
            continue
        mentions.append(Mention(key, role, at, mid))
    return Store(meta, tuple(mentions), github, github_max)


# --- 순수 규칙 ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class KeyState:
    state: str                                    # 열림 / 끝남
    at: datetime                                  # 상태를 정한 언급 시각

    @property
    def day(self) -> str:
        return self.at.astimezone(_KST).date().isoformat()


def role_states(mentions: Iterable[Mention], cutoff: datetime, key_rules: Optional[str] = None) -> Dict[str, KeyState]:
    """키마다 cutoff 이하 마지막 비참조 언급의 역할(같은 시각이 갈리면 열림). 비참조 언급이 없는 키는 없다(미정).

    key_rules 가 "v2"·"v2.1"(#396·#410, 저장소 meta `key_rules`)면 서로 다른 기억이 같은 시각에서 갈릴 때 끝남이다(실측 0건, 테스트로만
    고정). 그 밖(None·"v1")은 지금 규칙 그대로.
    입력은 state=valid 인 기억의 언급이어야 한다(`read_store` 가 거른다). 참조(ref) 언급은 무시한다."""
    tie_done = key_rules in (item_roles.KEY_RULES_V2, item_roles.KEY_RULES_V21)    # v2.1(#410)도 v2 와 같다
    last: Dict[str, Tuple[datetime, bool]] = {}
    for m in mentions:
        if m.role not in (ROLE_DONE, ROLE_OPEN) or m.at > cutoff:
            continue
        is_open = m.role == ROLE_OPEN
        prev = last.get(m.key)
        if prev is None or m.at > prev[0]:
            last[m.key] = (m.at, is_open)
        elif m.at == prev[0] and is_open != prev[1]:   # 같은 시각에서 갈리면 v1 열림, v2 끝남
            last[m.key] = (m.at, not tie_done)
    return {k: KeyState(STATE_OPEN if is_open else STATE_DONE, at) for k, (at, is_open) in last.items()}


def _key_sort(key: str) -> Tuple[str, int, str]:
    repo, _, n = key.rpartition("#")
    return (repo, int(n), key) if n.isdigit() else (repo, 0, key)


def request_repos(tags: Any, aliases: Mapping[str, str]) -> Set[str]:
    """요청 tags 의 `project:<slug>` -> 정규 repo 집합(뱅크 별칭 표로 정규화). glob 태그는 뺀다."""
    if not isinstance(tags, list):
        return set()
    table = item_keys._table(aliases)
    out: Set[str] = set()
    for tag in tags:
        if not isinstance(tag, str):
            continue
        low = tag.strip().lower()
        if not low.startswith("project:") or _GLOB & set(low):
            continue
        repo = item_keys._canonical(low[len("project:"):], table)
        if repo:
            out.add(repo)
    return out


def result_repos(rows: Any, aliases: Mapping[str, str]) -> Set[str]:
    """recall 결과 행에서 `item_keys.extract_keys` 로 나온 키의 정규 repo 집합(뱅크 별칭 표로 정규화)."""
    if not isinstance(rows, list):
        return set()
    out: Set[str] = set()
    for row in rows:
        if isinstance(row, Mapping):
            out.update(repo for repo, _ in item_keys.extract_keys(item_keys._text(row), row.get("tags"), aliases))
    return out


def table_scope(args: Mapping[str, Any], rows: Any, knobs: Mapping[str, Any],
                aliases: Mapping[str, str]) -> Tuple[Optional[Set[str]], str]:
    """표 범위 -> (repo 집합 또는 None(전체), 출처). 출처: `request` / `result` / `all` / `no-result-keys`(붙이지 않음).

    * `all`: 뱅크 전체.
    * `request_project`: 요청 project 태그의 repo, 없으면 뱅크 전체(런 5 기본값).
    * `request_or_result_project`(R6-1 기본값): 요청 project 태그의 repo, 없으면 결과 행 키의 repo. 결과에 키가 없으면
      붙이지 않는다(뱅크 전체는 담지 않는다)."""
    scope = knobs["scope"]
    if scope == "all":
        return None, "all"
    repos = request_repos(args.get("tags"), aliases)
    if repos:
        return repos, "request"
    if scope == "request_project":
        return None, "all"
    repos = result_repos(rows, aliases)
    if repos:
        return repos, "result"
    return None, NOT_ATTACHED_NO_KEYS


def result_repos_v2(rows: Any, aliases: Mapping[str, str], key_rules: str = item_roles.KEY_RULES_V2) -> Set[str]:
    """recall 결과 행에서 #395 v2 규칙(`item_keys.key_hits_v2`, key_rules="v2.1" 이면 #410 `key_hits_v21`)으로 나온 키의
    정규 repo 집합. 업스트림·외부 리포 문맥의 참조 고정 언급(`ref_fixed`)은 작업 키가 아니므로 범위로 치지 않는다."""
    if not isinstance(rows, list):
        return set()
    out: Set[str] = set()
    for row in rows:
        if isinstance(row, Mapping):
            out.update(h.key[0] for h in item_roles.key_hits_for(item_keys._text(row), row.get("tags"), aliases,
                                                                 key_rules)
                       if not h.ref_fixed)
    return out


def rescope_for_key_rules(scope: Tuple[Optional[Set[str]], str], rows: Any, aliases: Mapping[str, str],
                          key_rules: str) -> Tuple[Optional[Set[str]], str]:
    """저장소를 읽은 뒤의 범위(#396): key_rules 가 v2(·v2.1 #410) 이고 v1 범위 출처가 `result` 면 결과 행 키를 v2 로 다시 계산한다.
    비면 `(None, no-result-keys-v2)`(붙이지 않음). 그 밖은 v1 범위 그대로."""
    repos, source = scope
    if key_rules not in (item_roles.KEY_RULES_V2, item_roles.KEY_RULES_V21) or source != "result":
        return scope
    repos2 = result_repos_v2(rows, aliases, key_rules)
    return (repos2, "result") if repos2 else (None, NOT_ATTACHED_NO_KEYS_V2)


def github_read_scope(targets: Mapping[Any, Tuple[datetime, Optional[Set[str]], str]], rows_by_el: Mapping[Any, Any],
                      aliases: Mapping[str, str], knobs: Mapping[str, Any]) -> Any:
    """저장소를 열기 전에 근거 테이블 읽기 범위를 정한다(rework #396-1). ignore 면 None(읽지 않음). 범위가 전체(None)인
    요소가 있으면 GITHUB_ALL. 그 밖은 요소 범위 repo 의 합집합 + 결과 행 출처 요소의 v2 결과 repo(저장소 meta 가 v2 면
    읽은 뒤 범위가 그쪽으로 바뀔 수 있으므로 미리 넣는다)."""
    if github_mode(knobs) != GITHUB_USE:
        return None
    out: Set[str] = set()
    for el_id, (_, repos, source) in targets.items():
        if repos is None:
            return GITHUB_ALL
        out |= repos
        if source == "result":
            out |= result_repos_v2(rows_by_el.get(el_id), aliases)
    return out


def table_scope_v2(args: Mapping[str, Any], rows: Any, knobs: Mapping[str, Any], aliases: Mapping[str, str],
                   key_rules: str) -> Tuple[Optional[Set[str]], str]:
    """게이트웨이 범위 계산 전체(벤치 하네스 재현용): v1 `table_scope` -> (`no-result-keys` 가 아니면) 저장소 meta
    `key_rules` 로 `rescope_for_key_rules`. 게이트웨이는 두 단계 사이에 저장소를 연다."""
    scope = table_scope(args, rows, knobs, aliases)
    if scope[1] == NOT_ATTACHED_NO_KEYS:
        return scope
    return rescope_for_key_rules(scope, rows, aliases, key_rules)


def result_keys(rows: Any, aliases: Mapping[str, str], key_rules: Optional[str]) -> Set[str]:
    """recall 결과 행의 작업 키 `<repo>#<n>` 집합(#402 반증 줄 한정). 범위 계산과 같은 규칙: key_rules 가 v2 면
    `item_keys.key_hits_v2`(v2.1 이면 #410 `key_hits_v21`)의 참조 고정 아닌 키(`result_repos_v2` 와 같다), 그 밖은 v1 `item_keys.extract_keys`
    (`result_repos` 와 같다). 요소 범위(repo) 한정은 `build_table` 이 한다."""
    if not isinstance(rows, list):
        return set()
    out: Set[str] = set()
    v2 = key_rules in (item_roles.KEY_RULES_V2, item_roles.KEY_RULES_V21)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if v2:
            hits = [h.key for h in item_roles.key_hits_for(item_keys._text(row), row.get("tags"), aliases, key_rules)
                    if not h.ref_fixed]
        else:
            hits = list(item_keys.extract_keys(item_keys._text(row), row.get("tags"), aliases))
        out.update(item_roles.key_text(k) for k in hits)
    return out


def payload_table(table: Mapping[str, Any]) -> Dict[str, Any]:
    """`build_table` 결과 -> recall payload 에 싣는 모양 `{t, fmt, keys, truncated, text}`(R6-1 #387).

    상태·날짜는 `text` 만 담는다. `keys` 는 열린 키만(표 줄과 같은 순서): `recent_done_days` > 0 이면 `text` 에 들어가는
    최근 끝남 키는 `keys` 에 넣지 않는다(구조 필드만 읽는 소비자가 끝남을 열림으로 세지 않게, #387-1).
    3상태 모드(#396)면 `keys` 는 `열림 후보` 키만이고 `counts`(상태별 줄 수)와 `fetched_at`(근거 최신 시각)을 더한다.
    `build_table` 의 `rows` 는 런 5 하네스가 쓴다."""
    open_state = ROW_CANDIDATE if "counts" in table else STATE_OPEN
    out = {"t": table["t"], "fmt": table["fmt"], "keys": [r[0] for r in table["rows"] if r[1] == open_state],
           "truncated": table["truncated"], "text": table["text"]}
    if "counts" in table:
        out["counts"] = dict(table["counts"])
        out["fetched_at"] = table["fetched_at"]
    return out


def _gh_instant(value: Any) -> Optional[datetime]:
    return item_status._instant(value) if isinstance(value, str) and value else None


def github_row(key: str, state: KeyState, cutoff: datetime,
               github: Mapping[str, Mapping[Any, Mapping[str, Any]]], *,
               done_closed: bool = False) -> Optional[Tuple[str, str, str]]:
    """3상태 판정(#396, 순수 함수). 뱅크 키 상태 + #395 GitHub 근거(기준 시각 cutoff 시점으로 되돌린 판정) ->
    (줄 상태, 날짜, 세부) 또는 None(줄 없음). 세부 = 확인된 종결·종결(미계획)은 `github`, 미확인·충돌은 사유 코드,
    열림 후보·끝남은 빈 문자열. 날짜 = 종결 계열은 종결 시각의 KST 날짜, 그 밖은 뱅크 상태 날짜.

    t 시점 근거: 종결·미계획 근거는 종결 시각(머지 PR 은 merged_at, 그 밖은 closed_at)이 t 이하일 때만 쓰고, t 뒤에 닫혔으면
    t 시점에는 열림으로 본다. 종결 시각이 없으면 미확인(no-closed-at). created_at > t 면 그때 없던 번호(no-number).
    GitHub 미확인은 종결로 치지 않는다.

    done_closed(#402): 참이면 뱅크 끝남 키에 t 시점 종결 근거가 있을 때 `확인된 종결` 반증 줄을 낸다(결과 행 키만 참으로
    부른다). 거짓이면 지금처럼 끝남 키는 t 시점 GitHub 열림일 때만 줄이 있다."""
    ev = item_roles.evidence_for_key(key, github)
    parsed = item_roles.split_key(key)
    row = (github.get("items") or {}).get(parsed) if parsed else None
    verdict, reason, kind = ev.verdict, ev.reason, ev.kind
    closed_day = ""
    if verdict != item_roles.GH_UNKNOWN and isinstance(row, Mapping):
        created = _gh_instant(row.get("created_at"))
        if created is not None and created > cutoff:
            verdict, reason = item_roles.GH_UNKNOWN, REASON_NO_NUMBER
    if verdict in (item_roles.GH_CLOSED, item_roles.GH_CLOSED_NO_REASON, item_roles.GH_NOT_PLANNED):
        when = _gh_instant(ev.merged_at if reason == "merged" else ev.closed_at)
        if when is None:
            verdict, reason = item_roles.GH_UNKNOWN, REASON_NO_CLOSED_AT
        elif when > cutoff:                        # t 뒤에 닫힌 근거는 쓰지 않는다: t 시점에는 열림
            verdict, reason = item_roles.GH_OPEN, "open"
        else:
            closed_day = when.astimezone(_KST).date().isoformat()
    if state.state == STATE_DONE:
        if verdict == item_roles.GH_OPEN:
            return ROW_UNKNOWN, state.day, REASON_DONE_BUT_OPEN
        if done_closed and item_roles.is_closed_verdict(verdict):
            return ROW_CLOSED, closed_day, SOURCE_GITHUB
        return None
    if verdict == item_roles.GH_OPEN:
        if kind == "pr":
            return ROW_UNKNOWN, state.day, REASON_PR_OPEN
        return ROW_CANDIDATE, state.day, ""
    if item_roles.is_closed_verdict(verdict):
        return ROW_CLOSED, closed_day, SOURCE_GITHUB
    if verdict == item_roles.GH_NOT_PLANNED:
        return ROW_NOT_PLANNED, closed_day, SOURCE_GITHUB
    return ROW_UNKNOWN, state.day, reason


def _render_row(row: List[str], fmt: str) -> str:
    key, state, day, detail = row
    if state in (ROW_CLOSED, ROW_NOT_PLANNED):
        cells = [key, state, day] if fmt == "a" and state == ROW_CLOSED else (
            [key, state] if fmt == "a" else [key, state, detail, day])
    elif state == ROW_UNKNOWN:
        cells = [key, state, detail]
    elif state == ROW_CANDIDATE:
        cells = [key, state, day]
    else:
        cells = [key, state, day] if fmt == "b" else [key, state]
    return " | ".join(cells)


def github_fetched_at(github: Optional[Mapping[str, Mapping[Any, Mapping[str, Any]]]]) -> Optional[str]:
    """근거 테이블의 최신 fetched_at(리포·항목 행 중 최댓값, 없으면 None)."""
    if not github:
        return None
    vals = [r.get("fetched_at") for part in ("repos", "items") for r in (github.get(part) or {}).values()
            if isinstance(r, Mapping) and isinstance(r.get("fetched_at"), str)]
    return max(vals) if vals else None


def stale_repos(github: Optional[Mapping[str, Mapping[Any, Mapping[str, Any]]]], now: datetime,
                max_age_hours: int) -> Set[str]:
    """근거 나이 상한 밖 리포 집합(#402). 리포 행이 있는 리포만 본다(없으면 `repo-not-fetched` 그대로). 나이 기준 =
    리포 행 `fetched_at`, 없거나 못 읽으면 그 리포 항목 행 `fetched_at` 최댓값, 둘 다 없으면 stale. max_age_hours 0 = 빈
    집합. now 는 읽는 시각(벽시계, 기준 시각 t 가 아니다). 근거 시각이 now 보다 `GITHUB_FUTURE_SKEW` 넘게 미래면 stale
    (시계 어긋남이나 잘못 쓰인 값이 검사를 끄지 않게), 오차 안의 미래는 나이 0."""
    if not github or max_age_hours <= 0:
        return set()
    newest: Dict[str, datetime] = {}
    for (repo, _), r in (github.get("items") or {}).items():
        at = _gh_instant(r.get("fetched_at")) if isinstance(r, Mapping) else None
        if at is not None and (repo not in newest or at > newest[repo]):
            newest[repo] = at
    limit = timedelta(hours=max_age_hours)
    out: Set[str] = set()
    for repo, r in (github.get("repos") or {}).items():
        at = _gh_instant(r.get("fetched_at")) if isinstance(r, Mapping) else None
        at = at or newest.get(repo)
        if at is None or now - at > limit or at - now > GITHUB_FUTURE_SKEW:
            out.add(repo)
    return out


def repo_max_numbers(github: Optional[Mapping[str, Mapping[Any, Mapping[str, Any]]]]) -> Dict[str, int]:
    """근거 테이블(`item_roles.load_github` 모양, 항목 행 전부) -> {repo: 항목 번호 최댓값}(#410). 범위를 좁혀 읽은 근거
    (`read_store`)는 항목 행이 뱅크 언급 번호뿐이라 이 값이 리포 최대가 아니다: 게이트웨이는 `Store.github_max` 를 쓴다."""
    out: Dict[str, int] = {}
    for key in (github or {}).get("items") or {}:
        if isinstance(key, tuple) and len(key) == 2 and isinstance(key[1], int) and not isinstance(key[1], bool):
            out[key[0]] = max(key[1], out.get(key[0], 0))
    return out


def over_max(key: str, github: Optional[Mapping[str, Mapping[Any, Mapping[str, Any]]]],
             max_numbers: Mapping[str, int], *, mentioned_at: Optional[datetime] = None) -> bool:
    """over-max 판정(#410, 순수 함수): 키의 리포 행이 ok 이고 그 번호가 리포 최대 번호(max_numbers)보다 크면 True.
    리포 행이 없거나 ok 가 아니면, 최대 번호를 모르면, 키 모양이 틀리면 False(판정하지 않는다). 호출자는 줄 사유가
    `no-number` 이고 stale 이 아닌 줄에만 쓴다(`_build_table_3state`).
    신선도 예외(rework #410-1): 그 키의 언급 시각 mentioned_at(뱅크 상태를 정한 언급)이 리포 행 `fetched_at` 보다 늦으면
    수집 뒤 새로 생긴 번호일 수 있어 False(no-number 로 남긴다). mentioned_at 이 없거나 리포 행 fetched_at 을 못 읽으면
    이 예외를 쓰지 않는다(over-max 로 본다)."""
    parsed = item_roles.split_key(key)
    if parsed is None or not github:
        return False
    repo, n = parsed
    row = (github.get("repos") or {}).get(repo)
    if not isinstance(row, Mapping) or row.get("status") != "ok":
        return False
    top = max_numbers.get(repo)
    if not (isinstance(top, int) and n > top):
        return False
    fetched = _gh_instant(row.get("fetched_at"))
    return not (mentioned_at is not None and fetched is not None and mentioned_at > fetched)


def _build_table_3state(states: Mapping[str, KeyState], cutoff: datetime, repos: Optional[Set[str]],
                        knobs: Mapping[str, Any], github: Mapping[str, Mapping[Any, Mapping[str, Any]]],
                        result_keys: Optional[Set[str]] = None, now: Optional[datetime] = None,
                        max_numbers: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    t_day = cutoff.astimezone(_KST).date()
    n = knobs["recent_done_days"]
    lo = (t_day - timedelta(days=n - 1)).isoformat() if n > 0 else None
    picked: List[Tuple[List[str], datetime]] = []        # ([키, 줄 상태, 날짜, 세부], 뱅크 상태 시각)
    rebut = result_keys if result_keys is not None and done_closed_mode(knobs) == DONE_CLOSED_RESULT else set()
    stale = stale_repos(github, now, github_max_age_hours(knobs)) if now is not None else set()
    drop_over = max_numbers is not None and over_max_mode(knobs) == OVER_MAX_DROP
    n_over = 0
    for key, st in states.items():
        repo = key.rpartition("#")[0]
        if repos is not None and repo not in repos:
            continue
        got = github_row(key, st, cutoff, github, done_closed=key in rebut)
        if got is None and st.state == STATE_DONE and lo is not None and st.day >= lo:
            got = (STATE_DONE, st.day, "")
        if got is not None and repo in stale:            # 근거가 오래된 리포의 줄은 모두 미확인(#402)
            got = (ROW_UNKNOWN, st.day, REASON_STALE)
        if (drop_over and got is not None and got[0] == ROW_UNKNOWN and got[2] == REASON_NO_NUMBER
                and over_max(key, github, max_numbers, mentioned_at=st.at)):   # 최대 번호보다 큰 없는 번호는 뺀다(#410)
            n_over += 1
            continue
        if got is not None:
            picked.append(([key, got[0], got[1], got[2]], st.at))
    truncated = 0
    omitted: Dict[str, int] = {}
    if len(picked) > knobs["max_rows"]:
        # 열림 후보를 먼저 남기고, 그 안과 나머지는 날짜(뱅크 상태 시각) 최신순
        picked.sort(key=lambda ra: (ra[0][1] == ROW_CANDIDATE, ra[1], _key_sort(ra[0][0])), reverse=True)
        for row, _ in picked[knobs["max_rows"]:]:
            omitted[row[1]] = omitted.get(row[1], 0) + 1
        truncated = len(picked) - knobs["max_rows"]
        picked = picked[:knobs["max_rows"]]
    picked.sort(key=lambda ra: _key_sort(ra[0][0]))
    fmt = knobs["table_fmt"]
    rows = [r for r, _ in picked]
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r[1]] = counts.get(r[1], 0) + 1
    lines = [_render_row(r, fmt) for r in rows]
    if truncated:
        parts = ", ".join(f"{s} {omitted[s]}개" for s in ROW_STATES if omitted.get(s))
        lines.append(f"(상한 {knobs['max_rows']}줄: 생략 {parts})")
    header = knobs["header"].format(t=t_day.isoformat(), cols=COLS_3[fmt])
    text = "\n".join([OPEN_TAG, header, LEGEND[fmt], "\n".join(lines), CLOSE_TAG])
    out = {"t": t_day.isoformat(), "fmt": fmt, "rows": rows, "truncated": truncated, "text": text,
           "counts": {s: counts[s] for s in ROW_STATES if s in counts},
           "omitted": {s: omitted[s] for s in ROW_STATES if s in omitted},
           "fetched_at": github_fetched_at(github)}
    if drop_over:
        out["over_max"] = n_over                            # #410: 뺀 줄 수(payload 에는 싣지 않는다)
    return out


def build_table(states: Mapping[str, KeyState], cutoff: datetime, repos: Optional[Set[str]],
                knobs: Mapping[str, Any], *,
                github: Optional[Mapping[str, Mapping[Any, Mapping[str, Any]]]] = None,
                result_keys: Optional[Set[str]] = None, now: Optional[datetime] = None,
                max_numbers: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    """키 상태 -> 구조 필드 `{t, fmt, rows, truncated, text}`. repos 가 None 이면 전체 범위.

    github(#396, 선택 키워드): #395 근거 테이블(`item_roles.load_github` 모양). 주면 3상태 모드(줄 `[키, 줄 상태, 날짜,
    세부]`, 결과에 `counts`·`omitted`·`fetched_at`). None(4인자 호출, 테이블 없음, 손잡이 ignore)이면 지금 모양 그대로
    (줄 `[키, 상태, 날짜]`, 상태 열림/끝남).
    result_keys(#402, 3상태 모드만): 그 요소의 결과 행 키(`result_keys`). 주면 손잡이 `done_closed_rows` 대로 반증 줄을
    넣는다. now(#402, 3상태 모드만): 읽는 시각. 주면 손잡이 `github_max_age_hours` 로 근거 나이를 본다. 둘 다 없으면
    `github=` 호출 결과는 #396 그대로다.
    max_numbers(#410, 3상태 모드만): 리포별 수집 최대 번호(`Store.github_max`, 전체 근거면 `repo_max_numbers`). 주면 손잡이
    `over_max_rows` 가 drop 일 때 over-max 줄을 빼고 결과에 `over_max`(뺀 줄 수)를 더한다. 없으면 판정하지 않는다."""
    if github is not None:
        return _build_table_3state(states, cutoff, repos, knobs, github, result_keys, now, max_numbers)
    t_day = cutoff.astimezone(_KST).date()
    n = knobs["recent_done_days"]
    lo = (t_day - timedelta(days=n - 1)).isoformat() if n > 0 else None
    picked: List[Tuple[str, KeyState]] = []
    for key, st in states.items():
        if repos is not None and key.rpartition("#")[0] not in repos:
            continue
        if st.state == STATE_OPEN or (lo is not None and st.day >= lo):
            picked.append((key, st))
    truncated = 0
    if len(picked) > knobs["max_rows"]:
        picked.sort(key=lambda kv: (kv[1].at, _key_sort(kv[0])), reverse=True)     # 날짜 최신순으로 남긴다
        truncated = len(picked) - knobs["max_rows"]
        picked = picked[:knobs["max_rows"]]
    picked.sort(key=lambda kv: _key_sort(kv[0]))
    fmt = knobs["table_fmt"]
    rows = [[key, st.state, st.day] for key, st in picked]
    lines = [" | ".join(r if fmt == "b" else r[:2]) for r in rows]
    if truncated:
        lines.append(f"(상한 {knobs['max_rows']}줄: 날짜가 오래된 키 {truncated}개 생략)")
    header = knobs["header"].format(t=t_day.isoformat(), cols=COLS[fmt])
    text = "\n".join([OPEN_TAG, header, "\n".join(lines), CLOSE_TAG])
    return {"t": t_day.isoformat(), "fmt": fmt, "rows": rows, "truncated": truncated, "text": text}


def wants_table(args: Mapping[str, Any], knobs: Mapping[str, Any]) -> bool:
    if knobs["attach"] == "always":
        return True
    query = args.get("query")
    if not isinstance(query, str):
        return False
    low = query.lower()
    return any(term.strip().lower() in low for term in knobs["status_terms"])


def aliases_sha256(aliases: Mapping[str, str]) -> str:
    """meta `aliases_sha256` 과 같은 식(#376 과 맞춘 계약): 키·값 소문자 정규화 뒤 별칭 표의
    sha256(json.dumps(표, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))."""
    return _canon_sha256(item_keys._table(aliases))


def _with_table(payload: Any, table: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return None
    return {**payload, FIELD: payload_table(table)}


# --- 응답 읽기·쓰기 --------------------------------------------------------------------------------

async def attach_mcp_body(*, media: Optional[str], body: bytes, recall_calls: Mapping[Any, Any], bank: str,
                          bank_policy: Any, audit: Audit, token: Optional[str], identity: Optional[str]) -> bytes:
    """MCP 응답 바이트(SSE 또는 JSON)의 recall 성공 결과에 열린 키 표를 붙인 새 바이트. 어떤 실패에도 원본 바이트."""
    started = time.monotonic()
    info: Dict[str, Any] = {"transport": "mcp", "elements": 0, "rows": 0, "truncated": 0}

    def record(reason: str) -> None:
        info["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        _audit(audit, token=token, identity=identity, bank=bank, tool="recall", decision=DECISION,
               reason=reason, extra={"item_open_table": info})

    try:
        knobs_obj = load_knobs()
        knobs = knobs_obj.values
        info.update({"knobs_source": knobs_obj.source, "knobs_sha256": knobs_obj.sha256,
                     "table_fmt": knobs["table_fmt"], "attach": knobs["attach"], "scope": knobs["scope"],
                     "timeout_ms": knobs["timeout_ms"], "github_evidence": github_mode(knobs),
                     "done_closed_rows": done_closed_mode(knobs), "github_max_age_hours": github_max_age_hours(knobs),
                     "over_max_rows": over_max_mode(knobs)})
        if knobs_obj.warnings:
            info["knob_warnings"] = list(knobs_obj.warnings)
        store_bank = os.environ.get(ENV_BANK, "").strip()     # 저장소는 한 뱅크 전용(#377-1)
        if not store_bank:
            record("bank-unset")
            return body
        if bank != store_bank:
            record("other-bank")
            return body
        parsed = reframe.parse_response(media, body)
        if parsed.failure is not None or not parsed.frames:
            return body
        rows_by_el = item_status._recall_rows(parsed, set(recall_calls))
        aliases = getattr(bank_policy, "item_repo_aliases", None) or {}
        targets: Dict[Any, Tuple[datetime, Optional[Set[str]], str]] = {}
        skipped_ts = 0
        not_attached: Dict[str, int] = {}
        for el_id, rows in rows_by_el.items():
            args = recall_calls.get(el_id)
            args = args if isinstance(args, Mapping) else {}
            if not wants_table(args, knobs):
                not_attached[NOT_ATTACHED_NO_TERM] = not_attached.get(NOT_ATTACHED_NO_TERM, 0) + 1
                continue
            raw_ts = args.get("query_timestamp")
            cutoff = item_status._instant(raw_ts) if raw_ts else datetime.now(timezone.utc)
            if cutoff is None:                     # 기준 시각을 못 읽으면 그 요소는 건드리지 않는다(S1 과 같다)
                skipped_ts += 1
                continue
            repos, source = table_scope(args, rows, knobs, aliases)
            if source == NOT_ATTACHED_NO_KEYS:
                not_attached[source] = not_attached.get(source, 0) + 1
                continue
            targets[el_id] = (cutoff, repos, source)
        if skipped_ts:
            info["bad_query_timestamp"] = skipped_ts
        if not targets:
            if not_attached:                       # 요소 하나면 사유 문자열, 여럿이면 사유별 개수
                info["not_attached"] = (next(iter(not_attached)) if sum(not_attached.values()) == 1
                                        else dict(sorted(not_attached.items())))
            record("not-attached")
            return body
        path = db_path()
        timeout_s = knobs["timeout_ms"] / 1000.0
        gh_repos = github_read_scope(targets, rows_by_el, aliases, knobs)
        try:
            token_ctx = _GITHUB_SCOPE.set(gh_repos)      # to_thread 가 이 context 를 복사해 읽기 스레드로 넘긴다
            try:
                store = await asyncio.wait_for(asyncio.to_thread(read_store, path, timeout_s), timeout=timeout_s)
            finally:
                _GITHUB_SCOPE.reset(token_ctx)
        except StoreError as exc:
            record(exc.reason)
            return body
        except asyncio.TimeoutError:
            record("timeout")
            return body
        info["store_updated_at"] = store.meta.get("updated_at")
        info["mentions"] = len(store.mentions)
        key_rules = store.key_rules
        info["key_rules"] = key_rules
        if store.meta.get("aliases_sha256") != aliases_sha256(aliases):
            info["aliases_mismatch"] = True
        for el_id in list(targets):                 # v2 저장소면 결과 행 범위를 v2 키로 다시 정한다(#396)
            cutoff, repos, source = targets[el_id]
            repos, source = rescope_for_key_rules((repos, source), rows_by_el.get(el_id), aliases, key_rules)
            if source == NOT_ATTACHED_NO_KEYS_V2:
                not_attached[source] = not_attached.get(source, 0) + 1
                del targets[el_id]
            else:
                targets[el_id] = (cutoff, repos, source)
        if not targets:
            info["not_attached"] = (next(iter(not_attached)) if sum(not_attached.values()) == 1
                                    else dict(sorted(not_attached.items())))
            record("not-attached")
            return body
        github = store.github if github_mode(knobs) == GITHUB_USE else None
        now = None
        if github is not None:
            info["github_fetched_at"] = github_fetched_at(github)
            now = datetime.fromtimestamp(_wall_clock(), timezone.utc)     # 근거 나이는 읽는 시각 기준(#402)
        tables: Dict[Any, Dict[str, Any]] = {}
        for el_id, (cutoff, repos, _) in targets.items():
            keys = result_keys(rows_by_el.get(el_id), aliases, key_rules) if github is not None else None
            tables[el_id] = build_table(role_states(store.mentions, cutoff, key_rules), cutoff, repos, knobs,
                                        github=github, result_keys=keys, now=now,
                                        max_numbers=(store.github_max or {}) if github is not None else None)
        applied: Set[Any] = set()

        def rewrite(el: dict) -> dict:
            el_id = reframe.element_id(el)
            table = tables.get(el_id)
            if table is None:
                return el
            new, changed = item_status.rewrite_tool_result(el.get("result"), lambda p: _with_table(p, table))
            if not changed:
                return el
            applied.add(el_id)
            return {**el, "result": new}

        rewritten = reframe.transform_results(parsed, rewrite)
        _, new_body, serialize_failure = reframe.try_serialize(rewritten)
        if rewritten.failure or serialize_failure:
            record("failed:rewrite")
            return body
        if not applied:
            record("not-attached")
            return body
        info["elements"] = len(applied)
        info["rows"] = sum(len(tables[e]["rows"]) for e in applied)
        info["truncated"] = sum(tables[e]["truncated"] for e in applied)
        if github is not None:                      # 3상태 모드: 상태별 줄 수, 미확인 사유별 줄 수(#396)
            counts: Dict[str, int] = {}
            reasons: Dict[str, int] = {}
            for e in applied:
                for row in tables[e]["rows"]:
                    counts[row[1]] = counts.get(row[1], 0) + 1
                    if row[1] == ROW_UNKNOWN:
                        reasons[row[3]] = reasons.get(row[3], 0) + 1
                if tables[e].get("over_max"):                 # #410: 뺀 over-max 줄 수
                    reasons[REASON_OVER_MAX] = reasons.get(REASON_OVER_MAX, 0) + tables[e]["over_max"]
            info["counts"] = {s: counts[s] for s in ROW_STATES if s in counts}
            info["unknown_reasons"] = dict(sorted(reasons.items()))
        scope_from: Dict[str, int] = {}
        for e in applied:
            scope_from[targets[e][2]] = scope_from.get(targets[e][2], 0) + 1
        info["scope_from"] = dict(sorted(scope_from.items()))
        record("attached")
        return new_body
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - 표 실패가 recall 실패가 되면 안 된다
        record(f"failed:{type(exc).__name__}")
        return body
