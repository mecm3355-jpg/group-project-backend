from datetime import datetime

from app.schemas.common import ApiModel
from app.schemas.job import JobDocument, JobError, JobStatus


class JobDetail(ApiModel):
    """GET /api/v1/jobs/{job_id} 응답 — 작업 상태와 오류.

    DB 필드 field·createAt(표 기준)은 응답에서 fileId·createdAt으로 내보낸다
    (설계 파일 예시 JSON의 이름, files 응답과 같은 이름).
    """

    job_id: str
    file_id: str
    type: str
    status: JobStatus
    progress: int
    stage: str
    error: JobError | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_document(cls, doc: JobDocument) -> "JobDetail":
        """processing_jobs 문서를 API 응답으로 바꾼다.

        파라미터:
            doc: processing_jobs 컬렉션 문서
        """
        return cls(
            job_id=str(doc.id),
            file_id=str(doc.field),
            type=doc.type,
            status=doc.status,
            progress=doc.progress,
            stage=doc.stage,
            error=doc.error,
            started_at=doc.started_at,
            completed_at=doc.completed_at,
            created_at=doc.create_at,
        )
