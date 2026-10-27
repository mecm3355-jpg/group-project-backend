from app.schemas.common import ApiModel


class FileSummary(ApiModel):
    """전체 통계 중 파일 집계. 분류·상태는 값이 없어도 0으로 채운다."""

    total: int
    total_size_bytes: int
    by_category: dict[str, int]
    by_status: dict[str, int]


class JobSummary(ApiModel):
    """전체 통계 중 처리 작업 집계. 종류는 실제로 있는 값만 담는다."""

    total: int
    by_status: dict[str, int]
    by_type: dict[str, int]


class QualitySummary(ApiModel):
    """전체 통계 중 품질 진단 집계. 파일마다 최신 analysis 결과만 센다.

    averageScore는 지금 점수 기준으로 계산한 파일(표 형식)만의 평균이며, 없으면 null이다.
    outdatedScoreFileCount는 예전 점수 기준으로 진단해 평균에서 뺀 파일 수다(다시 처리하면 반영).
    issuesByType·issuesBySeverity는 문제 항목 수다(항목 안의 count 합이 아님).
    """

    analyzed_file_count: int
    scored_file_count: int
    outdated_score_file_count: int
    average_score: float | None
    issues_by_type: dict[str, int]
    issues_by_severity: dict[str, int]


class DashboardSummary(ApiModel):
    """GET /api/v1/dashboard/summary 응답 — 전체 통계."""

    files: FileSummary
    jobs: JobSummary
    quality: QualitySummary
