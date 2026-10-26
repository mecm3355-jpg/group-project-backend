"""월별 운영 시뮬레이션(PRD 9장). 정답 대분류가 있는 민원(가상민원)으로 운영을 재현해 지표를 잰다.

매달: 기존 카테고리에 배정 → 미배정 풀 군집화 → 후보 승인 → 체계 갱신(PRD 4장, 5.1).
- 담당자 승인은 "후보를 모두 승인"으로 가정한다. 승인된 후보의 민원은 소급해 배정한다.
- 애매 건은 사람이 검토해 확정한다고 보고 풀에 넣지 않는다.
- 카테고리의 정답은 승인 시점 소속 민원의 최다 정답 대분류로 본다(평가용).
DB에 저장하지 않고 메모리에서만 계산한다(민원 전용 컬렉션 미확정).
"""

from collections import Counter
from dataclasses import asdict, dataclass, field

import numpy as np
from pydantic import JsonValue
from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score

from app.core.analysis_config import AnalysisConfig
from app.schemas.complaint_analysis import Category, ComplaintRecord
from app.services.assignment import assign
from app.services.discovery import discover
from app.services.embedding import Vectors
from app.services.keywords import Tokenizer, noun_tokens

# PRD 9장 목표(초기 가설)
TARGETS = {
    "purity": 0.80,
    "typeCoverage": 0.80,
    "assignmentAccuracy": 0.90,
    "newDetectionRate": 0.80,
    "reviewRatio": 0.25,
}


@dataclass
class MonthStats:
    """한 달의 처리 결과."""

    month: str
    received: int
    assigned: int = 0
    assigned_correct: int = 0
    ambiguous: int = 0
    unassigned: int = 0
    # 아직 체계에 없는 정답 유형의 민원 수와, 그중 기존 카테고리로 흡수(자동 배정)된 수
    unseen_type_records: int = 0
    unseen_type_absorbed: int = 0
    pool_size: int = 0
    candidates: int = 0
    approved_members: int = 0
    # 승인 뒤 풀에서 소급 배정된 수와 그중 맞은 수
    retro_assigned: int = 0
    retro_correct: int = 0
    pool_left: int = 0
    taxonomy_size: int = 0
    # 이번 달 군집 품질(승인 후보 소속 민원 기준)
    purity: float | None = None
    ari: float | None = None
    nmi: float | None = None


@dataclass
class CategoryInfo:
    """승인된 카테고리의 평가 정보."""

    category_id: str
    month: str
    size: int
    keywords: list[str]
    gold_label: str
    gold_share: float


@dataclass
class SimulationResult:
    """시뮬레이션 전체 결과와 PRD 9장 지표."""

    months: list[MonthStats] = field(default_factory=list)
    categories: list[CategoryInfo] = field(default_factory=list)
    metrics: dict[str, float | None] = field(default_factory=dict)
    # 첫 등장 월 이후 그 달 또는 다음 달에 카테고리로 발견됐는지(유형별)
    new_type_found: dict[str, bool] = field(default_factory=dict)

    def to_json(self) -> dict[str, JsonValue]:
        """리포트 파일에 넣을 JSON 형태.

        파라미터: 없음
        """
        return {
            "metrics": dict(self.metrics),
            "newTypeFound": dict(self.new_type_found),
            "months": [asdict(month) for month in self.months],
            "categories": [asdict(category) for category in self.categories],
        }


def _ratio(numerator: int, denominator: int) -> float | None:
    """비율(분모가 0이면 None).

    파라미터:
        numerator: 분자
        denominator: 분모
    """
    return round(numerator / denominator, 4) if denominator else None


def _cluster_quality(
    members: list[list[int]], gold: list[str]
) -> tuple[float | None, float | None, float | None]:
    """군집들의 순도·ARI·NMI(군집에 든 민원만 대상).

    파라미터:
        members: 군집별 민원 위치 목록
        gold: 위치별 정답 대분류
    """
    flat = [(cluster, gold[i]) for cluster, items in enumerate(members) for i in items]
    if not flat:
        return None, None, None
    majority = sum(Counter(gold[i] for i in items).most_common(1)[0][1] for items in members)
    predicted = [cluster for cluster, _ in flat]
    truth = [label for _, label in flat]
    return (
        round(majority / len(flat), 4),
        round(float(adjusted_rand_score(truth, predicted)), 4),
        round(float(normalized_mutual_info_score(truth, predicted)), 4),
    )


