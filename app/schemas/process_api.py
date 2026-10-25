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
    version: int
    score: int | None
    issues: list[QualityIssue]
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
            created_at=doc.create_at,
        )
