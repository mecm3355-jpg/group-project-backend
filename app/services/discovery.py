"""신규 카테고리 발견(PRD 4장 5단계, 6장 A. 로컬 방식).

미배정 풀을 UMAP으로 줄인 뒤 HDBSCAN으로 묶는다. 군집 수를 미리 정하지 않고, 어느 군집에도
들지 않은 민원(잡음)은 미배정으로 남긴다. 후보 기준(PRD 5.3): 같은 군집 candidate_min_size건
이상, 또는 그보다 작아도 2개월 연속 등장.
"""

import numpy as np
from sklearn.cluster import HDBSCAN
from umap import UMAP

from app.core.analysis_config import DiscoveryConfig
from app.schemas.complaint_analysis import CandidateCategory, CandidateReason, ComplaintRecord
from app.services.embedding import Vectors, normalize
from app.services.keywords import Tokenizer, class_tfidf, noun_tokens

# 후보마다 기록하는 분석기 이름·버전(PRD 6장 "분석기 이름·버전·사용 모델을 결과 행마다 기록").
# 군집화 방식을 바꾸면 @ 뒤 번호를 올려 이전 결과와 구분한다
ANALYZER = "local-umap-hdbscan@1"


def next_month(month: str) -> str:
    """YYYY-MM의 다음 달.

    파라미터:
        month: YYYY-MM 문자열
    """
    year, mon = int(month[:4]), int(month[5:7])
    # 12월이면 mon // 12 = 1로 해가 넘어가고, mon % 12 + 1 = 1월이 된다
    return f"{year + mon // 12:04d}-{mon % 12 + 1:02d}"


def spans_consecutive_months(months: set[str]) -> bool:
    """서로 이어진 두 달이 함께 들어 있는지(PRD 5.3 "2개월 연속 등장").

    파라미터:
        months: 군집 민원들의 접수월(YYYY-MM) 집합
    """
    # 어떤 달의 다음 달이 집합에 있으면 연속. 정렬하지 않아도 되고 해 넘김(12월→1월)도 잡힌다
    return any(next_month(month) in months for month in months)


def cohesion(members: Vectors) -> float:
    """군집 응집도: 소속 민원과 (정규화한) 평균 벡터의 평균 코사인 유사도. 높을수록 한 주제다.

    파라미터:
        members: 군집 민원의 정규화된 임베딩
    """
    # 평균 벡터는 길이가 1보다 짧으므로 다시 정규화해야 내적이 코사인 유사도가 된다
    centroid = normalize(members.mean(axis=0, keepdims=True))[0]
    return float((members @ centroid).mean())


def cluster_labels(vectors: Vectors, config: DiscoveryConfig) -> list[int]:
    """민원마다 군집 번호를 매긴다. -1은 잡음(어느 군집에도 들지 않음).

    건수가 umap_min_records 이상이면 UMAP(코사인)으로 줄인 뒤, 아니면 그대로 HDBSCAN에 넣는다.

    파라미터:
        vectors: 정규화된 임베딩(건수 × 차원)
        config: 군집화 설정
    """
    count = len(vectors)
    # 최소 군집 크기보다 적으면 어떤 군집도 만들 수 없으므로 전부 잡음(미배정)으로 둔다
    if count < config.cluster_min_size:
        return [-1] * count
    points: Vectors = vectors
    # 고차원(bge-m3 1024차원)에서는 거리 차이가 고르게 퍼져 밀도 기반인 HDBSCAN이 군집을 잘
    # 못 찾으므로 UMAP으로 저차원으로 줄인다. 건수가 너무 적으면 UMAP 이웃 그래프가 불안정해
    # 원래 벡터를 그대로 쓴다
    if count >= config.umap_min_records:
        # n_neighbors·n_components는 건수보다 작아야 UMAP이 돈다(스펙트럴 초기화 제약).
        # min_dist=0은 점을 촘촘히 모아 군집화에 유리하게 하고, random_state는 재현성용
        reducer = UMAP(
            n_neighbors=min(config.umap_neighbors, count - 1),
            n_components=min(config.umap_components, count - 2),
            metric="cosine",
            min_dist=0.0,
            random_state=config.random_state,
        )
        points = np.asarray(reducer.fit_transform(vectors), dtype=np.float32)
    # HDBSCAN은 군집 수를 미리 정하지 않고, 밀도가 낮은 점은 -1(잡음)로 남긴다(PRD 6장).
    # copy=True는 입력 배열을 건드리지 않게 한다
    clusterer = HDBSCAN(min_cluster_size=config.cluster_min_size, copy=True)
    return [int(label) for label in clusterer.fit_predict(points)]


