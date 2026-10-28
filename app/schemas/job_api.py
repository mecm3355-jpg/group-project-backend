from datetime import datetime

from app.schemas.common import ApiModel
from app.schemas.job import JobDocument, JobError, JobStatus


class JobDetail(ApiModel):
    """GET /api/v1/jobs/{job_id} 응답 — 작업 상태와 오류.

    DB 필드 field·createAt(표 기준)은 응답에서 fileId·createdAt으로 내보낸다
    (설계 파일 예시 JSON의 이름, files 응답과 같은 이름).
    """

    job_id: str
    # DB의 field(대상 파일 ID)를 명세 예시 이름 fileId로 내보낸다
    file_id: str
    type: str
    status: JobStatus
    # 0~100(%). 프런트엔드 진행 막대에 그대로 쓴다
    progress: int
    stage: str
    # 실패했을 때만 채운다. code로 분기하고 message를 사용자에게 보여 준다
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
            # DB 표 기준 이름(createAt)을 응답 이름(createdAt)으로 바꾸는 곳
            created_at=doc.create_at,
        )
