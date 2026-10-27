"""민원 분석 기준값(config/analysis.toml). 비밀 값이 아니므로 .env가 아닌 설정 파일에 둔다."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import AppError

# app/core/ 기준 세 단계 위가 저장소 루트. 실행 위치(cwd)와 무관하게 같은 파일을 찾는다
DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "analysis.toml"


class _Section(BaseModel):
    """설정 구역 공통: 오타 난 키를 조용히 무시하지 않도록 모르는 키는 거부한다."""

    model_config = ConfigDict(extra="forbid")


class AssignmentConfig(_Section):
    """카테고리 배정 기준(PRD 5.3)."""

    # 카테고리 중심과의 코사인 유사도가 이 값 이상이면 배정. 코사인 범위라 -1~1로 제한
    # 기본값은 PRD 초기값이고, 실제 값은 config/analysis.toml(M2 시뮬레이션으로 확정)을 따른다
    threshold: float = Field(default=0.75, ge=-1, le=1)
    # 1·2위 유사도 차이가 이 값 미만이면 어느 쪽인지 애매하므로 검토 대기로 돌린다
    ambiguity_margin: float = Field(default=0.05, ge=0)


class DiscoveryConfig(_Section):
    """미배정 풀 군집화와 후보 기준(PRD 5.3, 6장)."""

    # 이 건수 이상 묶인 군집만 신규 카테고리 후보. 1건짜리는 군집이 아니므로 최소 2
    candidate_min_size: int = Field(default=5, ge=2)
    # HDBSCAN 최소 군집 크기. 후보 기준보다 작은 군집은 2개월 연속 등장할 때만 후보가 된다
    cluster_min_size: int = Field(default=3, ge=2)
    # [M2 제안, PRD 5.3 외] 소속 민원과 중심의 평균 유사도가 이 값 미만인 군집은
    # 후보로 올리지 않는다. 0이면 끈다
    min_cohesion: float = Field(default=0.0, ge=0, le=1)
    # 건수가 적으면 UMAP 축소가 불안정하므로 이 건수 미만이면 축소 없이 바로 군집화
    umap_min_records: int = Field(default=15, ge=3)
    umap_neighbors: int = Field(default=15, ge=2)
    umap_components: int = Field(default=5, ge=2)
    # UMAP 난수 시드. 같은 입력이면 같은 후보가 나오도록 고정한다
    random_state: int = 42
    # categories.keywords 최대 10개, exemplarIds 5건(PRD 5.2 제안 컬렉션)에 맞춘 개수
    keyword_count: int = Field(default=10, ge=1)
    exemplar_count: int = Field(default=5, ge=1)


class AttributeConfig(_Section):
    """규칙 속성 보강(임베딩 원형 분류) 기준. 규칙에 걸리는 낱말이 없을 때만 쓴다."""

    # 원형 문장 중심과의 유사도가 이 값 이상이고 1·2위 차이가 margin 이상일 때만 채운다
    prototype_min_similarity: float = Field(default=0.45, ge=-1, le=1)
    prototype_margin: float = Field(default=0.01, ge=0)


class SpikeConfig(_Section):
    """급증 판정 기준(PRD 8.2, 9장 통계·급증)."""

    # 이번 달 건수가 직전 baseline_months개월 평균의 ratio배 이상이고 min_count건 이상이면 급증
    ratio: float = Field(default=2.0, gt=0)
    baseline_months: int = Field(default=3, ge=1)
    min_count: int = Field(default=5, ge=0)
    # 전년 같은 달 대비 배수 조건. 계절 정점을 급증으로 오탐하지 않기 위함이며 None이면 끈다
    yoy_ratio: float | None = Field(default=1.5, gt=0)
    # 처음·마지막 달이 그 달 일수의 이 비율 미만만 덮으면 일부 월로 보고 판정에서 뺀다
    min_month_coverage: float = Field(default=0.9, gt=0, le=1)


class IssueConfig(_Section):
    """이슈 묶기와 반복 이슈 기준(PRD 4장 7단계, 8.2)."""

    # 같은 이슈로 묶는 좌표 거리(미터)와 본문 유사도 기준
    radius_meters: float = Field(default=100, gt=0)
    text_similarity: float = Field(default=0.8, ge=-1, le=1)
    # 같은 이슈가 이 건수 이상이면 반복 이슈(PRD 8.2)
    repeat_min_count: int = Field(default=3, ge=2)


class TaxonomyConfig(_Section):
    """분기 정리 기준(PRD 5.3)."""

    # 카테고리 중심끼리 이 값 이상 비슷하면 병합 후보로 올린다
    merge_similarity: float = Field(default=0.85, ge=-1, le=1)


class AnalysisConfig(_Section):
    """민원 분석 기준값 전체."""

    # TOML에서 구역을 통째로 빼도 기본값으로 동작하도록 구역마다 기본 객체를 둔다
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
        # 한글 주석이 있으므로 인코딩을 명시해 읽는다(Windows 기본 cp949 회피)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return AnalysisConfig.model_validate(data)
    # 파일 없음·문법 오류·범위 초과(pydantic ValidationError는 ValueError)를 한 오류로 모은다.
    # 설정 문제는 사용자 요청 탓이 아니므로 500으로 알린다
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise AppError(
            "INVALID_ANALYSIS_CONFIG",
            f"분석 기준값 파일을 읽을 수 없습니다({path.name}): {exc}",
            500,
        ) from exc
