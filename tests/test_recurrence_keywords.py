"""이슈 #277: 트리거 초안 키워드에서 일반어·범용 호스트를 제외한다.

`match_keywords`가 `project:batch-job` 같은 slug를 `batch`,`job`으로 쪼개 넣고, 일반어 목록이 slug에만 걸려 공유어 `gotcha`가
통과하고, `gmail.com` 같은 범용 호스트(와 `_WORD_RE`가 따로 뽑는 맨 라벨 `gmail`)가 들어갔다. 트리거는 any-of 부분문자열
매칭이라 이런 키워드 하나가 관련 없는 대화마다 발화한다.

확인하는 경계:
  1. 재현 입력(합성): 일반어·범용 호스트는 빠지고 고유 호스트는 남는다.
  2. 같은 필터가 모든 출처(호스트·공유어·엔티티·slug)에 적용된다.
  3. 걸러서 남는 게 없으면 match_keywords는 [], build_draft는 `<match keyword needed>` 그대로다(초안 스키마 불변).
입력은 전부 합성 텍스트다.
"""
import pytest

from gateway import recurrence

BANK = "synthetic-bank"
MEM_ID = "mem-synthetic-1"
NEEDED = "<match keyword needed>"

NEW = ("[gotcha] gmail.com 로그인 뒤 도서 search 색인이 비고 gotcha 재발 - 야간 job 이 example-catalog.kr 대출 현황을 못 읽는다")
OLD = ("[gotcha] 도서 search 색인에서 gotcha 가 반복된다 - gmail.com 계정 전환 뒤 야간 job 이 example-catalog.kr 을 못 읽는다")


def _similar(text=OLD, tags=(), entities=()):
    return {"id": MEM_ID, "text": text, "tags": list(tags), "entities": list(entities), "score": 0.95}


def _rescue():
    return recurrence.RecallEvent(ts=0.0, identity="alice", bank=BANK, transport="mcp",
                                  query="synthetic rescue query", tags=("project:x",), result_ids=(MEM_ID,),
                                  reusable=True)


def test_issue_input_drops_generic_words_and_keeps_specific_host():
    kws = recurrence.match_keywords(NEW, _similar(tags=["project:batch-job", "kind:gotcha"]))
    for generic in ("job", "search", "gotcha", "gmail.com", "gmail"):
        assert generic not in kws
    assert "example-catalog.kr" in kws


GENERIC_HOSTS = ["gmail.com", "naver.com", "github.com", "google.com"]


@pytest.mark.parametrize("host", GENERIC_HOSTS)
def test_generic_host_and_its_bare_label_are_dropped_from_every_source(host):
    label = host.split(".")[0]
    text = f"[발견] {host} 접속 뒤 {label} 쪽 설정이 바뀐다 - uniquehost.kr 도 같다"
    kws = recurrence.match_keywords(text, _similar(text=text, entities=[label, host]))
    assert host not in kws and label not in kws
    assert "uniquehost.kr" in kws


# 기존 일반어(security)가 들어오는 세 경로
SECURITY_PATHS = [
    pytest.param("[발견] 전혀 다른 본문", _similar(text="[발견] 또 다른 본문", tags=["topic:security"]),
                 id="slug"),
    pytest.param("[발견] security 설정이 바뀐다", _similar(text="[발견] security 설정이 다시 바뀐다"),
                 id="shared-word"),
    pytest.param("[발견] security 설정이 바뀐다",
                 _similar(text="[발견] security 설정이 다시 바뀐다", entities=["security"]),
                 id="shared-word-and-entity"),
]


@pytest.mark.parametrize("new, similar", SECURITY_PATHS)
def test_existing_generic_word_is_excluded_on_every_path(new, similar):
    assert "security" not in recurrence.match_keywords(new, similar)


# 새 일반어(job/search)와 공유어 gotcha도 같은 경로 셋
@pytest.mark.parametrize("word", ["job", "search", "gotcha"])
def test_new_generic_words_are_excluded_on_every_path(word):
    new = f"[발견] {word} 관련 실패가 난다 - uniquething 문제"
    similar = _similar(text=f"[발견] {word} 관련 실패가 또 난다 - uniquething 문제",
                       tags=[f"project:{word}-x"], entities=[word])
    kws = recurrence.match_keywords(new, similar)
    assert word not in kws
    assert "uniquething" in kws


ALL_GENERIC_NEW = "[gotcha] gmail.com security deploy config"
ALL_GENERIC_OLD = "[gotcha] gmail.com security deploy config"


def test_all_generic_input_gives_empty_list_and_unchanged_draft_placeholder():
    similar = _similar(text=ALL_GENERIC_OLD, tags=["project:deploy-ops", "kind:gotcha"],
                       entities=["gmail", "security"])
    assert recurrence.match_keywords(ALL_GENERIC_NEW, similar) == []
    draft = recurrence.build_draft(bank=BANK, signal=recurrence.SIGNAL_GOTCHA, new_content=ALL_GENERIC_NEW,
                                   similar=similar, rescue=_rescue())
    assert draft["trigger"]["all_of"] == [[NEEDED]]


# 범용 호스트의 서브도메인도 같은 부류다(final-review #1). 비범용 호스트는 서브도메인이어도 남는다.
@pytest.mark.parametrize("host, kept", [
    pytest.param("mail.google.com", False, id="mail.google.com"),
    pytest.param("blog.naver.com", False, id="blog.naver.com"),
    pytest.param("api.github.com", False, id="api.github.com"),
    pytest.param("example-catalog.kr", True, id="example-catalog.kr"),
    pytest.param("news.example.com", True, id="news.example.com"),
])
def test_generic_host_subdomains_are_dropped_and_other_hosts_kept(host, kept):
    new = f"[발견] {host} 접속이 실패한다"
    kws = recurrence.match_keywords(new, _similar(text=f"[발견] {host} 접속이 또 실패한다"))
    assert (host in kws) is kept
