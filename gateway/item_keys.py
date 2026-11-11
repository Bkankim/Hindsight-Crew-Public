"""작업 키 추출과 무효화 대상 판정 - 순수 함수 모듈 (issue #320, 아키텍처 v2 런 2 의 B 처치).

배경: 끝난 일을 알리는 `kind:completion` 기억과 그 일의 인계·할 일 기억을 잇는 키가 없어서, 통합이 만든
관찰이 "아직 안 끝남"을 현재 상태로 되돌린다. B 처치(작업 키 무효화)는 완료 기억이 들어오면 같은 작업 키를
가진 낡은 인계·할 일 기억을 upstream 의 무효화(`PATCH /memories/{id} {"state": "invalidated"}`)로 내린다.
upstream v1.1.0 `api/memories.mdx` 178-179행: 무효화는 그 기억에서 파생된 관찰을 지우고 같은 출처를 공유하던
관찰을 다시 계산한다. 그래서 관찰(observation)은 후보가 아니고, 원문(world/experience)만 후보다.

이 모듈은 규칙만 갖는다. 게이트웨이 훅(#321)과 사본 backfill(#322)이 같은 규칙을 쓰도록 한 곳에 둔다.
  * 네트워크·LLM·임베딩·upstream 호출이 없다(D8: 게이트웨이는 LLM 판정 없이 표시와 수신만, #322 결정성).
    표준 라이브러리만 import 한다(tests/test_item_keys.py 가 import 목록을 고정한다).
  * 리포 별칭 표 `aliases`({별칭 소문자: 정규 repo slug})는 인자로만 받는다. 표의 출처는 뱅크 정책 키
    `item_repo_aliases`(gateway/bank_policies.py)다. 패턴은 이 표와 GitHub 번호 문법만으로 정했다.

키 정규형: `(<repo>, <n>)`. repo 는 GitHub 리포 이름 소문자 slug `[a-z0-9-]+`, n 은 1 이상 정수(7자리까지).
태그 표기는 `item:<repo>-<n>`(예 `item:hindsight-crew-251`). 태그에서는 마지막 `-<숫자>`가 번호다.

extract_keys 가 키로 치는 것(content 는 `[라벨] 본문` 한 줄 기억의 본문 전체):
  1. 태그 `item:<repo>-<n>` (repo 는 별칭이면 정규 repo 로).
  2. 알려진 repo 이름 바로 뒤의 번호: `<repo>#<n>`, `<repo> #<n>`(공백 정확히 1개). 알려진 repo 이름은 별칭 표의
     키와 값(대소문자 무시, 예 `HC#251`)과 이 기억의 `project:` 태그 slug 다. 표에 없는 임의 단어(`PR#12`,
     `issue #251`)는 repo 로 치지 않는다 - 서로 다른 리포의 같은 번호가 가짜 키로 맞아떨어지는 것을 막는다.
  3. GitHub 교차 참조 문법 `<owner>/<repo>#<n>`은 표에 없는 repo 라도 키다(repo 가 글자로 적혀 있다). 이 문법은
     공백을 허용하지 않는다(`CI/CD #251` 같은 슬래시 낱말은 교차 참조가 아니다).
  4. 번호만 `#<n>`: 기억의 `project:` 태그가 (별칭으로 정규화한 repo 기준) 정확히 하나일 때 그 slug 가 repo 다.
     project 태그가 없거나 서로 다른 repo 둘 이상이면 키로 치지 않는다.
  숫자 경계: `#` 앞에 영숫자·`_`·`/`·`&`·`#`이 붙어 있거나 번호 뒤에 영숫자·`_`가 붙으면 키가 아니다
  (`#25` 가 `#2511` 에 걸리지 않고, `xhc#251`·`&#251;`·`#251a`·16진 색상은 걸리지 않는다). 앞뒤가 한글·공백·
  구두점이면 경계다(`이슈#251`, `(#251)`).
  코드 블록·URL 안 번호는 키가 아니다(정책 고정, 테스트로 고정): URL(`/pull/251`, `/issues/251`, `#251` 조각)과
  펜스 코드 블록(``` 또는 ~~~, 닫지 않으면 끝까지)은 본문에서 지우고 본다. 근거(링크·로그·diff 는 작업 대상의
  선언이 아니라 인용이다)로 오탐을 줄이는 쪽(무효화는 되돌리기 비싸다)을 골랐다. 인라인 코드(`` `#251` ``)는
  강조 표기일 뿐이라 키로 친다. 태그 `item:` 은 명시 선언이라 항상 키다.

is_target 의 무효화 대상 규칙(모두 참이어야 True):
  1. 후보 `fact_type`(없으면 `type`)이 world 또는 experience 이고 `state` 가 정확히 `valid` 다. state 가 없으면
     대상이 아니다(무효화된 행과 구별할 수 없다). 호출자는 state 를 실어 보내야 한다(memories/list 는 싣는다).
  2. 후보에 `kind:completion` 이 없다.
  3. 완료와 후보가 같은 키를 하나 이상 공유하고, 같은 `project:` 태그를 하나 이상 공유한다.
  4. 후보의 kind 가 `kind:handoff`·`kind:status`·`kind:todo`(옛 태그)이거나, `kind:decision` 이면서 맨 앞 괄호
     라벨이 보류·대기·미완(미완료·미해결 포함)·할 일·todo·pending 류다. 라벨 안의 결과 낱말 중 맨 뒤 것이
     판정한다: 맨 뒤가 해제·해소·해결·완료·취소·재개·종료·끝이면 풀린 것(`보류 해제`)이라 대상이 아니고,
     `재개 대기`·`완료 대기` 는 대상이다. 낱말은 통째로 맞아야 한다(`대기업` 은 `대기` 가 아니다). 게이트웨이 별칭이 `kind:todo` 를
     `kind:decision` 으로 바꾸기 때문이다(템플릿 tag_aliases). 라벨 판정은 gateway/contradiction.py 처럼
     라벨만 보고 본문은 읽지 않는다(contradiction 은 httpx 를 끌어오므로 라벨 정규식만 같은 모양으로 둔다).
  5. 후보 `mentioned_at` 이 완료의 것보다 엄격히 이르고(시각으로 비교, 오프셋 없는 값은 UTC, 읽을 수 없거나
     없으면 False), 같은 `document_id` 가 아니다(둘 중 하나라도 없으면 같은 문서로 보지 않는다).
  "완료 기억인가"는 호출자가 정한다(`is_completion` 은 편의 함수다). 이 모듈은 completion 인자의 kind 를 검사하지
  않는다 - 훅이 정규화 전 태그를 넘겨도 되도록 하려는 것이다.

over_cap / find_targets: 한 완료가 MAX_TARGETS_PER_COMPLETION 보다 많은 대상을 만나면 넓은 키 오탐으로 보고
아무것도 무효화하지 않는다(`TargetResult.invalidate` 가 빈 튜플). 대상 목록 자체는 dry-run 표본을 위해 돌려준다.

알려진 한계(고치지 않기로 함, 근거는 커밋 trailer):
  * 번호만 규칙은 `<단어> #<n>`(알려진 repo 가 아닌 단어 뒤 공백 1개)의 번호도 기억의 project 키로 친다. `PR #251`·
    `issue #251` 같은 같은 리포 표기를 살리려는 것이고, 다른 리포 번호(`kit #251`)가 섞이는 피해는 같은 project +
    같은 키 + over_cap 상한이 묶는다.
  * 별칭 체인(`{"hc": "a", "a": "b"}`)은 로드 때 거부하지 않는다. 한 홉만 따라가므로 같은 리포가 표기에 따라 다른 키로
    갈라질 수 있다. 운영 표에 체인이 없고 자기 매핑 예시(`hindsight-crew: hindsight-crew`)와 충돌해서 검사를 두지 않았다.

RULE_VERSION: 위 규칙(키 패턴, 대상 조건, 상한)을 바꾸면 올린다. #322 의 LINEAGE 와 #321 의 감사 로그가 이 값을
남겨서, 어느 규칙으로 무효화했는지 나중에 가를 수 있다.

키 규칙 v2 (issue #395, R7-1, RULE_VERSION_V2): `extract_keys_v2`·`key_hits_v2` 만 쓴다. `extract_keys`·`is_target`·
RULE_VERSION 은 v1 그대로다(무효화 훅·S1·런 1~6 재현이 v1 에 기댄다). v2 는 v1 위에 세 가지만 더한다.
  * 범위 표기 전개: `#a~#b`·`#a-#b`·`#a..#b`·`#a ~ #b`. 범위 앞에 붙은 접두(알려진 repo 이름, `owner/repo`, 공백 1개
    뒤 알려진 repo)는 중간 번호에도 같이 붙인 것으로 본다. 한 범위가 RANGE_CAP(20)개를 넘거나 a >= b 면 전개하지 않는다
    (그러면 v1 과 같이 양 끝만 키). 알려진 repo 가 아닌 영숫자 낱말이 `#` 에 바로 붙은 범위(`xhc#10~#12`)도 v1 그대로 둔다.
  * PR 표기의 번호는 작업 키가 아니다: `PR #N`·`PR#N`·`PR N`·`pull #N`·`pull request #N`(대소문자 무시, 뒤 범위 포함).
    같은 문장의 다른 번호는 그대로 키다.
  * 업스트림·외부 리포 문맥: 문장에 CONTEXT_WORDS 낱말이 있으면 그 문장에서 번호만 규칙으로 얻은 키는 `ref_fixed`
    표시를 단다(키는 그대로 낸다). 명시 repo 표기(`<repo>#n`, `owner/repo#n`)와 태그 키는 표시하지 않는다.
  문장은 gateway/item_roles.py 와 같은 규칙(펜스 코드 블록을 지운 뒤 줄바꿈과 `.`·`!`·`?`·`。` 뒤 공백)으로 나눈다.
  리포별 관측 최대 번호(의심 키)는 이 모듈이 아니라 item_roles.py 가 `key_hits_v2` 의 `source` 로 센다.

키 규칙 v2.1 (issue #410, R7-5, RULE_VERSION_V21): `extract_keys_v21`·`key_hits_v21` 만 쓴다. v1·v2 함수는 그대로다
(런 7 단계 A·B 재현과 기존 v2 저장소가 v2 에 기댄다). v2 위에 열거 접두 전파와 문장 단일 명시 리포 두 가지만 더한다
(규칙은 `key_hits_v21` 위 주석).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, FrozenSet, Iterable, Mapping, Optional, Sequence, Tuple, Union

RULE_VERSION = "item-keys-v1"
MAX_TARGETS_PER_COMPLETION = 10

ItemKey = Tuple[str, int]

KIND_COMPLETION = "kind:completion"
_PENDING_KINDS = frozenset({"kind:handoff", "kind:status", "kind:todo"})
_DECISION_KIND = "kind:decision"
_CANDIDATE_FACT_TYPES = frozenset({"world", "experience"})

# 1 이상, 최대 7자리(GitHub 번호 상한보다 넉넉하다). 앞자리 0 은 번호가 아니다.
_NUM = r"([1-9][0-9]{0,6})"
_AFTER = r"(?![0-9A-Za-z_])"                       # 번호 뒤 경계
_ITEM_TAG_RE = re.compile(r"^item:([a-z0-9-]+)-" + _NUM + r"$")
_PROJECT_TAG_PREFIX = "project:"
_BARE_RE = re.compile(r"(?<![0-9A-Za-z_/&#])#" + _NUM + _AFTER)
_OWNER_RE = re.compile(
    r"(?<![0-9A-Za-z_./-])[A-Za-z0-9][A-Za-z0-9_.-]*/([A-Za-z0-9][A-Za-z0-9_.-]*)#" + _NUM + _AFTER)
# 여는 펜스(백틱·틸데 3개 이상)와 같은 길이 이상의 같은 문자가 나올 때까지(안쪽의 더 짧은 펜스는 닫힘이 아니다)
_FENCE_RE = re.compile(r"(`{3,}|~{3,}).*?(?:(?<![`~])\1|\Z)", re.S)
# URL 은 공백·쉼표·세미콜론에서 끝난다(`.../pull/250,#251` 의 뒤쪽 번호까지 지우지 않는다)
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s,;]+")
_LABEL_RE = re.compile(r"^\s*\[([^\]]{1,24})\]")  # gateway/contradiction.py 와 같은 모양
# 라벨 안의 결과 낱말. 왼쪽에서 오른쪽으로 읽어 맨 뒤 낱말이 판정한다(contradiction 의 "마지막 결과가 이긴다"와
# 같은 방식): `보류 해제` 는 풀린 것, `재개 대기`·`완료 대기` 는 아직 기다리는 것. 미완료·미해결은 pending 쪽이
# 먼저 먹어서 그 안의 완료·해결이 해제류로 읽히지 않는다. `대기업` 은 `대기` 가 아니다.
_LABEL_WORD_RE = re.compile(
    r"(?P<pending>보류|대기(?!업)|미완료|미완|미해결|할\s*일|(?<![a-z0-9_])(?:todo|to-do|pending)(?![a-z0-9_]))"
    r"|(?P<resolved>해제|해소|해결|완료|취소|재개|종료|(?<![가-힣])끝(?![가-힣]))")


# --- 별칭 표·태그 도우미 -------------------------------------------------------------------

def _table(aliases: Optional[Mapping[str, str]]) -> dict:
    """호출자 표를 소문자로 읽은 사본. 문자열이 아닌 항목은 건너뛴다(표는 로드 때 검증된다)."""
    if not isinstance(aliases, Mapping):
        return {}
    return {k.strip().lower(): v.strip().lower() for k, v in aliases.items()
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}


def _slug(name: str) -> str:
    """게이트웨이 태그 정규화(rules._normalize_tag)와 같은 slug 규칙."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", name.lower())).strip("-")


