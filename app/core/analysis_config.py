"""민원 분석 기준값(config/analysis.toml). 비밀 값이 아니므로 .env가 아닌 설정 파일에 둔다."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import AppError

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "analysis.toml"


class _Section(BaseModel):
    """설정 구역 공통: 오타 난 키를 조용히 무시하지 않도록 모르는 키는 거부한다."""

    model_config = ConfigDict(extra="forbid")


class AssignmentConfig(_Section):
    """카테고리 배정 기준(PRD 5.3)."""

    threshold: float = Field(default=0.75, ge=-1, le=1)
    ambiguity_margin: float = Field(default=0.05, ge=0)


class DiscoveryConfig(_Section):
    """미배정 풀 군집화와 후보 기준(PRD 5.3, 6장)."""

    candidate_min_size: int = Field(default=5, ge=2)
    cluster_min_size: int = Field(default=3, ge=2)
    # [M2 제안, PRD 5.3 외] 소속 민원과 중심의 평균 유사도가 이 값 미만인 군집은
    # 후보로 올리지 않는다. 0이면 끈다
    min_cohesion: float = Field(default=0.0, ge=0, le=1)
    umap_min_records: int = Field(default=15, ge=3)
    umap_neighbors: int = Field(default=15, ge=2)
    umap_components: int = Field(default=5, ge=2)
    random_state: int = 42
    keyword_count: int = Field(default=10, ge=1)
    exemplar_count: int = Field(default=5, ge=1)


class AttributeConfig(_Section):
    """규칙 속성 보강(임베딩 원형 분류) 기준. 규칙에 걸리는 낱말이 없을 때만 쓴다."""

    # 원형 문장 중심과의 유사도가 이 값 이상이고 1·2위 차이가 margin 이상일 때만 채운다
    prototype_min_similarity: float = Field(default=0.45, ge=-1, le=1)
    prototype_margin: float = Field(default=0.01, ge=0)


class SpikeConfig(_Section):
    """급증 판정 기준(PRD 8.2, 9장 통계·급증)."""

    ratio: float = Field(default=2.0, gt=0)
    baseline_months: int = Field(default=3, ge=1)
    min_count: int = Field(default=5, ge=0)
    yoy_ratio: float | None = Field(default=1.5, gt=0)
    min_month_coverage: float = Field(default=0.9, gt=0, le=1)


class IssueConfig(_Section):
    """이슈 묶기와 반복 이슈 기준(PRD 4장 7단계, 8.2)."""

    radius_meters: float = Field(default=100, gt=0)
    text_similarity: float = Field(default=0.8, ge=-1, le=1)
    repeat_min_count: int = Field(default=3, ge=2)


class TaxonomyConfig(_Section):
    """분기 정리 기준(PRD 5.3)."""

    merge_similarity: float = Field(default=0.85, ge=-1, le=1)


class AnalysisConfig(_Section):
    """민원 분석 기준값 전체."""

    assignment: AssignmentConfig = AssignmentConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    attributes: AttributeConfig = AttributeConfig()
    spike: SpikeConfig = SpikeConfig()
    issue: IssueConfig = IssueConfig()
    taxonomy: TaxonomyConfig = TaxonomyConfig()


def load_analysis_config(path: Path = DEFAULT_PATH) -> AnalysisConfig:
    """기준값 파일을 읽는다. 파일에 없는 값은 기본값을 쓴다.

    파일이 없거나 TOML 형식이 아니거나 값이 범위를 벗어나면 AppError(INVALID_ANALYSIS_CONFIG).

    파라미터:
        path: TOML 파일 경로(기본: 저장소의 config/analysis.toml)
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return AnalysisConfig.model_validate(data)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise AppError(
            "INVALID_ANALYSIS_CONFIG",
            f"분석 기준값 파일을 읽을 수 없습니다({path.name}): {exc}",
            500,
        ) from exc
