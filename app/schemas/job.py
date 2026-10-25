from datetime import datetime
from typing import Literal

from bson import ObjectId
from pydantic import Field

from app.schemas.mongo import MongoDocument, MongoModel, utc_now

JobStatus = Literal["queued", "processing", "completed", "failed"]

# 데이터 추출·품질 진단 작업. 업로드할 때와 POST /files/{file_id}/process 때 만든다(PRD 7.2)
ANALYZE_JOB_TYPE = "analyze"
# 승인한 정제 규칙 적용 작업. POST /files/{file_id}/clean 때 만든다
CLEAN_JOB_TYPE = "clean"


class JobError(MongoModel):
    """processing_jobs.error — 오류 코드와 메시지."""

    code: str
    message: str


class JobDocument(MongoDocument):
    """processing_jobs 컬렉션 문서(schema.md의 processing_jobs 표).

    대상 파일 ID는 표 기준 `field`, 생성 시간은 `createAt`이다.
    예시 JSON의 fileId·createdAt과 다르며 설계 작성자 확인 대기 중(PRD 7.7).
    """

    field: ObjectId
    type: str
    status: JobStatus = "queued"
    progress: int = Field(default=0, ge=0, le=100)
    stage: str = "queued"
    error: JobError | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    create_at: datetime = Field(default_factory=utc_now)
