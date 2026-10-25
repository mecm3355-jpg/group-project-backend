from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi import status as http_status

from app.api.deps import get_file_service
from app.schemas.common import ApiResponse, ok
from app.schemas.file import FileStatus
from app.schemas.file_api import FileDeleteResult, FileDetail, FileList, FileUploadResult
from app.schemas.preview_api import FilePreview
from app.schemas.process_api import ProcessStartResult, QualityReport
from app.services.file_service import FileService

router = APIRouter(prefix="/files", tags=["files"])

Service = Annotated[FileService, Depends(get_file_service)]


@router.post(
    "",
    response_model=ApiResponse[FileUploadResult],
    status_code=http_status.HTTP_201_CREATED,
)
async def upload_file(
    service: Service, file: Annotated[UploadFile, File()]
) -> ApiResponse[FileUploadResult]:
    """파일을 업로드하고 처리 작업을 만든다(multipart/form-data, 필드명 file).

    파라미터:
        service: 파일 서비스
        file: 업로드 파일
    """
    uploaded = await service.upload(file.file, file.filename)
    return ok(
        FileUploadResult(
            file_id=str(uploaded.file.id),
            original_name=uploaded.file.original_name,
            file_type=uploaded.file.extension,
            status=uploaded.file.status,
            message="파일이 등록되었습니다.",
        )
    )


@router.get("", response_model=ApiResponse[FileList])
async def list_files(
    service: Service,
    status: Annotated[FileStatus | None, Query(description="상태 필터")] = None,
    q: Annotated[str | None, Query(max_length=200, description="파일명 검색어")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApiResponse[FileList]:
    """파일 목록을 최신 업로드 순으로 조회하고 상태·파일명으로 검색한다.

    파라미터:
        service: 파일 서비스
        status: 이 상태인 파일만
        q: 원래 파일명에 들어간 글자(대소문자 무시)
        page: 페이지 번호(1부터)
        size: 페이지 크기(최대 100)
    """
    items, total = await service.search(status, q, page, size)
    return ok(
        FileList(
            items=[FileDetail.from_document(d) for d in items], total=total, page=page, size=size
        )
    )


@router.get("/{file_id}", response_model=ApiResponse[FileDetail])
async def get_file(service: Service, file_id: str) -> ApiResponse[FileDetail]:
    """파일 상세 정보를 조회한다.

    파라미터:
        service: 파일 서비스
        file_id: 파일 ID
    """
    return ok(FileDetail.from_document(await service.get(file_id)))


@router.delete("/{file_id}", response_model=ApiResponse[FileDeleteResult])
async def delete_file(service: Service, file_id: str) -> ApiResponse[FileDeleteResult]:
    """파일과 관련 작업·결과·원본 파일을 삭제한다.

    파라미터:
        service: 파일 서비스
        file_id: 파일 ID
    """
    await service.delete(file_id)
    return ok(FileDeleteResult(file_id=file_id, message="파일이 삭제되었습니다."))


@router.get("/{file_id}/preview", response_model=ApiResponse[FilePreview])
async def preview_file(service: Service, file_id: str) -> ApiResponse[FilePreview]:
    """원본 데이터 앞부분(표 20행 또는 텍스트 20줄)을 미리 본다.

    파라미터:
        service: 파일 서비스
        file_id: 파일 ID
    """
    return ok(await service.preview(file_id))


@router.post(
    "/{file_id}/process",
    response_model=ApiResponse[ProcessStartResult],
    status_code=http_status.HTTP_202_ACCEPTED,
)
async def process_file(service: Service, file_id: str) -> ApiResponse[ProcessStartResult]:
    """데이터 추출·진단 작업을 시작한다(재처리). 진행 상황은 GET /api/v1/jobs/{job_id}로 본다.

    파라미터:
        service: 파일 서비스
        file_id: 파일 ID
    """
    job = await service.start_processing(file_id)
    return ok(
        ProcessStartResult(
            job_id=str(job.id),
            file_id=file_id,
            status=job.status,
            message="추출·진단 작업이 등록되었습니다.",
        )
    )


@router.get("/{file_id}/quality", response_model=ApiResponse[QualityReport])
async def get_quality(service: Service, file_id: str) -> ApiResponse[QualityReport]:
    """가장 최근 분석의 데이터 품질 진단 결과를 조회한다.

    파라미터:
        service: 파일 서비스
        file_id: 파일 ID
    """
    return ok(QualityReport.from_document(await service.latest_analysis(file_id)))
