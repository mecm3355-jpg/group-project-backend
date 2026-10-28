from datetime import datetime
from typing import Literal

from bson import ObjectId
from pydantic import Field

from app.schemas.mongo import MongoDocument, MongoModel, utc_now

# schema.md files.category 설명의 다섯 값. other는 내용 분석을 지원하지 않는 형식
FileCategory = Literal["tabular", "text", "image", "document", "other"]
# files.status 값 목록은 설계 파일에 없어 processing_jobs.status와 같게 둔다(PRD 7.7)
FileStatus = Literal["queued", "processing", "completed", "failed"]


class FileStorage(MongoModel):
    """files.storage — 저장소 종류와 파일 위치."""

    # provider: 저장소 종류(현재 local), key: 저장소 안 상대 경로. 파일 내용은 DB에 넣지 않는다
    provider: str
    key: str


class ProcessingCapabilities(MongoModel):
    """files.processingCapabilities — 해당 파일에 지원되는 처리 기능."""

    # 형식마다 지원 기능이 달라(예: 이미지는 정제·통계 없음) 프런트엔드가 버튼을 켜고 끄는 데 쓴다
    preview: bool
    quality_check: bool
    cleaning: bool
    statistics: bool
    ai_summary: bool


class FileDocument(MongoDocument):
    """files 컬렉션 문서(docs/db-schema/schema.md의 files 표)."""

    original_name: str
    # 점 없는 소문자 확장자(예: csv). mimeType은 확장자가 아니라 실제 내용으로 감지한 값
    extension: str
    mime_type: str
    category: FileCategory
    # 바이트 단위
    size_bytes: int
    storage: FileStorage
    status: FileStatus
    processing_capabilities: ProcessingCapabilities
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    # 가장 최근에 만든 작업. 상태 확인 화면이 작업 목록을 뒤지지 않고 바로 찾게 한다
    last_job_id: ObjectId | None = None