def _canonical(name: str, table: Mapping[str, str]) -> str:
    low = name.strip().lower()
    if low in table:
        return table[low]
    slug = _slug(low)
    return table.get(slug, slug)


def _tag_set(tags: Any) -> FrozenSet[str]:
    if isinstance(tags, (str, bytes)) or not isinstance(tags, Iterable):
        return frozenset()
    return frozenset(t.strip().lower() for t in tags if isinstance(t, str) and t.strip())


def _project_repos(tags: FrozenSet[str], table: Mapping[str, str]) -> FrozenSet[str]:
    return frozenset(
        c for c in (_canonical(t[len(_PROJECT_TAG_PREFIX):], table) for t in tags
                    if t.startswith(_PROJECT_TAG_PREFIX)) if c)


@lru_cache(maxsize=64)
def _known_repo_re(names: Tuple[str, ...]) -> "re.Pattern[str]":
    alt = "|".join(re.escape(n) for n in sorted(names, key=lambda n: (-len(n), n)))
    return re.compile(r"(?<![0-9A-Za-z_./-])(?:" + alt + r")[ ]?#" + _NUM + _AFTER, re.I)


def _mask_quotes(content: str) -> str:
    """URL 과 펜스 코드 블록을 공백 하나로 바꾼다(경계는 남기고 그 안의 번호는 지운다)."""
    return _URL_RE.sub(" ", _FENCE_RE.sub(" ", content))


