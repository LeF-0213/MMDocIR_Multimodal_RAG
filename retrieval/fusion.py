"""채널별 검색 결과를 평가 단위(페이지)로 모으고 여러 채널의 순위를 결합한다.

전체 위치: Qdrant 후보(채널별 청크·페이지) → 페이지 집계·결합〔현재〕 → 리랭킹
입력: 채널마다 [(페이지 ID, 점수, 레코드 ID)] 목록. 출력: [(페이지 ID, 결합 점수)] 내림차순.
채널마다 모델과 점수 분포가 다르므로 점수를 직접 더하지 않고 순위만 쓰는 RRF를 쓴다.
"""


def pages_by_max(hits):
    """청크 결과를 페이지로 모은다. 한 페이지의 여러 청크 중 가장 높은 점수를 그 페이지 점수로 쓴다.

    max는 근거가 한 청크에만 있어도 페이지를 올린다. 같은 페이지 청크가 상위를 독점하는 것을 막는 효과도 있다.
    반환: [{"page_id", "score", "best_record_id"}] 점수 내림차순, 같은 점수는 페이지 ID 순으로 고정.
    """
    best = {}
    for page_id, score, record_id in hits:
        if page_id not in best or score > best[page_id]["score"]:
            best[page_id] = {"page_id": page_id, "score": score, "best_record_id": record_id}
    return sorted(best.values(), key=lambda item: (-item["score"], item["page_id"]))


def reciprocal_rank_fusion(rankings, k):
    """RRF: 페이지마다 채널별 1/(k + 순위)를 더한다. 순위는 1부터 센다.

    k는 상위 몇 개의 차이를 얼마나 강하게 볼지 정한다. 원 논문(Cormack et al., 2009)은 60을 썼다.
    한 채널에만 나온 페이지도 그 채널의 몫만 받는다.
    """
    scores = {}
    for ranking in rankings:
        for rank, page_id in enumerate(ranking, start=1):
            scores[page_id] = scores.get(page_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
