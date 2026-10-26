import numpy as np
import pytest

from app.core.analysis_config import AssignmentConfig, DiscoveryConfig
from app.schemas.complaint_analysis import Category, ComplaintRecord
from app.services.assignment import assign
from app.services.discovery import discover, next_month, spans_consecutive_months
from app.services.embedding import Vectors, normalize
from app.services.keywords import class_tfidf, noun_tokens


def blobs(sizes: list[int], dim: int = 32, spread: float = 0.05, seed: int = 0) -> Vectors:
    """서로 직교에 가까운 중심 주변에 모인 정규화 벡터 묶음(군집화 테스트용).

    파라미터:
        sizes: 묶음별 건수
        dim: 차원
        spread: 중심에서 흩어진 정도
        seed: 난수 시드
    """
    rng = np.random.default_rng(seed)
    centers = np.eye(dim)[: len(sizes)]
    parts = [centers[i] + rng.normal(0, spread, (size, dim)) for i, size in enumerate(sizes)]
    return normalize(np.vstack(parts))


def words(text: str) -> list[str]:
    """띄어쓰기로 나누는 단순 단어 분리기(형태소 분석 없이 테스트).

    파라미터:
        text: 본문
    """
    return text.split()


def records_for(sizes: list[int], months: list[str] | None = None) -> list[ComplaintRecord]:
    """묶음마다 같은 단어를 쓰는 민원 목록.

    파라미터:
        sizes: 묶음별 건수
        months: 민원별 접수월(없으면 None)
    """
    topics = ["가로등 꺼짐", "쓰레기 무단투기", "불법 주차"]
    records: list[ComplaintRecord] = []
    for group, size in enumerate(sizes):
        for _ in range(size):
            index = len(records)
            records.append(
                ComplaintRecord(
                    record_id=f"R{index}",
                    text=f"{topics[group]} 신고",
                    month=months[index] if months else None,
                )
            )
    return records


def test_discovers_separated_groups() -> None:
    sizes = [12, 10, 8]
    candidates = discover(
        records_for(sizes), blobs(sizes), DiscoveryConfig(), embedding_model="test", tokenizer=words
    )

    assert [len(c.member_ids) for c in candidates] == sizes
    assert {m[1:] for m in candidates[0].member_ids} == {str(i) for i in range(12)}
    assert candidates[0].keywords[:2] == ["가로등", "꺼짐"]
    assert len(candidates[0].exemplar_ids) == 5
    assert all(c.reason == "size" for c in candidates)
    assert abs(np.linalg.norm(candidates[0].centroid) - 1) < 1e-5


def test_small_cluster_needs_consecutive_months() -> None:
    sizes = [10, 3]
    small_months = ["2026-01"] * 10 + ["2026-03", "2026-04", "2026-04"]
    scattered = ["2026-01"] * 10 + ["2026-01", "2026-03", "2026-05"]
    config = DiscoveryConfig(umap_min_records=100)  # 축소 없이 바로 군집화

    consecutive = discover(
        records_for(sizes, small_months), blobs(sizes), config, embedding_model="t", tokenizer=words
    )
    not_consecutive = discover(
        records_for(sizes, scattered), blobs(sizes), config, embedding_model="t", tokenizer=words
    )

    assert [c.reason for c in consecutive] == ["size", "consecutive"]
    assert [c.reason for c in not_consecutive] == ["size"]


def test_low_cohesion_clusters_are_held_back() -> None:
    sizes = [12, 10]
    vectors = blobs(sizes, spread=0.15)

    loose = discover(
        records_for(sizes),
        vectors,
        DiscoveryConfig(umap_min_records=100, min_cohesion=0.999),
        embedding_model="t",
        tokenizer=words,
    )
    kept = discover(
        records_for(sizes),
        vectors,
        DiscoveryConfig(umap_min_records=100),
        embedding_model="t",
        tokenizer=words,
    )

    assert loose == []
    assert len(kept) == 2
    assert 0.5 < kept[0].cohesion < 0.999


def test_too_few_records_make_no_candidates() -> None:
    candidates = discover(
        records_for([2]), blobs([2]), DiscoveryConfig(), embedding_model="t", tokenizer=words
    )

    assert candidates == []


def test_month_helpers() -> None:
    assert next_month("2026-12") == "2027-01"
    assert next_month("2026-03") == "2026-04"
    assert spans_consecutive_months({"2026-12", "2027-01"})
    assert not spans_consecutive_months({"2026-01", "2026-03"})


def category(category_id: str, vector: list[float]) -> Category:
    """테스트용 카테고리.

    파라미터:
        category_id: 카테고리 ID
        vector: 중심 벡터(정규화해서 넣는다)
    """
    centroid = normalize(np.asarray([vector]))[0]
    return Category(
        category_id=category_id,
        name=category_id,
        centroid=[float(x) for x in centroid],
        created_in_version=1,
    )


@pytest.mark.parametrize(
    ("vector", "status", "category_id"),
    [
        ([1.0, 0.1, 0.0], "assigned", "A"),  # A와 매우 가깝다
        ([1.0, 1.0, 0.0], "ambiguous", "A"),  # A·B 사이(차이 0)
        ([0.0, 0.0, 1.0], "unassigned", "A"),  # 어느 쪽과도 멀다(1위는 기록)
    ],
)
def test_assign_status(vector: list[float], status: str, category_id: str) -> None:
    taxonomy = [category("A", [1, 0, 0]), category("B", [0, 1, 0])]
    vectors = normalize(np.asarray([vector]))

    [result] = assign(
        [ComplaintRecord(record_id="X", text="t")],
        vectors,
        taxonomy,
        AssignmentConfig(threshold=0.6, ambiguity_margin=0.05),
    )

    assert result.status == status
    assert result.category_id == category_id
    assert result.second_category_id == "B"


def test_assign_without_taxonomy_is_unassigned() -> None:
    [result] = assign(
        [ComplaintRecord(record_id="X", text="t")],
        normalize(np.asarray([[1.0, 0.0]])),
        [],
        AssignmentConfig(),
    )

    assert result.status == "unassigned"
    assert result.category_id is None


def test_class_tfidf_prefers_distinctive_words() -> None:
    groups = [["가로등 고장 민원", "가로등 꺼짐 민원"], ["쓰레기 투기 민원", "쓰레기 냄새 민원"]]

    keywords = class_tfidf(groups, top_n=1, tokenizer=words)

    assert keywords == [["가로등"], ["쓰레기"]]


def test_noun_tokens_skip_mask_tags_and_stopwords() -> None:
    tokens = noun_tokens("수청동 놀이터 가로등이 고장입니다. 연락처 [전화] 민원 부탁드립니다")

    assert "가로등" in tokens
    assert "놀이터" in tokens
    assert "전화" not in tokens
    assert "민원" not in tokens