# --- 키 추출 ---------------------------------------------------------------------------------

def extract_keys(content: Any, tags: Any, aliases: Optional[Mapping[str, str]]) -> FrozenSet[ItemKey]:
    """기억 하나(본문 `content`, 태그 `tags`)가 가리키는 작업 키 집합 `{(repo, n)}`. 모듈 머리 주석의 규칙."""
    table = _table(aliases)
    tag_set = _tag_set(tags)
    keys = set()

    for tag in tag_set:
        m = _ITEM_TAG_RE.match(tag)
        repo = _canonical(m.group(1), table) if m else ""
        if repo:                                    # `item:--251` 의 빈 repo 는 키가 아니다
            keys.add((repo, int(m.group(2))))

    if not isinstance(content, str) or "#" not in content:
        return frozenset(keys)
    text = _mask_quotes(content)

    projects = _project_repos(tag_set, table)
    names = set(table) | set(table.values()) | projects
    names.discard("")

    def take_known(m: "re.Match[str]") -> str:
        keys.add((_canonical(m.group(0)[: m.group(0).rindex("#")], table), int(m.group(1))))
        return " "                                  # 같은 번호를 번호만 규칙이 다시 세지 않게 지운다

    def take_owner(m: "re.Match[str]") -> str:
        repo = _canonical(m.group(1), table)
        if repo:
            keys.add((repo, int(m.group(2))))
        return " "

    if names:
        text = _known_repo_re(tuple(sorted(names))).sub(take_known, text)
    text = _OWNER_RE.sub(take_owner, text)
    if len(projects) == 1:
        (repo,) = projects
        for m in _BARE_RE.finditer(text):
            keys.add((repo, int(m.group(1))))
    return frozenset(keys)