def discover(
    records: list[ComplaintRecord],
    vectors: Vectors,
    config: DiscoveryConfig,
    *,
    embedding_model: str,
    id_prefix: str = "CAND",
    tokenizer: Tokenizer = noun_tokens,
) -> list[CandidateCategory]:
    """미배정 민원을 군집화해 후보 기준을 넘는 군집을 후보로 돌려준다(큰 군집부터).

    파라미터:
        records: 미배정 풀의 민원(마스킹본). vectors와 같은 순서
        vectors: records의 정규화된 임베딩
        config: 군집화·후보 기준
        embedding_model: 벡터를 만든 임베딩 모델 이름(후보에 기록)
        id_prefix: 후보 ID 앞부분(예: 2026-03 → CAND-2026-03-01)
        tokenizer: 핵심어용 단어 분리기
    """
    # 1) 군집 번호별로 소속 민원 위치를 모은다. 잡음(-1)은 후보가 되지 않고 풀에 남는다
    labels = cluster_labels(vectors, config)
    clusters: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        if label >= 0:
            clusters.setdefault(label, []).append(index)

    # 2) 후보 기준(PRD 5.3)을 넘는 군집만 고른다. 큰 군집부터, 크기가 같으면 첫 민원 위치 순으로
    #    정렬해 실행할 때마다 후보 번호가 같게 한다
    chosen: list[tuple[list[int], CandidateReason]] = []
    for members in sorted(clusters.values(), key=lambda m: (-len(m), m[0])):
        if cohesion(vectors[members]) < config.min_cohesion:
            continue  # 여러 주제가 섞인 군집. 풀에 남겨 다음 달에 다시 본다
        # 한두 건짜리 특이 민원으로 카테고리가 늘지 않도록 최소 규모를 두되, 작아도 두 달 이어
        # 나오면 일회성이 아니라고 보고 후보로 올린다(PRD 5.3)
        if len(members) >= config.candidate_min_size:
            chosen.append((members, "size"))
        elif spans_consecutive_months({m for i in members if (m := records[i].month)}):
            chosen.append((members, "consecutive"))

    # 3) 핵심어는 고른 후보끼리만 비교해 뽑는다. 다른 후보에도 흔한 낱말은 c-TF-IDF가 낮춘다
    keywords = class_tfidf(
        [[records[i].text for i in members] for members, _ in chosen],
        config.keyword_count,
        tokenizer,
    )
    # 4) 후보마다 중심·대표 민원·응집도를 붙인다. 중심은 승인 때 카테고리 중심이 된다(PRD 5.2)
    candidates: list[CandidateCategory] = []
    for number, ((members, reason), words) in enumerate(zip(chosen, keywords, strict=True), 1):
        centroid = normalize(vectors[members].mean(axis=0, keepdims=True))[0]
        closeness = vectors[members] @ centroid
        # 중심에 가장 가까운 민원이 군집을 가장 잘 대표한다고 보고 담당자에게 보여 준다
        exemplars = [members[i] for i in np.argsort(-closeness)[: config.exemplar_count]]
        candidates.append(
            CandidateCategory(
                candidate_id=f"{id_prefix}-{number:02d}",
                member_ids=[records[i].record_id for i in members],
                keywords=words,
                exemplar_ids=[records[i].record_id for i in exemplars],
                centroid=[float(x) for x in centroid],
                reason=reason,
                cohesion=round(float(closeness.mean()), 4),
                analyzer=ANALYZER,
                embedding_model=embedding_model,
            )
        )
    return candidates
