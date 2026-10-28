import numpy as np

from app.core.analysis_config import AnalysisConfig, AssignmentConfig, DiscoveryConfig
from app.schemas.complaint_analysis import ComplaintRecord
from app.services.embedding import normalize
from app.services.simulation import meets_targets, run_simulation


def words(text: str) -> list[str]:
    """띄어쓰기로 나누는 단순 단어 분리기.

    파라미터:
        text: 본문
    """
    return text.split()


def test_simulation_assigns_known_types_and_finds_new_types() -> None:
    # 1월: A·B 유형 8건씩, 2월: A·B 4건씩 + 처음 나온 C·D 유형 8건씩
    rng = np.random.default_rng(0)
    plan = [("2026-01", "A", 8), ("2026-01", "B", 8)]
    plan += [("2026-02", "A", 4), ("2026-02", "B", 4), ("2026-02", "C", 8), ("2026-02", "D", 8)]
    centers = {"A": 0, "B": 1, "C": 2, "D": 3}
    records: list[ComplaintRecord] = []
    gold: list[str] = []
    rows: list[np.ndarray] = []
    for month, label, count in plan:
        for _ in range(count):
            records.append(
                ComplaintRecord(record_id=f"R{len(records)}", text=f"{label}유형 민원", month=month)
            )
            gold.append(label)
            rows.append(np.eye(16)[centers[label]] + rng.normal(0, 0.03, 16))
    config = AnalysisConfig(
        assignment=AssignmentConfig(threshold=0.8, ambiguity_margin=0.05),
        discovery=DiscoveryConfig(umap_min_records=1000),  # 축소 없이 바로 군집화
    )

    result = run_simulation(
        records, normalize(np.vstack(rows)), gold, config, embedding_model="t", tokenizer=words
    )

    january, february = result.months
    assert january.candidates == 2
    assert february.assigned == 8
    assert february.assigned_correct == 8
    assert february.unseen_type_records == 16
    assert february.unseen_type_absorbed == 0
    assert february.candidates == 2
    # 군집에서 잡음으로 빠진 민원이 있어도 승인 뒤 소급 배정돼 풀이 빈다
    assert february.approved_members + february.retro_assigned == 16
    assert february.pool_left == 0
    assert result.metrics["retroAssignmentAccuracy"] in (None, 1.0)
    assert {c.gold_label for c in result.categories} == {"A", "B", "C", "D"}
    assert all(c.gold_share == 1.0 for c in result.categories)
    assert result.new_type_found == {"C": True, "D": True}
    assert result.metrics["purity"] == 1.0
    assert result.metrics["assignmentAccuracy"] == 1.0
    assert result.metrics["newDetectionRate"] == 1.0
    assert result.metrics["typeCoverage"] == 1.0
    assert result.metrics["reviewRatio"] == round(16 / 24, 4)
    assert meets_targets(result.metrics) == {
        "purity": True,
        "typeCoverage": True,
        "assignmentAccuracy": True,
        "newDetectionRate": True,
        "reviewRatio": False,
    }
    assert result.to_json()["metrics"] == result.metrics


def test_warmup_builds_first_taxonomy_from_several_months() -> None:
    rng = np.random.default_rng(1)
    records = [
        ComplaintRecord(record_id=f"R{i}", text="A유형 민원", month=f"2026-0{i % 3 + 1}")
        for i in range(12)
    ]
    vectors = normalize(np.eye(8)[0] + rng.normal(0, 0.03, (12, 8)))
    config = AnalysisConfig(discovery=DiscoveryConfig(umap_min_records=1000))

    result = run_simulation(
        records, vectors, ["A"] * 12, config, embedding_model="t", tokenizer=words, warmup_months=2
    )

    # 1·2월은 한 번에(2월로 기록), 3월부터 월별로 운영한다
    assert [m.month for m in result.months] == ["2026-02", "2026-03"]
    assert result.months[0].received == 8
