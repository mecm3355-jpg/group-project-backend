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

ANALYZER = "local-umap-hdbscan@1"


def next_month(month: str) -> str:
    """YYYY-MM의 다음 달.

    파라미터:
        month: YYYY-MM 문자열
    """
    year, mon = int(month[:4]), int(month[5:7])
    return f"{year + mon // 12:04d}-{mon % 12 + 1:02d}"


def spans_consecutive_months(months: set[str]) -> bool:
    """서로 이어진 두 달이 함께 들어 있는지(PRD 5.3 "2개월 연속 등장").

    파라미터:
        months: 군집 민원들의 접수월(YYYY-MM) 집합
    """
    return any(next_month(month) in months for month in months)


def cohesion(members: Vectors) -> float:
    """군집 응집도: 소속 민원과 (정규화한) 평균 벡터의 평균 코사인 유사도. 높을수록 한 주제다.

    파라미터:
        members: 군집 민원의 정규화된 임베딩
    """
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
    if count < config.cluster_min_size:
        return [-1] * count
    points: Vectors = vectors
    if count >= config.umap_min_records:
        reducer = UMAP(
            n_neighbors=min(config.umap_neighbors, count - 1),
            n_components=min(config.umap_components, count - 2),
            metric="cosine",
            min_dist=0.0,
            random_state=config.random_state,
        )
        points = np.asarray(reducer.fit_transform(vectors), dtype=np.float32)
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
    labels = cluster_labels(vectors, config)
    clusters: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        if label >= 0:
            clusters.setdefault(label, []).append(index)

    chosen: list[tuple[list[int], CandidateReason]] = []
    for members in sorted(clusters.values(), key=lambda m: (-len(m), m[0])):
        if cohesion(vectors[members]) < config.min_cohesion:
            continue  # 여러 주제가 섞인 군집. 풀에 남겨 다음 달에 다시 본다
        if len(members) >= config.candidate_min_size:
            chosen.append((members, "size"))
        elif spans_consecutive_months({m for i in members if (m := records[i].month)}):
            chosen.append((members, "consecutive"))

    keywords = class_tfidf(
        [[records[i].text for i in members] for members, _ in chosen],
        config.keyword_count,
        tokenizer,
    )
    candidates: list[CandidateCategory] = []
    for number, ((members, reason), words) in enumerate(zip(chosen, keywords, strict=True), 1):
        centroid = normalize(vectors[members].mean(axis=0, keepdims=True))[0]
        closeness = vectors[members] @ centroid
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