# --- 키 규칙 v2 (issue #395) ------------------------------------------------------------------------

RULE_VERSION_V2 = "item-keys-v2"
RANGE_CAP = 20
CONTEXT_WORDS = ("upstream", "업스트림", "mem0", "외부 리포", "외부 저장소", "다른 리포", "타 리포")
SOURCE_TAG, SOURCE_KNOWN, SOURCE_OWNER, SOURCE_BARE = "tag", "known", "owner", "bare"

_SENT_SPLIT_RE = re.compile(r"\n+|(?<=[.!?。])\s+")           # gateway/item_roles.py 와 같은 문장 경계
# `(?!트)`: '다른 리포트'·'기타 리포트'의 '리포트'(report)를 리포 문맥으로 잡지 않는다(final-review C7).
_CONTEXT_RE = re.compile("|".join(re.escape(w).replace(r"\ ", r"\s*") + "(?!트)" for w in CONTEXT_WORDS), re.I)
_RANGE_SEP = r"(?:[ ]?~[ ]?|-|\.\.)"
_PR_RE = re.compile(
    r"(?<![0-9A-Za-z_])(?:pull[ ]requests?|pulls?|prs?)[ ]?(?:#[ ]?)?" + _NUM + _AFTER
    + r"(?:" + _RANGE_SEP + r"#?" + _NUM + _AFTER + r")?", re.I)
