from datetime import datetime

from app.schemas.common import ApiModel
from app.schemas.file import FileCategory, FileDocument, FileStatus, ProcessingCapabilities


class FileUploadResult(ApiModel):
    """POST /api/v1/files 응답 — 명세서 "파일 등록 응답 예시"의 필드 그대로.

    fileType은 files 스키마에 없는 이름이라 확장자로 채운다(PRD 7.7 확인 대기).
    """

    file_id: str
    original_name: str
    # fileType은 명세서 예시 이름. files에 같은 필드가 없어 확장자(csv 등)를 넣는다
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
            # storage(서버 내부 경로)는 일부러 빼고 옮긴다. 경로가 노출되면 저장 구조가 드러난다
            original_name=doc.original_name,
            extension=doc.extension,
            mime_type=doc.mime_type,
            category=doc.category,
            size_bytes=doc.size_bytes,
            status=doc.status,
            processing_capabilities=doc.processing_capabilities,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
            # 프런트엔드가 이 ID로 바로 GET /jobs/{job_id}를 불러 진행 상황을 본다
            last_job_id=None if doc.last_job_id is None else str(doc.last_job_id),
        )


class FileList(ApiModel):
    """GET /api/v1/files 응답 — 한 페이지의 파일 목록과 전체 개수."""

    items: list[FileDetail]
    # 필터를 적용한 전체 개수(이 페이지 개수가 아님). page·size는 요청 값을 되돌려 준다
    total: int
    page: int
    size: int


class FileDeleteResult(ApiModel):
    """DELETE /api/v1/files/{file_id} 응답."""

    file_id: str
    message: str
