from datetime import datetime

from app.schemas.common import ApiModel
from app.schemas.file import FileCategory, FileDocument, FileStatus, ProcessingCapabilities


class FileUploadResult(ApiModel):
    """POST /api/v1/files 응답 — 명세서 "파일 등록 응답 예시"의 필드 그대로.

    fileType은 files 스키마에 없는 이름이라 확장자로 채운다(PRD 7.7 확인 대기).
    """

    file_id: str
    original_name: str
    file_type: str
    status: FileStatus
    message: str


class FileDetail(ApiModel):
    """파일 상세·목록 항목. 내부 저장 위치(storage)는 내보내지 않는다."""

    file_id: str
    original_name: str
    extension: str
    mime_type: str
    category: FileCategory
    size_bytes: int
    status: FileStatus
    processing_capabilities: ProcessingCapabilities
    created_at: datetime
    updated_at: datetime
    last_job_id: str | None

    @classmethod
    def from_document(cls, doc: FileDocument) -> "FileDetail":
        """files 문서를 API 응답으로 바꾼다. ObjectId는 문자열로 바꾼다.

        파라미터:
            doc: files 컬렉션 문서
        """
        return cls(
            file_id=str(doc.id),
            original_name=doc.original_name,
            extension=doc.extension,
            mime_type=doc.mime_type,
            category=doc.category,
            size_bytes=doc.size_bytes,
            status=doc.status,
            processing_capabilities=doc.processing_capabilities,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
            last_job_id=None if doc.last_job_id is None else str(doc.last_job_id),
        )


class FileList(ApiModel):
    """GET /api/v1/files 응답 — 한 페이지의 파일 목록과 전체 개수."""

    items: list[FileDetail]
    total: int
    page: int
    size: int


class FileDeleteResult(ApiModel):
    """DELETE /api/v1/files/{file_id} 응답."""

    file_id: str
    message: str
