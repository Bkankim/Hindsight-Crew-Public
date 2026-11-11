# 지식 베이스 읽기 도구 fixture (합성, issue #302)

`kb_tree_ok`·`kb_search_ok`·`kb_page_ok`(`.raw`/`.headers`)는 **채집한 원문이 아니라 합성 fixture**다.
사본(hc-next)을 띄우지 않고 만들었다(#302 범위 규칙).

- 응답 모양 출처: upstream v1.1.0 `hindsight_api/mcp_tools.py`의 `_do_get_knowledge_base_tree`·`_do_search_knowledge_base`·`_do_get_knowledge_page`와
  `_knowledge_node_json`. 최상위 키는 각각 `roots` / `results`+`total` / `id,name,type,description,tags,timestamp,markdown`이다.
  `search_knowledge_base` 결과 행의 세부 키(`id,name,score,snippet`)는 소스에서 확인하지 못한 추정이다.
- 프레임: `get_memory_ok.raw`(v1.1.0 실측)와 같은 SSE 프레임, `-> str` 변형(bank_id 인자를 받는 쪽)이라 본문이 indent 2 + `ensure_ascii` 기본의 `\uXXXX` 문자열이고 `structuredContent.result`에 같은 문자열이 한 벌 더 있다.
  헤더 파일은 `get_memory_ok.headers`를 그대로 복사했다.
- 내용(페이지 이름·본문)은 전부 지어낸 값이고 사적 맥락이 없다.
- 이 fixture가 증명하는 것은 게이트웨이의 허용·거부·감사·바이트 무변조 경로다. 실제 응답 모양과의 일치는 사본 채집(`scripts/mcp-probe.sh` 확장)으로 따로 확인해야 한다.

## 한글 `\uXXXX` (rework #302-1)

세 fixture의 본문은 한글이 전부 `\uXXXX`로 이스케이프된 상태다(`get_memory_ok.raw`와 같은 `-> str` 변형 모양을 따른 합성). `gateway/unescape.py`의 `TOOLS`에 KB 읽기 3개를 더한 시험(`tests/test_kb_read_tools.py`)이 이 합성 원본을 쓴다. 실제 v1.1.0 KB 읽기 응답이 이스케이프로 나오는지는 사본 채집으로 따로 확인해야 한다.
