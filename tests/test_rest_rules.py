"""N3: 요청측 규칙 강제 (REST) — `POST /v1/{tenant}/banks/{bank}/memories` items[] 순회.

MCP(N2)와 **같은** 순수 함수 `gateway.rules.evaluate_item`을 REST 형상에 적용한다. 두
표면이 같은 규칙을 강제한다는 것(P1)이 이 파일의 존재 이유이므로, 페이로드 형상은 실측
fixture(`fixtures/rest/memories_post_ok.json`)에서 출발한다 — tags·timestamp는 item
**최상위** 키다(`fixtures/mcp/NOTES.md` ②).

거부 채널 3종은 서로 구분 가능해야 한다:
  * 게이트웨이 형식 위반 = **HTTP 422** `{"error":"rule-violation","rule":"RULE:<id>","rule_detail":[...]}`
  * upstream Pydantic 422 = `{"detail":[...]}` → 키 충돌 회피 위해 게이트웨이는 `rule_detail` 사용
  * 인증·ACL 위반 = **HTTP 403** `{"error","reason"}` (N3 이전과 동일, 회귀 금지)

위반이 하나라도 있으면 **전량 미전달**: REST 한 요청은 여러 item을 담으므로 부분 저장을
허용하면 "무엇이 저장됐는지" 모델이 알 수 없게 된다.
"""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, load_rest_fixture
from gateway.bank_policies import BankPolicies

import os

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
REPO_POLICY_FILE = os.path.join(REPO_ROOT, "tests", "fixtures", "policies", "sample", "bank-policies.json")

STRICT_BANK = "personal-alice"   # 정책 파일상 엄격 프로필
TEAM_BANK = "team-eng"           # 정책 파일상 태그만 강제(옵션 B) + 귀속 각인 대상
LOOSE_BANK = "personal-bob"      # 정책 파일에 없음 → defaults 폴백(느슨). tok-bob 소유


# --- 로컬 헬퍼 (conftest에는 MCP 헬퍼만 있고 REST 헬퍼는 없다; conftest는 추가만 허용) ---

def strict_app(make_app):
    return make_app(bank_policies=BankPolicies.from_file(REPO_POLICY_FILE))


def rest_post(client, bank, token, payload, rest="memories", tenant="default"):
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return client.post(f"/v1/{tenant}/banks/{bank}/{rest}",
                       content=json.dumps(payload), headers=headers)


def item(**overrides):
    """실측 요청 형상에서 출발한 준수 item (timestamp는 기본적으로 제거 = autofill 대상)."""
    src = dict(load_rest_fixture("memories_post_ok")["request"]["items"][0])
    src.pop("timestamp", None)
    src.update(overrides)
    return src


def body(*items, **kw):
    payload = {"items": list(items), "async": False}
    payload.update(kw)
    return payload


