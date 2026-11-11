"""retain 게이트 작업 키 검사 S2 (issue #364, 아키텍처 v2 런 5 R5-B2, 기능 플래그 기본 off).

왜: 작업 키 없이 저장된 인계·할 일·완료 기억은 키 기반 처치(X1 recall 보강, B 무효화, S1 원장)가 닿지 못한다.
런 3 기준 끝난 항목의 절반이 키가 없었다. 쓰는 시점에 키를 요구하면 그 뒤로 저장되는 기억은 처치 범위에 든다.

무엇을(`HC_RETAIN_ITEM_KEY`, 값 `off`(기본) / `warn` / `reject`, 대소문자·앞뒤 공백 무시, 그 밖의 값은 off):
  검사 대상 retain 항목(아래 셋 중 하나)에서 `item_keys.extract_keys(content, 교정 뒤 tags, 뱅크 item_repo_aliases)`가
  비면
    * warn: `ItemVerdict.warnings` 에 경고 한 줄을 싣고 저장은 그대로 한다(MCP 성공 응답 본문, REST `gateway_warnings`).
    * reject: 위반 `RULE:item-key-required` 로 거부한다(MCP isError, REST 422 `rule-violation`). 다른 위반과 함께 실린다.
  검사 대상:
    1. 보낸 태그(교정 전)에 `kind:todo` 가 있다. 템플릿 별칭이 `kind:todo` 를 `kind:decision` 으로 바꾸므로 교정 뒤
       태그로는 가릴 수 없다. 보낸 태그는 소문자·앞뒤 공백만 맞춰 본다(`item_keys._tag_set`).
    2. 교정 뒤 태그에 `kind:handoff` 또는 `kind:completion` 이 있다(`kind:done` -> `kind:completion` 같은 별칭 반영).
    3. 교정 뒤 태그에 `kind:decision` 이 있고 맨 앞 괄호 라벨이 보류 계열이다(`item_keys._pending_label`, B 무효화
       대상 규칙과 같은 판정).
  `kind:status` 는 #360 결정의 S2 범위(handoff·todo·completion)에 없어 대상이 아니다.

판정은 `rules.evaluate_item` 한 곳에서 돈다. MCP retain 계열(`retain`·`sync_retain`)과 REST `POST .../memories`
`items[i]` 가 같은 함수를 거치므로 두 표면이 같은 결과를 낸다. 규칙뿐이다(LLM·네트워크 없음, 표준 라이브러리 +
item_keys). 플래그는 호출마다 읽는다(item_status 와 같은 방식, 테스트가 env 로 켠다).
off 면 이 모듈은 판정 함수를 부르지 않고 `rules.evaluate_item` 결과는 S2 이전과 바이트까지 같다(테스트로 고정).
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from . import item_keys

ENV_FLAG = "HC_RETAIN_ITEM_KEY"
MODE_OFF, MODE_WARN, MODE_REJECT = "off", "warn", "reject"
RULE_ID = "RULE:item-key-required"

_TODO = "kind:todo"
_HANDOFF = "kind:handoff"
_DECISION = "kind:decision"


def mode() -> str:
    """현재 모드. `warn`·`reject` 가 아니면 전부 off(기본, 오타도 off 라 켜지 않는 쪽으로 실패한다)."""
    value = os.environ.get(ENV_FLAG, MODE_OFF).strip().lower()
    return value if value in (MODE_WARN, MODE_REJECT) else MODE_OFF


def trigger(sent_tags: Any, stored_tags: Any, content: Any) -> Optional[str]:
    """검사 대상이면 그 사유(사람이 읽는 태그 표기), 아니면 None. 순서는 모듈 머리 주석의 1-2-3."""
    if _TODO in item_keys._tag_set(sent_tags):
        return _TODO
    stored = item_keys._tag_set(stored_tags)
    for kind in (_HANDOFF, item_keys.KIND_COMPLETION):
        if kind in stored:
            return kind
    if _DECISION in stored and isinstance(content, str) and item_keys._pending_label(content):
        return "kind:decision with a pending label"
    return None


def missing_key(sent_tags: Any, stored_tags: Any, content: Any,
                aliases: Optional[Mapping[str, str]]) -> Optional[str]:
    """검사 대상인데 작업 키가 하나도 없으면 사유, 아니면 None."""
    why = trigger(sent_tags, stored_tags, content)
    if why is None or item_keys.extract_keys(content, stored_tags, aliases):
        return None
    return why


def _project_slug(stored_tags: Any) -> str:
    projects = sorted(t.split(":", 1)[1] for t in item_keys._tag_set(stored_tags)
                      if t.startswith("project:") and t.split(":", 1)[1])
    return projects[0] if len(projects) == 1 else "<repo>"


def item_tag_allowed(tag_prefixes: Any) -> bool:
    """`item:<repo>-<n>` 태그를 안내해도 되는가: 뱅크가 접두어를 선언하지 않았거나 `item` 을 선언했다.
    선언하지 않은 뱅크에 그 태그를 안내하면 다음 시도가 RULE:tags-grammar 로 거부된다(rules 의 예시 원칙)."""
    return not tag_prefixes or "item" in tuple(tag_prefixes)


_EXAMPLE_LABELS = {_HANDOFF: "handoff", item_keys.KIND_COMPLETION: "completion"}


def example_kind(why: str, tag_aliases: Optional[Mapping[str, str]]) -> str:
    """Valid example 에 쓸 kind: 계기 kind 의 교정 결과(final-review #364-1 #5). 완료 기억이 handoff 예시를
    베껴 극성이 뒤집히지 않게 한다. todo 는 뱅크 별칭(템플릿은 kind:decision)을 따르고, 보류 decision 은 decision."""
    if why == _TODO:
        table = tag_aliases if isinstance(tag_aliases, Mapping) else {}
        return table.get(_TODO, _TODO)
    if why.startswith(_DECISION):
        return _DECISION
    return why


def violation_message(why: str, stored_tags: Any, item_tag: bool = True,
                      kind: Optional[str] = None) -> str:
    """거부 문구(모델이 고쳐 다시 보낼 수 있게: 무엇이 틀렸나 + 키로 치는 표기 + 유효한 예시).
    `kind` 는 예시에 쓸 kind(`example_kind`), 없으면 kind:handoff. todo·decision 예시는 보류 계열 라벨 `[todo]`."""
    repo = _project_slug(stored_tags)
    kind = kind or _HANDOFF
    label = _EXAMPLE_LABELS.get(kind, "todo")
    tag_form = ', or add the tag "item:<repo>-<n>"' if item_tag else ""
    return (
        f"{RULE_ID} this retain carries {why}, so it must name the work item it is about, but no "
        f"item key was found in \"content\" or \"tags\". Name the issue in content as \"<owner>/<repo>#<n>\", "
        f"or as \"<repo>#<n>\" where <repo> is this memory's own project: tag name (another word before "
        f"\"#\" is not read as a repo){tag_form}; with exactly one project: tag a bare \"#<n>\" also "
        f"counts. URLs and fenced code blocks are not read. "
        f"Valid example: \"content\": \"[{label}] {repo if repo != '<repo>' else 'hindsight-crew'}#123 "
        f"next step for the next session\", "
        f"\"tags\": [\"project:{repo if repo != '<repo>' else 'hindsight-crew'}\", \"{kind}\"]."
    )


def warning_text(why: str, stored_tags: Any, item_tag: bool = True) -> str:
    """경고 문구(저장은 진행했다는 사실 + 다음부터 붙일 표기)."""
    repo = _project_slug(stored_tags)
    tag_form = f" 태그 \"item:{repo}-<번호>\"를 붙이거나" if item_tag else ""
    return (
        f"[gateway 경고] {RULE_ID} {why} 기억인데 작업 키가 없습니다(본문·태그에서 이슈 번호를 찾지 못함)."
        f"{tag_form} 본문에 \"<owner>/<repo>#<번호>\" 또는 \"{repo}#<번호>\"(이 기억의 project 태그 이름)를 쓰세요"
        f"(다른 낱말 뒤 \"#\"은 repo로 읽지 않습니다. project 태그가 하나면 \"#<번호>\"도 됩니다). 저장은 그대로 진행했습니다."
    )
