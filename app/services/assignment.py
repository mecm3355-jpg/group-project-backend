"""카테고리 배정(PRD 4장 4단계, 5.3).

카테고리 중심(승인 시점에 고정)과의 코사인 유사도로 배정한다.
- 1위 유사도가 기준값 미만이면 미배정 → 미배정 풀로 간다
- 기준값 이상이지만 1·2위 차이가 애매 구간보다 작으면 애매 → 검토 대기
- 그 외에는 1위 카테고리에 배정
"""

import numpy as np

from app.core.analysis_config import AssignmentConfig
from app.schemas.complaint_analysis import Assignment, Category, ComplaintRecord
from app.services.embedding import Vectors

# 배정 결과 행마다 기록하는 분석기 이름·버전(PRD 6장). 배정 규칙을 바꾸면 번호를 올린다
ANALYZER = "local-cosine@1"


def assign(
    records: list[ComplaintRecord],
    vectors: Vectors,
    taxonomy: list[Category],
    config: AssignmentConfig,
) -> list[Assignment]:
    """민원마다 배정 상태와 상위 두 카테고리 유사도를 돌려준다. 체계가 비면 모두 미배정.

    파라미터:
        records: 배정할 민원. vectors와 같은 순서
        vectors: records의 정규화된 임베딩
        taxonomy: 현재 체계의 카테고리(중심 벡터는 정규화된 값)
        config: 배정 기준값
    """
    # 첫 달처럼 승인된 카테고리가 없으면 비교할 중심이 없으므로 전부 미배정 풀로 보낸다(PRD 4장)
    if not taxonomy:
        return [
            Assignment(
                record_id=record.record_id,
                status="unassigned",
                category_id=None,
                top1_similarity=None,
                second_category_id=None,
                top2_similarity=None,
                analyzer=ANALYZER,
            )
            for record in records
        ]

    # 민원 벡터와 중심 모두 길이 1이라 행렬곱 한 번이 (민원 × 카테고리) 코사인 유사도 표가 된다
    centroids = np.asarray([category.centroid for category in taxonomy], dtype=np.float32)
    similarities = vectors @ centroids.T
    results: list[Assignment] = []
    for record, row in zip(records, similarities, strict=True):
        # 유사도 높은 순으로 1·2위를 찾는다. 카테고리가 하나뿐이면 2위는 없다
        order = np.argsort(-row)
        first = int(order[0])
        top1 = float(row[first])
        second = int(order[1]) if len(order) > 1 else None
        top2 = float(row[second]) if second is not None else None
        # 기준값(초기 0.75)이 낮으면 다른 민원이 기존 카테고리로 흡수되고, 높으면 신규가 남발된다.
        # 1·2위가 거의 같으면(차이 0.05 미만) 어느 쪽인지 기계가 정하지 않고 사람 검토로 넘긴다
        # (PRD 5.3)
        if top1 < config.threshold:
            status = "unassigned"
        elif top2 is not None and top1 - top2 < config.ambiguity_margin:
            status = "ambiguous"
        else:
            status = "assigned"
        # 미배정·애매여도 1·2위 카테고리와 유사도는 남긴다. 검토 화면과 기준값 조정의 근거가 된다
        results.append(
            Assignment(
                record_id=record.record_id,
                status=status,
                category_id=taxonomy[first].category_id,
                top1_similarity=round(top1, 4),
                second_category_id=taxonomy[second].category_id if second is not None else None,
                top2_similarity=round(top2, 4) if top2 is not None else None,
                analyzer=ANALYZER,
            )
        )
    return results