_RANGE_RE = re.compile(r"(?<![&#])#" + _NUM + _RANGE_SEP + r"#" + _NUM + _AFTER)
_PREFIX_RE = re.compile(r"([A-Za-z0-9_./-]+)([ ]?)$")


@dataclass(frozen=True)
class KeyHit:
    """v2 키 언급 하나. source = tag|known|owner|bare, sent = 본문 문장 번호(태그는 None), ref_fixed = 참조 고정."""

    key: ItemKey
    source: str
    sent: Optional[int]
    ref_fixed: bool


def _expand_ranges(text: str, names: FrozenSet[str]) -> str:
    """상한 안 범위를 낱개 번호로 다시 쓴다. 접두가 알려진 repo·owner/repo 면 중간 번호에도 붙인다."""

    def rep(m: "re.Match[str]") -> str:
        a, b = int(m.group(1)), int(m.group(2))
        if a >= b or b - a + 1 > RANGE_CAP:
            return m.group(0)
        pm = _PREFIX_RE.search(m.string, 0, m.start())
        prefix = ""
        if pm:
            token, space = pm.group(1), pm.group(2)
            if token.lower() in names or (not space and "/" in token.strip("/")):
                prefix = token + space
            elif not space:
                return m.group(0)           # 알려진 repo 가 아닌 낱말에 붙은 범위(`xhc#10~#12`)는 v1 그대로
        return "#%d" % a + "".join(" %s#%d" % (prefix, n) for n in range(a + 1, b + 1))

    return _RANGE_RE.sub(rep, text)


def key_hits_v2(content: Any, tags: Any, aliases: Optional[Mapping[str, str]]) -> Tuple[KeyHit, ...]:
    """v2 규칙의 키 언급 목록(태그 먼저, 그다음 문장 순). 같은 문장의 같은 (키, source, 표시)는 한 번만 낸다."""
    table = _table(aliases)
    tag_set = _tag_set(tags)
    out: list = []
    seen: set = set()

    def add(hit: KeyHit) -> None:
        if (hit.key, hit.source, hit.sent, hit.ref_fixed) not in seen:
            seen.add((hit.key, hit.source, hit.sent, hit.ref_fixed))
            out.append(hit)

    for tag in sorted(tag_set):
        m = _ITEM_TAG_RE.match(tag)
        repo = _canonical(m.group(1), table) if m else ""
        if repo:
            add(KeyHit((repo, int(m.group(2))), SOURCE_TAG, None, False))
    if not isinstance(content, str) or "#" not in content:
        return tuple(out)

    projects = _project_repos(tag_set, table)
    names = set(table) | set(table.values()) | projects
    names.discard("")
    frozen_names = frozenset(names)
    known_re = _known_repo_re(tuple(sorted(names))) if names else None
    bare_repo = next(iter(projects)) if len(projects) == 1 else None

    sentences = [s for s in _SENT_SPLIT_RE.split(_FENCE_RE.sub(" ", content)) if s and s.strip()]
    for i, sentence in enumerate(sentences):
        if "#" not in sentence:
            continue
        ctx = bool(_CONTEXT_RE.search(sentence))
        text = _URL_RE.sub(" ", sentence)
        text = _PR_RE.sub(" ", text)
        text = _expand_ranges(text, frozen_names)

        def take_known(m: "re.Match[str]", i: int = i) -> str:
            add(KeyHit((_canonical(m.group(0)[: m.group(0).rindex("#")], table), int(m.group(1))),
                       SOURCE_KNOWN, i, False))
            return " "

        def take_owner(m: "re.Match[str]", i: int = i) -> str:
            repo = _canonical(m.group(1), table)
            if repo:
                add(KeyHit((repo, int(m.group(2))), SOURCE_OWNER, i, False))
            return " "

        if known_re is not None:
            text = known_re.sub(take_known, text)
        text = _OWNER_RE.sub(take_owner, text)
        if bare_repo is not None:
            for m in _BARE_RE.finditer(text):
                add(KeyHit((bare_repo, int(m.group(1))), SOURCE_BARE, i, ctx))
    return tuple(out)


