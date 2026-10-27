from datetime import datetime

from app.schemas.common import ApiModel
from app.schemas.job import JobStatus
from app.schemas.result import QualityIssue, ResultDocument


class ProcessStartResult(ApiModel):
    """POST /api/v1/files/{file_id}/process 응답 — 새로 만든 추출·진단 작업."""

    job_id: str
    file_id: str
    status: JobStatus
    message: str


class QualityReport(ApiModel):
    """GET /api/v1/files/{file_id}/quality 응답 — 최신 분석 결과의 품질 진단."""

    file_id: str
    job_id: str
    # 어느 분석 버전의 진단인지. 재처리할 때마다 올라간다
    version: int
    score: int | None
    issues: list[QualityIssue]
    # 표 형식만 채운다: 완전성·유일성·유효성·일관성(0~100)과 점수 계산 기준
    metrics: dict[str, float] | None
    score_basis: str | None
    created_at: datetime

    @classmethod
    def from_document(cls, doc: ResultDocument) -> "QualityReport":
        """processing_results 문서에서 품질 진단 부분을 꺼낸다.

        파라미터:
            doc: resultType이 analysis인 결과 문서
        """
        return cls(
            file_id=str(doc.field),
            job_id=str(doc.job_id),
            version=doc.version,
            score=doc.quality.score,
            issues=doc.quality.issues,
            metrics=doc.quality.metrics,
            score_basis=doc.quality.score_basis,
            # DB 표 기준 이름(createAt)을 응답 이름(createdAt)으로 바꾼다
            created_at=doc.create_at,
        )
