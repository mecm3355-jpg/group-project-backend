from datetime import datetime
from typing import Literal

from bson import ObjectId
from pydantic import Field

from app.schemas.mongo import MongoDocument, MongoModel, utc_now

# schema.md processing_jobs.status 설명의 네 값 그대로
JobStatus = Literal["queued", "processing", "completed", "failed"]

# 데이터 추출·품질 진단 작업. 업로드할 때와 POST /files/{file_id}/process 때 만든다(PRD 7.2)
ANALYZE_JOB_TYPE = "analyze"
# 승인한 정제 규칙 적용 작업. POST /files/{file_id}/clean 때 만든다
CLEAN_JOB_TYPE = "clean"
# AI 분석 리포트 작업. POST /files/{file_id}/report 때 만든다
REPORT_JOB_TYPE = "report"
# 여러 원본을 공통 필드로 합치는 작업. POST /files/merge 때 만든다(대상은 새 통합 파일)
MERGE_JOB_TYPE = "merge"


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
    # analyze·clean·report·merge·complaint_analysis 등. 처리기를 추가할 수 있도록 str로 둔다
    type: str
    status: JobStatus = "queued"
    progress: int = Field(default=0, ge=0, le=100)
    # 세부 단계(validating·parsing·quality_check 등, schema.md 비고). 실패하면 그 단계가 남는다
    stage: str = "queued"
    # 실패 원인 추적용(CLAUDE.md: processing_jobs는 실패 원인과 재처리 상태 추적)
    error: JobError | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    create_at: datetime = Field(default_factory=utc_now)