def extract_keys_v2(content: Any, tags: Any, aliases: Optional[Mapping[str, str]]) -> FrozenSet[ItemKey]:
    """v2 규칙의 키 집합(참조 고정 키도 포함한다 - 키는 내되 역할을 ref 로 고정하는 것은 item_roles 몫)."""
    return frozenset(h.key for h in key_hits_v2(content, tags, aliases))


# --- 키 규칙 v2.1 (issue #410, R7-5) ------------------------------------------------------------------------
#
# v2 위에 두 가지만 더한다(v2 의 문장 나누기·URL 마스킹·PR 표기 제외·범위 전개·업스트림 문맥 표시는 그대로 쓴다).
#   * 열거 접두 전파: 명시 repo 표기(`<repo>#n`·`<repo> #n`·`owner/repo#n`) 바로 뒤에 구분자(`·`·`,`·`/`·공백)와 번호 바로
#     뒤 괄호 설명(`(...)`, 안에 `#`·괄호 없음)만 사이에 두고 이어지는 `#N` 은 그 접두 repo 키다(source enum, 참조 고정
#     아님: v2 범위 전개의 "접두는 중간 번호에도"와 같이 접두가 붙은 것으로 본다). 낱말·다른 명시 접두·문장 끝에서 멈춘다.
#     괄호 안에 업스트림 문맥 낱말(CONTEXT_WORDS)·알려진 repo 이름(별칭 포함)·`owner/repo` 가 있으면 그 괄호에서 멈춘다
#     (rework #410-1: 리포 맥락이 바뀐 괄호 뒤 번호는 아래 문장 단일 규칙·업스트림 참조 고정이 판정한다).
#   * 문장 단일 명시 리포: 위 둘을 뺀 접두 없는 `#N` 은 그 문장의 명시 repo(정규 repo 기준)가 정확히 하나면 그 repo 키
#     (source sentence), 둘 이상이면 키가 아니다. 업스트림 문맥 문장이면 참조 고정이다(접두 없는 번호라 v2 와 같다).
#     명시 repo 가 없으면 v2 번호만 규칙 그대로(project 태그가 정확히 하나일 때 그 repo, source bare).
# 결과 repo 집합(참조 고정 제외)은 v2 의 부분집합이다: 명시 표기 repo 는 두 판본이 같고, v2.1 이 더하는 키는 그 repo 로만
# 붙는다(gateway/item_open_table.py 의 근거 읽기 범위가 v2 결과 repo 로 미리 정해도 모자라지 않다).

RULE_VERSION_V21 = "item-keys-v2.1"
SOURCE_ENUM, SOURCE_SENTENCE = "enum", "sentence"

_ENUM_ITEM = r"#" + _NUM + _AFTER
_ENUM_TAIL = r"((?:(?:[\s·,/]|\([^()#\n]*\))+" + _ENUM_ITEM + r")*)"
_ENUM_NUM_RE = re.compile(r"#([1-9][0-9]{0,6})")
_OWNER_ENUM_RE = re.compile(_OWNER_RE.pattern + _ENUM_TAIL)
_PAREN_RE = re.compile(r"\(([^()#\n]*)\)")
_PAREN_OWNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9]")


@lru_cache(maxsize=64)
def _name_word_re(names: Tuple[str, ...]) -> "re.Pattern[str]":
    alt = "|".join(re.escape(n) for n in sorted(names, key=lambda n: (-len(n), n)))
    return re.compile(r"(?<![0-9A-Za-z_./-])(?:" + alt + r")(?![0-9A-Za-z_-])", re.I)


