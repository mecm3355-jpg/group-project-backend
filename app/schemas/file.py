from datetime import datetime
from typing import Literal

from bson import ObjectId
from pydantic import Field

from app.schemas.mongo import MongoDocument, MongoModel, utc_now

FileCategory = Literal["tabular", "text", "image", "document", "other"]
# files.status 값 목록은 설계 파일에 없어 processing_jobs.status와 같게 둔다(PRD 7.7)
FileStatus = Literal["queued", "processing", "completed", "failed"]


class FileStorage(MongoModel):
    """files.storage — 저장소 종류와 파일 위치."""

    provider: str
    key: str


class ProcessingCapabilities(MongoModel):
    """files.processingCapabilities — 해당 파일에 지원되는 처리 기능."""

    preview: bool
    quality_check: bool
    cleaning: bool
    statistics: bool
    ai_summary: bool


class FileDocument(MongoDocument):
    """files 컬렉션 문서(docs/db-schema/schema.md의 files 표)."""

    original_name: str
    extension: str
    mime_type: str
    category: FileCategory
    size_bytes: int
    storage: FileStorage
    status: FileStatus
    processing_capabilities: ProcessingCapabilities
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_job_id: ObjectId | None = None
