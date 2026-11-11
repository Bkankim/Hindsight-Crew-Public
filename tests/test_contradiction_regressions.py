"""Issue #12 re-review: label outcomes and safe precedent warnings."""
import pytest

from gateway import contradiction


@pytest.mark.parametrize("label", [
    "차단", "해결 불가", "복구 실패", "우회 실패", "미완료", "미해결",
    "차단 해제 실패", "완료 후 실패", "blocked", "failed", "incomplete",
    "unresolved", "unsuccessful", "not resolved", "not yet fixed",
    "cannot complete", "fixed then failed", "failed to complete",
    "cannot be resolved", "not success", "not succeeded",
])
def test_negative_outcomes_are_not_successful_precedents(label):
    content = f"[{label}] 같은 서비스 접근 결과"
    assert contradiction.is_negative(content)
    assert not contradiction.is_positive_precedent({"text": content, "tags": ["kind:gotcha"]})


@pytest.mark.parametrize("label", [
    "완료", "정정+완료", "차단해제", "차단 해제", "실패 복구", "불가 우회",
    "실패 후 완료", "done", "completed", "unblocked", "resolved",
    "failed then fixed", "not resolved then fixed",
    "no longer blocked", "not blocked",
])
def test_resolution_outcomes_remain_successful_precedents(label):
    content = f"[{label}] 같은 서비스 접근 결과"
    assert not contradiction.is_negative(content)
    assert contradiction.is_positive_precedent({"text": content, "tags": ["kind:gotcha"]})


@pytest.mark.parametrize("kind", ["kind:completion", "kind:fix"])
def test_explicit_negative_label_overrides_success_kind(kind):
    blocked = {"id": "blocked", "text": "[해결 불가] 같은 서비스 접근 결과",
               "tags": [kind], "scores": {"reranker": 0.99}}
    resolved = {"id": "resolved", "text": "[실패 복구] 같은 서비스 접근 결과",
                "tags": ["kind:gotcha"], "scores": {"reranker": 0.95}}
    assert contradiction.find_precedents("[차단] 현재 접근 불가", [blocked, resolved]) == [resolved]
    # A neutral or missing label keeps the existing kind-tag fallback.
    assert contradiction.is_positive_precedent({"text": "[발견] service outcome", "tags": [kind]})
    assert contradiction.is_positive_precedent({"text": "service outcome", "tags": [kind]})


@pytest.mark.parametrize("content", ["[함정] 차단", "성공 후 접근 차단", "[fixedness] body"])
def test_unknown_labels_and_body_words_do_not_invent_polarity(content):
    assert not contradiction.is_negative(content)
    assert not contradiction.is_positive_precedent({"text": content, "tags": ["kind:gotcha"]})


def test_warning_redacts_precedent_before_truncating():
    # Synthetic credential shape starts before the cut and ends well after it.
    token = "sk-" + "x" * 60
    prefix = "[완료] " + "a" * 20 + " "
    memory = {"id": "precedent-1", "text": prefix + token + " succeeded",
              "occurred_start": "2026-09-21", "scores": {"reranker": 0.99}}
    warning = contradiction.warning_text([memory], max_chars=40)
    assert "[redacted]" in warning
    assert token[:12] not in warning
    assert prefix in warning
    assert "id:preceden" in warning and "2026-09-21" in warning


def test_warning_does_not_claim_async_storage_has_completed():
    warning = contradiction.warning_text([])
    assert "저장은 됐다" not in warning
    assert "저장 요청은 허용됐다" in warning
    assert "비동기" in warning and "완료 상태" in warning