def _enum_cut(tail: str, names: Tuple[str, ...]) -> int:
    """열거 꼬리에서 리포 맥락이 바뀌는 첫 괄호의 시작 위치(없으면 len(tail)). 그 앞까지만 접두 리포로 붙인다."""
    name_re = _name_word_re(names) if names else None
    for m in _PAREN_RE.finditer(tail):
        inner = m.group(1)
        if (_CONTEXT_RE.search(inner) or _PAREN_OWNER_RE.search(inner)
                or (name_re is not None and name_re.search(inner))):
            return m.start()
    return len(tail)


@lru_cache(maxsize=64)
def _known_repo_enum_re(names: Tuple[str, ...]) -> "re.Pattern[str]":
    return re.compile(_known_repo_re(names).pattern + _ENUM_TAIL, re.I)


def key_hits_v21(content: Any, tags: Any, aliases: Optional[Mapping[str, str]]) -> Tuple[KeyHit, ...]:
    """v2.1 규칙의 키 언급 목록(태그 먼저, 그다음 문장 순, 문장 안에서는 명시·열거, 그다음 접두 없는 번호).
    같은 문장의 같은 (키, source, 표시)는 한 번만 낸다. 규칙은 위 주석."""
    table = _table(aliases)
    tag_set = _tag_set(tags)
    out: list = []
    seen: set = set()

    def add(hit: KeyHit) -> None:
        if (hit.key, hit.source, hit.sent, hit.ref_fixed) not in seen:
            seen.add((hit.key, hit.source, hit.sent, hit.ref_fixed))
            out.append(hit)

    for tag in sorted(tag_set):
        m = _ITEM_TAG_RE.match(tag)
        repo = _canonical(m.group(1), table) if m else ""
        if repo:
            add(KeyHit((repo, int(m.group(2))), SOURCE_TAG, None, False))
    if not isinstance(content, str) or "#" not in content:
        return tuple(out)

    projects = _project_repos(tag_set, table)
    names = set(table) | set(table.values()) | projects
    names.discard("")
    frozen_names = frozenset(names)
    known_re = _known_repo_enum_re(tuple(sorted(names))) if names else None
    name_tuple = tuple(sorted(names))
    bare_repo = next(iter(projects)) if len(projects) == 1 else None

    sentences = [s for s in _SENT_SPLIT_RE.split(_FENCE_RE.sub(" ", content)) if s and s.strip()]
    for i, sentence in enumerate(sentences):
        if "#" not in sentence:
            continue
        ctx = bool(_CONTEXT_RE.search(sentence))
        text = _URL_RE.sub(" ", sentence)
        text = _PR_RE.sub(" ", text)
        text = _expand_ranges(text, frozen_names)
        explicit: set = set()

        def take(repo: str, n: str, tail: str, source: str, i: int = i) -> str:
            """명시 키와 열거 키를 내고, 리포 맥락이 바뀐 괄호부터의 꼬리는 돌려준다(뒤 규칙이 다시 본다)."""
            explicit.add(repo)
            add(KeyHit((repo, int(n)), source, i, False))
            cut = _enum_cut(tail, name_tuple)
            for e in _ENUM_NUM_RE.findall(tail[:cut]):
                add(KeyHit((repo, int(e)), SOURCE_ENUM, i, False))
            return tail[cut:]

        def take_known(m: "re.Match[str]") -> str:
            head = m.group(0)[: len(m.group(0)) - len(m.group(2))]
            return " " + take(_canonical(head[: head.rindex("#")], table), m.group(1), m.group(2), SOURCE_KNOWN)

        def take_owner(m: "re.Match[str]") -> str:
            repo = _canonical(m.group(1), table)
            if repo:
                return " " + take(repo, m.group(2), m.group(3), SOURCE_OWNER)
            return " "

        if known_re is not None:
            text = known_re.sub(take_known, text)
        text = _OWNER_ENUM_RE.sub(take_owner, text)
        if len(explicit) == 1:
            (repo,) = explicit
            for m in _BARE_RE.finditer(text):
                add(KeyHit((repo, int(m.group(1))), SOURCE_SENTENCE, i, ctx))
        elif not explicit and bare_repo is not None:
            for m in _BARE_RE.finditer(text):
                add(KeyHit((bare_repo, int(m.group(1))), SOURCE_BARE, i, ctx))
    return tuple(out)


def extract_keys_v21(content: Any, tags: Any, aliases: Optional[Mapping[str, str]]) -> FrozenSet[ItemKey]:
    """v2.1 규칙의 키 집합(참조 고정 키 포함, `extract_keys_v2` 와 같은 뜻)."""
    return frozenset(h.key for h in key_hits_v21(content, tags, aliases))


