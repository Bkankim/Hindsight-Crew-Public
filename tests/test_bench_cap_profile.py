"""issue #13 리랭크 후보 상한 분석 - 상한을 낮춰도 recall 결과가 불변인지 가르는 규칙을 고정한다.

upstream 은 RRF 상위 MAX_CANDIDATES 개만 리랭크하고, 최종 점수는 후보마다 독립적으로 계산한다
(rrf_normalized=0 고정, reranking.py). 그래서 반환된 결과가 모두 RRF 순위 cap 이내면 cap 에서도 결과가 같다.
"""
from bench import cap_profile as C


def trace(*rrf_ranks):
    """실제 recall trace 형태(final_results[*].rrf_rank)를 본뜬 최소 픽스처. 본문은 더미."""
    return {"final_results": [{"id": f"m{i}", "text": "dummy", "rrf_rank": r} for i, r in enumerate(rrf_ranks)]}


def test_required_cap_is_deepest_rrf_rank_among_returned_results():
    assert C.required_cap(trace(3, 1, 187, 42)) == 187


def test_query_with_no_results_is_unchanged_at_any_cap():
    assert C.required_cap(trace()) == 0


def test_result_without_rrf_rank_makes_the_query_unprovable():
    """출처 순위를 모르는 결과가 하나라도 있으면 어떤 상한에서도 불변을 주장할 수 없다."""
    assert C.required_cap(trace(3, None)) is None


def test_profile_counts_queries_unchanged_at_each_cap():
    # 쿼리 5개: 필요 상한 12, 90, 150, 151, 증명 불가
    prof = C.profile([12, 90, 150, 151, None], caps=[100, 150, 200])
    assert prof == {
        100: {"unchanged": 2, "total": 5},
        150: {"unchanged": 3, "total": 5},
        200: {"unchanged": 4, "total": 5},   # 증명 불가 쿼리는 어느 상한에서도 불변으로 세지 않는다
    }