def run_simulation(
    records: list[ComplaintRecord],
    vectors: Vectors,
    gold: list[str],
    config: AnalysisConfig,
    *,
    embedding_model: str,
    tokenizer: Tokenizer = noun_tokens,
    warmup_months: int = 1,
) -> SimulationResult:
    """접수월 순서대로 운영을 재현하고 PRD 9장 지표를 계산한다.

    파라미터:
        records: 접수월(month)이 있는 마스킹된 민원
        vectors: records의 정규화된 임베딩(같은 순서)
        gold: records의 정답 대분류(같은 순서)
        config: 분석 기준값
        embedding_model: 임베딩 모델 이름
        tokenizer: 핵심어용 단어 분리기
        warmup_months: 첫 체계(v1)를 만들 때 모을 개월 수. 2 이상이면 처음 그만큼의 달을
            한 번에 군집화하고(그 마지막 달 이름으로 기록), 그다음 달부터 월별로 운영한다
    """
    all_months = sorted({record.month for record in records if record.month})
    warmup = set(all_months[:warmup_months])
    first_period = all_months[: max(warmup_months, 1)][-1] if all_months else ""
    by_month: dict[str, list[int]] = {}
    for index, record in enumerate(records):
        if record.month:
            period = first_period if record.month in warmup else record.month
            by_month.setdefault(period, []).append(index)
    first_seen: dict[str, str] = {}
    for month in sorted(by_month):
        for index in by_month[month]:
            first_seen.setdefault(gold[index], month)

    result = SimulationResult()
    taxonomy: list[Category] = []
    category_gold: dict[str, str] = {}
    approved_clusters: list[list[int]] = []
    found_month: dict[str, str] = {}
    pool: list[int] = []
    position = {record.record_id: index for index, record in enumerate(records)}

    for month in sorted(by_month):
        incoming = by_month[month]
        stats = MonthStats(month=month, received=len(incoming))
        known = set(category_gold.values())
        assignments = assign(
            [records[i] for i in incoming], vectors[incoming], taxonomy, config.assignment
        )
        for index, assignment in zip(incoming, assignments, strict=True):
            unseen = gold[index] not in known
            stats.unseen_type_records += unseen
            if assignment.status == "assigned" and assignment.category_id is not None:
                stats.assigned += 1
                stats.assigned_correct += category_gold[assignment.category_id] == gold[index]
                stats.unseen_type_absorbed += unseen
            elif assignment.status == "ambiguous":
                stats.ambiguous += 1
            else:
                stats.unassigned += 1
                pool.append(index)

        stats.pool_size = len(pool)
        candidates = discover(
            [records[i] for i in pool],
            vectors[pool],
            config.discovery,
            embedding_model=embedding_model,
            id_prefix=f"CAND-{month}",
            tokenizer=tokenizer,
        )
        stats.candidates = len(candidates)
        month_clusters: list[list[int]] = []
        for candidate in candidates:
            members = [position[record_id] for record_id in candidate.member_ids]
            labels = Counter(gold[i] for i in members)
            label, share = labels.most_common(1)[0]
            category = Category(
                category_id=f"CAT-{len(taxonomy) + 1:04d}",
                name=" ".join(candidate.keywords[:3]),
                centroid=candidate.centroid,
                created_in_version=len(result.months) + 1,
            )
            taxonomy.append(category)
            category_gold[category.category_id] = label
            found_month.setdefault(label, month)
            month_clusters.append(members)
            result.categories.append(
                CategoryInfo(
                    category_id=category.category_id,
                    month=month,
                    size=len(members),
                    keywords=candidate.keywords,
                    gold_label=label,
                    gold_share=round(share / len(members), 4),
                )
            )
        approved = {i for members in month_clusters for i in members}
        pool = [i for i in pool if i not in approved]
        approved_clusters.extend(month_clusters)
        stats.approved_members = len(approved)
        if month_clusters and pool:
            # 승인 뒤 소급 배정(PRD 4장 6단계): 잡음으로 빠졌던 풀 민원도 새 체계로 다시 본다
            retro = assign([records[i] for i in pool], vectors[pool], taxonomy, config.assignment)
            kept: list[int] = []
            for index, assignment in zip(pool, retro, strict=True):
                if assignment.status == "assigned" and assignment.category_id is not None:
                    stats.retro_assigned += 1
                    stats.retro_correct += category_gold[assignment.category_id] == gold[index]
                else:
                    kept.append(index)
            pool = kept
        stats.pool_left = len(pool)
        stats.taxonomy_size = len(taxonomy)
        stats.purity, stats.ari, stats.nmi = _cluster_quality(month_clusters, gold)
        result.months.append(stats)

    months = sorted(by_month)
    later = result.months[1:]
    received_later = sum(m.received for m in later)
    assigned = sum(m.assigned for m in later)
    purity, ari, nmi = _cluster_quality(approved_clusters, gold)
    found_types = set(category_gold.values())
    for label, month in first_seen.items():
        if month == months[0]:
            continue
        index = months.index(month)
        window = set(months[index : index + 2])
        result.new_type_found[label] = found_month.get(label) in window
    result.metrics = {
        "purity": purity,
        "ari": ari,
        "nmi": nmi,
        "typeCoverage": _ratio(len(found_types), len(set(gold))),
        "assignmentAccuracy": _ratio(sum(m.assigned_correct for m in later), assigned),
        "newDetectionRate": _ratio(
            sum(m.unseen_type_records - m.unseen_type_absorbed for m in later),
            sum(m.unseen_type_records for m in later),
        ),
        "newTypeFoundRate": _ratio(sum(result.new_type_found.values()), len(result.new_type_found)),
        "reviewRatio": _ratio(sum(m.ambiguous + m.unassigned for m in later), received_later),
        "autoAssignedRatio": _ratio(assigned, received_later),
        "retroAssignmentAccuracy": _ratio(
            sum(m.retro_correct for m in result.months),
            sum(m.retro_assigned for m in result.months),
        ),
        "finalUnassignedRatio": _ratio(len(pool), len(records)),
        "categoryCount": float(len(taxonomy)),
        "meanCategorySize": round(float(np.mean([c.size for c in result.categories])), 2)
        if result.categories
        else None,
    }
    return result


def meets_targets(metrics: dict[str, float | None]) -> dict[str, bool]:
    """PRD 9장 목표를 지표마다 달성했는지(reviewRatio는 이하, 나머지는 이상).

    파라미터:
        metrics: run_simulation 결과의 metrics
    """
    passed: dict[str, bool] = {}
    for name, target in TARGETS.items():
        value = metrics.get(name)
        if value is None:
            passed[name] = False
        elif name == "reviewRatio":
            passed[name] = value <= target
        else:
            passed[name] = value >= target
    return passed