# --- 무효화 대상 판정 --------------------------------------------------------------------------

def _text(mem: Mapping[str, Any]) -> str:
    for key in ("text", "content"):
        if isinstance(mem.get(key), str):
            return mem[key]
    return ""


def _fact_type(mem: Mapping[str, Any]) -> Optional[str]:
    # REST recall 행은 `type`, memories/list 는 `fact_type` 을 쓴다(v1.0.0 실측 #259).
    for key in ("fact_type", "type"):
        if isinstance(mem.get(key), str):
            return mem[key].strip().lower()
    return None


def _instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _pending_label(text: str) -> bool:
    m = _LABEL_RE.match(text)
    if not m:
        return False
    last = None
    for word in _LABEL_WORD_RE.finditer(m.group(1).strip().lower()):
        last = word.lastgroup
    return last == "pending"


def is_completion(memory: Any) -> bool:
    """편의 함수: 기억이 `kind:completion` 태그를 가졌는가(is_target 은 호출하지 않는다)."""
    return isinstance(memory, Mapping) and KIND_COMPLETION in _tag_set(memory.get("tags"))


def is_target(completion: Any, candidate: Any, aliases: Optional[Mapping[str, str]]) -> bool:
    """`completion` 하나에 대해 `candidate` 하나가 무효화 대상이면 True. 규칙은 모듈 머리 주석."""
    if not isinstance(completion, Mapping) or not isinstance(candidate, Mapping):
        return False
    if _fact_type(candidate) not in _CANDIDATE_FACT_TYPES or candidate.get("state") != "valid":
        return False
    cand_tags = _tag_set(candidate.get("tags"))
    comp_tags = _tag_set(completion.get("tags"))
    if KIND_COMPLETION in cand_tags:
        return False
    pending = bool(cand_tags & _PENDING_KINDS) or (
        _DECISION_KIND in cand_tags and _pending_label(_text(candidate)))
    if not pending:
        return False
    if not any(t.startswith(_PROJECT_TAG_PREFIX) for t in comp_tags & cand_tags):
        return False
    if not (extract_keys(_text(completion), comp_tags, aliases)
            & extract_keys(_text(candidate), cand_tags, aliases)):
        return False
    done_at, cand_at = _instant(completion.get("mentioned_at")), _instant(candidate.get("mentioned_at"))
    if done_at is None or cand_at is None or not cand_at < done_at:
        return False
    doc, cand_doc = completion.get("document_id"), candidate.get("document_id")
    return not (isinstance(doc, str) and doc and doc == cand_doc)


def over_cap(targets: Union[int, Iterable[Any]], cap: int = MAX_TARGETS_PER_COMPLETION) -> bool:
    """대상 수(정수 또는 대상 목록)가 상한을 넘으면 True: 이 완료는 아무것도 무효화하지 않는다."""
    if isinstance(targets, int) and not isinstance(targets, bool):
        count = targets
    elif hasattr(targets, "__len__"):
        count = len(targets)                                   # type: ignore[arg-type]
    else:
        count = sum(1 for _ in targets)
    return count > cap


@dataclass(frozen=True)
class TargetResult:
    """한 완료에 대한 판정. `targets` 는 규칙에 맞은 후보 전부(입력 순서), `over_cap` 이 True 면 무효화는 0건이다."""

    targets: Tuple[Mapping[str, Any], ...]
    over_cap: bool

    @property
    def invalidate(self) -> Tuple[Mapping[str, Any], ...]:
        return () if self.over_cap else self.targets


def find_targets(completion: Any, candidates: Sequence[Any], aliases: Optional[Mapping[str, str]],
                 *, cap: int = MAX_TARGETS_PER_COMPLETION) -> TargetResult:
    """`candidates` 중 `completion` 의 무효화 대상과 상한 판정. 같은 `id` 의 후보는 한 번만 센다."""
    seen = set()
    targets = []
    own_id = completion.get("id") if isinstance(completion, Mapping) else None
    for cand in candidates:
        if not is_target(completion, cand, aliases):
            continue
        cid = cand.get("id")
        if isinstance(cid, str) and cid:
            if cid in seen or cid == own_id:
                continue
            seen.add(cid)
        targets.append(cand)
    return TargetResult(tuple(targets), over_cap(len(targets), cap))