def audit_entries(client):
    with open(client.app.state._audit_path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def decisions(client):
    return [e["decision"] for e in audit_entries(client)]


def forwarded_items(recorder):
    return recorder.last["body"]["items"]


# ============================ ① timestamp autofill ============================

def test_missing_timestamp_is_autofilled_into_the_forwarded_items(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice", body(item()))

    assert r.status_code == 200
    fwd = forwarded_items(recorder)
    assert len(fwd) == 1
    assert fwd[0]["timestamp"].endswith("Z")                 # 부재였던 키가 채워졌다
    assert fwd[0]["content"] == item()["content"]            # 보정 외 원본 유지
    assert fwd[0]["tags"] == item()["tags"]
    assert "enforce-autofill" in decisions(c)


def test_autofill_is_applied_to_every_item_in_the_batch(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice", body(item(), item(content="[검증] 두 번째 항목")))
    assert r.status_code == 200
    stamps = [it["timestamp"] for it in forwarded_items(recorder)]
    assert len(stamps) == 2 and all(s.endswith("Z") for s in stamps)


# ============================ ② "unset" 덮어쓰기 =============================

def test_unset_timestamp_is_overwritten_on_the_wire(make_app, recorder):
    """upstream 공식 특수값 `"unset"`으로 D1-① 재발을 우회할 수 없어야 한다."""
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice", body(item(timestamp="unset")))
    assert r.status_code == 200
    assert forwarded_items(recorder)[0]["timestamp"] != "unset"
    assert "enforce-autofill" in decisions(c)


def test_explicit_timestamp_is_respected(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice", body(item(timestamp="2020-01-02T03:04:05Z")))
    assert r.status_code == 200
    assert forwarded_items(recorder)[0]["timestamp"] == "2020-01-02T03:04:05Z"
    assert "enforce-autofill" not in decisions(c)


# ==================== ③ 엄격 뱅크 위반 = 422 rule-violation ====================

def test_untagged_item_on_strict_bank_is_422_rule_violation(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice", body(item(tags=[])))

    assert r.status_code == 422                       # 403(authz)도, 200도 아니다
    data = r.json()
    assert data["error"] == "rule-violation"
    assert data["rule"] == "RULE:tags-required"
    assert "detail" not in data                       # upstream Pydantic 422와 키 충돌 회피
    detail = data["rule_detail"]
    assert isinstance(detail, list) and len(detail) == 1
    assert detail[0]["index"] == 0
    assert detail[0]["rules"] == ["RULE:tags-required"]
    assert detail[0]["message"].startswith("RULE:tags-required")
    assert recorder.calls == []                       # 업스트림 미전달
    assert "enforce-reject" in decisions(c)


def test_bad_tag_grammar_and_content_prefix_are_reported_per_item(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice",
                  body(item(tags=["한글"], content="접두 없는 본문")))   # 정규화 불가 태그
    assert r.status_code == 422
    data = r.json()
    assert data["rule"] in ("RULE:tags-grammar", "RULE:content-prefix")
    assert set(data["rule_detail"][0]["rules"]) == {"RULE:tags-grammar", "RULE:content-prefix"}
    assert recorder.calls == []


def test_illegal_strategy_is_rejected_on_strict_bank(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice", body(item(strategy="deep")))
    assert r.status_code == 422
    assert r.json()["rule"] == "RULE:strategy-unknown"
    assert recorder.calls == []


# ==================== ④ 느슨 뱅크는 autofill만 (강제 없음) ====================

def test_loose_bank_untagged_item_passes_with_autofill_only(make_app, recorder):
    """정책 파일 미등록 뱅크(personal-bob). 2026-07-26 이전엔 team-eng이 이 자리였다."""
    c = TestClient(strict_app(make_app))
    r = rest_post(c, LOOSE_BANK, "tok-bob",
                  body({"content": "자유 형식 노트", "strategy": "whatever"}))
    assert r.status_code == 200
    fwd = forwarded_items(recorder)[0]
    assert fwd["content"] == "자유 형식 노트"
    assert fwd["strategy"] == "whatever"
    assert fwd["timestamp"].endswith("Z")            # autofill은 전 뱅크 보편
    assert "enforce-reject" not in decisions(c)


def test_team_attribution_still_stamped_after_rules(make_app, recorder):
    """순서 계약: rules(보정·검증) → sanitize(귀속 각인). 둘 다 살아있어야 한다.

    옵션 B 이후 team-eng은 태그를 요구하므로 페이로드에 준수 태그를 실어 규칙을
    통과시킨 뒤, 그 다음 단계인 귀속 각인이 여전히 동작하는지를 본다.
    """
    c = TestClient(strict_app(make_app))
    r = rest_post(c, TEAM_BANK, "tok-alice",
                  body({"content": "팀 노트", "tags": ["project:hindsight-crew", "kind:note"],
                        "metadata": {"hc_member": "bob"}}))
    assert r.status_code == 200
    fwd = forwarded_items(recorder)[0]
    assert fwd["metadata"]["hc_member"] == "alice"   # 위조 덮어쓰기 유지
    assert fwd["timestamp"].endswith("Z")


# ========== ④-b 팀 뱅크 = 태그만 강제 (옵션 B 절충, 2026-07-26 운영자 승인) ==========
#
# REST 표면도 MCP와 같은 순수 함수를 쓰므로(P1), team-eng의 태그 강제는 여기서도
# 동일하게 관측돼야 한다. 다른 점은 거부 채널뿐: isError가 아니라 HTTP 422.

def test_team_bank_untagged_item_is_422_rule_violation(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = rest_post(c, TEAM_BANK, "tok-alice", body({"content": "팀 노트"}))

    assert r.status_code == 422
    data = r.json()
    assert data["error"] == "rule-violation"
    assert data["rule"] == "RULE:tags-required"
    assert data["rule_detail"][0]["rules"] == ["RULE:tags-required"]
    assert recorder.calls == []
    assert "enforce-reject" in decisions(c)


def test_team_bank_content_without_prefix_passes_when_tagged(make_app, recorder):
    """태그**만** 강제 — 개인(엄격) 뱅크였다면 RULE:content-prefix였을 본문이 통과한다."""
    c = TestClient(strict_app(make_app))
    r = rest_post(c, TEAM_BANK, "tok-alice",
                  body({"content": "접두 없는 팀 노트",
                        "tags": ["project:hindsight-crew", "kind:note"]}))
    assert r.status_code == 200
    fwd = forwarded_items(recorder)[0]
    assert fwd["content"] == "접두 없는 팀 노트"
    assert fwd["tags"] == ["project:hindsight-crew", "kind:note"]
    assert fwd["timestamp"].endswith("Z")
    assert "enforce-reject" not in decisions(c)


# ==================== ⑤ 혼재 items = 전량 거부 (부분 저장 금지) ====================

def test_mixed_items_reject_everything_and_forward_nothing(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice",
                  body(item(), item(tags=[]), item(content="접두 없음")))

    assert r.status_code == 422
    assert recorder.calls == []                      # 정상 item도 업스트림에 안 간다
    data = r.json()
    reported = {e["index"] for e in data["rule_detail"]}
    assert reported == {1, 2}                        # 위반 item만 지목 (0번은 정상)
    assert data["rule"] == data["rule_detail"][0]["rules"][0]


# ==================== ⑥ 기존 REST 경로 회귀 (403 / 비-retain) ====================

def test_rest_bank_policy_blocks_only_catastrophic_delete_paths(
    make_app, recorder, monkeypatch
):
    monkeypatch.setitem(
        ACL_DATA["tokens"],
        "tok-demo-gm",
        {
            "identity": "demo-gm-user",
            "personal": "project-demo-gm",
            "teams": [],
        },
    )
    monkeypatch.setitem(
        ACL_DATA["tokens"],
        "tok-demo",
        {"identity": "demo", "personal": "personal-demo", "teams": []},
    )
    c = TestClient(strict_app(make_app))
    demogm_headers = {"authorization": "Bearer tok-demo-gm"}

    delete_bank = c.delete(
        "/v1/default/banks/project-demo-gm", headers=demogm_headers
    )
    assert delete_bank.status_code == 403
    assert delete_bank.json() == {
        "error": "forbidden",
        "reason": "blocked-by-bank-policy:delete_bank",
    }
    assert recorder.calls == []

    clear_memories = c.delete(
        "/v1/default/banks/project-demo-gm/memories",
        headers=demogm_headers,
    )
    assert clear_memories.status_code == 403
    assert clear_memories.json() == {
        "error": "forbidden",
        "reason": "blocked-by-bank-policy:clear_memories",
    }
    assert recorder.calls == []

    # The path classifier strips slashes but must not broaden to memories/<id>.
    trailing_slash = c.delete(
        "/v1/default/banks/project-demo-gm/memories/",
        headers=demogm_headers,
    )
    assert trailing_slash.status_code == 403
    assert recorder.calls == []

    granular_delete = c.delete(
        "/v1/default/banks/project-demo-gm/memories/memory-1",
        headers=demogm_headers,
    )
    assert granular_delete.status_code == 200
    assert [call["path"] for call in recorder.calls] == [
        "/v1/default/banks/project-demo-gm/memories/memory-1"
    ]

    unblocked_clear = c.delete(
        "/v1/default/banks/personal-demo/memories",
        headers={"authorization": "Bearer tok-demo"},
    )
    assert unblocked_clear.status_code == 200
    assert [call["path"] for call in recorder.calls] == [
        "/v1/default/banks/project-demo-gm/memories/memory-1",
        "/v1/default/banks/personal-demo/memories",
    ]


def test_rest_403_paths_are_unchanged_under_strict_policies(make_app, recorder):
    """인증·ACL은 여전히 403 `{"error","reason"}` — 형식 위반(422)과 별개 채널(P2)."""
    c = TestClient(strict_app(make_app))

    r = rest_post(c, "personal-bob", "tok-alice", body(item()))
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert "reason" in r.json()

    assert rest_post(c, STRICT_BANK, "tok-nope", body(item())).status_code == 403
    assert rest_post(c, STRICT_BANK, None, body(item())).status_code == 403

    # destructive on a team bank stays owner-only
    assert c.delete(f"/v1/default/banks/{TEAM_BANK}/memories",
                    headers={"authorization": "Bearer tok-alice"}).status_code == 403
    assert recorder.calls == []


def test_recall_path_is_not_rule_checked(make_app, recorder):
    """retain 외 REST 경로(recall 등)는 무변경 — 규칙도 autofill도 붙지 않는다."""
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice", {"query": "무엇이든"}, rest="memories/recall")
    assert r.status_code == 200
    assert recorder.last["body"] == {"query": "무엇이든"}
    assert "enforce-reject" not in decisions(c)
    assert "enforce-autofill" not in decisions(c)


def test_non_dict_item_is_passed_through_unjudged(make_app, recorder):
    """dict가 아닌 원소는 게이트웨이가 판정하지 않는다 — 형상 위반은 upstream 422의 몫이고,
    여기서 {}로 취급하면 클라이언트 데이터를 조용히 파괴하게 된다."""
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice", body("문자열 item"))
    assert r.status_code == 200
    assert recorder.last["body"]["items"] == ["문자열 item"]


def test_non_items_retain_body_is_passed_through_unchanged(make_app, recorder):
    """items[]가 아닌(=계약 밖) 본문은 게이트웨이가 판정하지 않고 upstream 422에 맡긴다."""
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice", {"nope": True})
    assert r.status_code == 200
    assert recorder.last["body"] == {"nope": True}


# --- 태그 정규화(N2b)의 REST 패리티 -------------------------------------------
# MCP는 정규화를 감사(enforce-normalize)하고 응답에 알린다. REST가 조용히 고치면
# 같은 게이트웨이가 표면에 따라 다른 정직성 계약을 갖게 된다(P1/P5 동시 위반).

def test_rest_normalizes_fixable_tags_instead_of_rejecting(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice", body(item(tags=["skills", "Kind:Decision"])))

    assert r.status_code == 200
    assert recorder.last["body"]["items"][0]["tags"] == ["topic:skills", "kind:decision"]


def test_rest_tag_normalization_is_audited(make_app, recorder):
    c = TestClient(strict_app(make_app))
    rest_post(c, STRICT_BANK, "tok-alice", body(item(tags=["skills"])))
    assert "enforce-normalize" in [e["decision"] for e in audit_entries(c)]


def test_rest_tag_normalization_is_disclosed_to_the_caller(make_app, recorder):
    """조용한 정정 금지: 무엇이 저장됐는지 호출자가 응답만 보고 알 수 있어야 한다."""
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice", body(item(tags=["skills"])))

    assert r.status_code == 200
    assert r.json()["gateway_normalized_tags"] == [
        {"index": 0, "rewrites": [{"sent": "skills", "stored": "topic:skills"}]}
    ]


def test_undisclosable_rest_rewrite_is_counted_not_silent(tmp_path):
    """upstream 이 dict 가 아닌 2xx 본문을 주면 공개할 자리가 없다. 그래도 조용히
    넘어가지 않는다 - MCP 재작성 실패와 같은 카운터·감사 경보를 올린다.

    conftest 의 Recorder 는 고정 dict 응답이라 이 경로를 못 만든다(conftest 는 무수정).
    """
    import httpx
    from fastapi.testclient import TestClient as _TC
    from conftest import ACL_DATA, UPSTREAM_KEY, bank_config_reply
    from gateway.acl import ACL
    from gateway.app import create_app
    from gateway.audit import Audit

    def handler(request):
        if (config := bank_config_reply(request)) is not None:   # #473 b upstream config read
            return config
        return httpx.Response(200, content=b'"not-a-dict"',
                              headers={"content-type": "application/json"})

    audit_path = str(tmp_path / "audit.log")
    app = create_app(
        acl=ACL.from_dict(ACL_DATA), audit=Audit(path=audit_path), upstream_key=UPSTREAM_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                      base_url="http://upstream", timeout=5.0),
        bank_policies=BankPolicies.from_file(REPO_POLICY_FILE))
    app.state._audit_path = audit_path
    c = _TC(app)
    r = rest_post(c, STRICT_BANK, "tok-alice", body(item(tags=["skills"])))

    assert r.status_code == 200                       # fail-open: 원본 응답 유지
    assert r.content == b'"not-a-dict"'
    assert c.app.state.rewrite_failures == 1
    assert "rewrite-failure" in [e["decision"] for e in audit_entries(c)]


# ============ 이슈 #107-4: upstream 2xx 실패 본문에는 저장을 단정하는 태그 경고를 싣지 않는다 ============
# "저장은 그대로 진행했습니다" 류 태그 경고(#40)는 저장이 됐다는 전제의 말이다. upstream 이 2xx 에
# 실패 본문(`success:false`·`error`)을 주면 그 말이 거짓이므로 싣지 않는다(MCP 가 isError 응답에
# 경고를 달지 않는 것과 같은 기준). 교정 공개(gateway_normalized_tags)는 게이트웨이가 한 일의 사실이라 유지한다.

DEMO_BANK, DEMO_TOKEN = "personal-demo", "tok-test-demo"       # 테스트 전용 리터럴. 실제 토큰 아님.
WARN_TAGS = {
    "similar-name": ["kind:fix", "project:hemlock-loom-v2"],                   # 비슷한 새 이름(#40)
    "ambiguous-candidates": ["kind:fix", "topic:larch-yard", "topic:tupelo"],  # 후보 둘 이상(#40)
}
SUCCESS_BODY = {"success": True, "bank_id": DEMO_BANK, "items_count": 1, "async": False}
FAILURE_BODIES = {
    "success-false": {"success": False, "bank_id": DEMO_BANK, "items_count": 0},
    "error-string": {"error": "internal failure"},
    "success-false-with-error": {"success": False, "error": "boom"},
    "status-failed": {"success": True, "status": "failed"},
}


def demo_rest_app(tmp_path, upstream_body):
    import httpx
    from conftest import UPSTREAM_KEY, bank_config_reply
    from gateway.acl import ACL
    from gateway.app import create_app
    from gateway.audit import Audit

    audit_path = str(tmp_path / "audit.log")
    app = create_app(
        acl=ACL.from_dict({"tokens": {DEMO_TOKEN: {"identity": "demo", "personal": DEMO_BANK, "teams": []}}}),
        audit=Audit(path=audit_path), upstream_key=UPSTREAM_KEY,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(   # #473 b: answer the upstream config read first
                lambda request: bank_config_reply(request) or httpx.Response(200, json=upstream_body)),
            base_url="http://upstream", timeout=5.0),
        bank_policies=BankPolicies.from_file(REPO_POLICY_FILE))
    app.state._audit_path = audit_path
    return TestClient(app)


@pytest.mark.parametrize("warn_kind", sorted(WARN_TAGS))
def test_tag_warning_is_disclosed_on_a_real_success_body(tmp_path, warn_kind):
    c = demo_rest_app(tmp_path, SUCCESS_BODY)
    r = rest_post(c, DEMO_BANK, DEMO_TOKEN, body(item(tags=WARN_TAGS[warn_kind])))

    assert r.status_code == 200
    texts = [w["text"] for w in r.json()["gateway_warnings"]]
    assert len(texts) == 1 and texts[0].startswith("[gateway 경고]")        # 성공 본문의 경고는 그대로


@pytest.mark.parametrize("failure", sorted(FAILURE_BODIES))
@pytest.mark.parametrize("warn_kind", sorted(WARN_TAGS))
def test_tag_warning_is_not_attached_to_a_2xx_failure_body(tmp_path, warn_kind, failure):
    c = demo_rest_app(tmp_path, FAILURE_BODIES[failure])
    r = rest_post(c, DEMO_BANK, DEMO_TOKEN, body(item(tags=WARN_TAGS[warn_kind])))

    assert r.status_code == 200                                  # 상태 코드는 upstream 그대로
    data = r.json()
    assert "gateway_warnings" not in data                        # 저장을 단정하는 경고를 싣지 않는다
    assert "저장" not in json.dumps(data, ensure_ascii=False)
    for key, value in FAILURE_BODIES[failure].items():
        assert data[key] == value                                # upstream 본문은 손대지 않는다


@pytest.mark.parametrize("failure", sorted(FAILURE_BODIES))
def test_normalization_disclosure_is_kept_on_a_2xx_failure_body(tmp_path, failure):
    """범위: 경고의 정확성만 고친다. 게이트웨이가 한 교정의 공개(rest_normalizations)는 그대로다."""
    c = demo_rest_app(tmp_path, FAILURE_BODIES[failure])
    r = rest_post(c, DEMO_BANK, DEMO_TOKEN,
                  body(item(tags=["kind:fix", "project:hindsight-crew-v2", "skills"])))

    data = r.json()
    assert data["gateway_normalized_tags"] == [
        {"index": 0, "rewrites": [{"sent": "skills", "stored": "topic:skills"}]}]
    assert "gateway_warnings" not in data
