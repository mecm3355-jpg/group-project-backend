from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi import status as http_status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.api.deps import (
    get_cleaning_service,
    get_complaint_service,
    get_dataset_service,
    get_file_service,
    get_report_service,
)
from app.schemas.cleaning import CleanRequest
from app.schemas.cleaning_api import (
    CleaningSuggestions,
    CleanStartResult,
    ReportStartResult,
    SuggestedRule,
    VersionItem,
    VersionList,
)
from app.schemas.common import ApiResponse, ok
from app.schemas.complaint_api import AnalysisResult, AnalysisStartResult, ComplaintAnalysisRequest
from app.schemas.dataset import MappingSuggestion, MergeRequest, MergeStartResult
from app.schemas.file import FileStatus
from app.schemas.file_api import FileDeleteResult, FileDetail, FileList, FileUploadResult
from app.schemas.preview_api import FilePreview
from app.schemas.process_api import ProcessStartResult, QualityReport
from app.schemas.result import ResultType
from app.services.cleaning_service import CleaningService
from app.services.complaint_service import ComplaintService
from app.services.dataset_service import DatasetService
from app.services.export import ExportFormat
from app.services.file_service import FileService
from app.services.report_service import ReportService

router = APIRouter(prefix="/files", tags=["files"])

# 의존성 별칭: 라우터마다 Annotated[..., Depends(...)]를 반복하지 않도록 한 번만 정의한다
Service = Annotated[FileService, Depends(get_file_service)]
Cleaning = Annotated[CleaningService, Depends(get_cleaning_service)]
Reports = Annotated[ReportService, Depends(get_report_service)]
Datasets = Annotated[DatasetService, Depends(get_dataset_service)]
Complaints = Annotated[ComplaintService, Depends(get_complaint_service)]


@router.post(
    "",
    response_model=ApiResponse[FileUploadResult],
    # 파일 문서는 즉시 만들어지므로 201. 분석 자체는 함께 만든 작업이 워커에서 처리한다
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
    # 확장자·실제 형식 검증, 저장, 문서·작업 생성은 서비스가 맡는다(CLAUDE.md 업로드 규칙)
    uploaded = await service.upload(file.file, file.filename)
    return ok(
        FileUploadResult(
            # 명세서: fileId는 MongoDB _id를 문자열로 반환
            file_id=str(uploaded.file.id),
            original_name=uploaded.file.original_name,
            file_type=uploaded.file.extension,
            status=uploaded.file.status,
            # 미지원 형식은 작업 없이 등록만 되므로(job=None) 그 이유를 알린다
            message="파일이 등록되었습니다."
            if uploaded.job
            else "파일이 등록되었습니다. 이 형식은 내용 분석을 지원하지 않습니다.",
        )
    )


@router.post(
    "/merge",
    response_model=ApiResponse[MergeStartResult],
    status_code=http_status.HTTP_202_ACCEPTED,
)
async def merge_files(service: Datasets, request: MergeRequest) -> ApiResponse[MergeStartResult]:
    """[명세서 외] 여러 원본 파일을 공통 필드로 합친 통합 파일을 만든다.

    매핑은 POST /files/{file_id}/mapping-suggestion으로 제안받아 확인·수정한 것을 넣는다.
    통합 파일은 일반 파일과 같아서 진단·정제·AI 리포트 API를 그대로 쓴다.

    파라미터:
        service: 통합 데이터셋 서비스
        request: 합칠 파일과 파일별 매핑
    """
    # 통합 파일 문서를 먼저 만들고 합치기는 워커 작업으로 넘긴다. 오래 걸릴 수 있어 202로 응답
    merged, job = await service.start_merge(request)
    return ok(
        MergeStartResult(
            file_id=str(merged.id),
            job_id=str(job.id),
            status=job.status,
            message="통합 데이터셋 작업이 등록되었습니다.",
        )
    )


@router.get("", response_model=ApiResponse[FileList])
async def list_files(
    service: Service,
    status: Annotated[FileStatus | None, Query(description="상태 필터")] = None,
    q: Annotated[str | None, Query(max_length=200, description="파일명 검색어")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    # 페이지 크기 상한 100: 한 요청이 컬렉션을 통째로 읽지 않도록 막는다
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
    # total은 필터를 적용한 전체 건수라 프런트엔드가 페이지 수를 계산할 수 있다
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
    # 작업·결과·산출물·민원 데이터까지 서비스가 함께 지운다(명세서: 파일 및 관련 결과 삭제)
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
    # 명세서 권장대로 결과를 기다리지 않고 작업 ID만 바로 돌려준다(202). 결과는 작업 조회로 확인
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
    # 재처리로 분석 버전이 여러 개일 수 있어 가장 최근 버전만 보여준다
    return ok(QualityReport.from_document(await service.latest_analysis(file_id)))


@router.post("/{file_id}/cleaning-suggestions", response_model=ApiResponse[CleaningSuggestions])
async def suggest_cleaning(service: Cleaning, file_id: str) -> ApiResponse[CleaningSuggestions]:
    """최신 품질 진단을 근거로 정제 규칙을 추천한다(적용은 하지 않는다).

    파라미터:
        service: 정제 서비스
        file_id: 파일 ID
    """
    # 추천은 저장된 진단 결과만 읽어 빨리 끝나므로 작업을 만들지 않고 바로 응답한다
    analysis, suggestions = await service.suggest(file_id)
    return ok(
        CleaningSuggestions(
            file_id=file_id,
            # 근거 분석 버전을 알려, 재처리로 진단이 바뀌었는지 확인하게 한다
            based_on_version=analysis.version,
            suggestions=[
                SuggestedRule(rule=s.rule, reason=s.reason, expected_changes=s.expected_changes)
                for s in suggestions
            ],
        )
    )


@router.post(
    "/{file_id}/clean",
    response_model=ApiResponse[CleanStartResult],
    status_code=http_status.HTTP_202_ACCEPTED,
)
async def clean_file(
    service: Cleaning, file_id: str, request: CleanRequest
) -> ApiResponse[CleanStartResult]:
    """승인한 정제 규칙을 원본에 적용하는 작업을 만든다. 진행 상황은 작업 조회 API로 본다.

    파라미터:
        service: 정제 서비스
        file_id: 파일 ID
        request: 적용할 규칙 목록(trim_whitespace, remove_duplicates, fill_missing)
    """
    # 명세서: AI가 만든 코드가 아니라 미리 정의된 규칙만 받는다. 규칙 검증은 CleanRequest가 한다
    job = await service.start_cleaning(file_id, request)
    return ok(
        CleanStartResult(
            job_id=str(job.id),
            file_id=file_id,
            status=job.status,
            message="정제 작업이 등록되었습니다.",
        )
    )


@router.get("/{file_id}/versions", response_model=ApiResponse[VersionList])
async def list_versions(
    service: Cleaning,
    file_id: str,
    result_type: Annotated[
        # 파이썬 내장 이름 type을 피하려고 변수는 result_type, 쿼리 이름은 type으로 둔다
        ResultType | None, Query(alias="type", description="결과 종류 필터")
    ] = None,
) -> ApiResponse[VersionList]:
    """분석·정제 결과의 버전과 이력(정제 규칙·변경 내역·전후 요약)을 조회한다.

    파라미터:
        service: 정제 서비스
        file_id: 파일 ID
        result_type: analysis, cleaning, report 중 하나만 볼 때(쿼리 이름 type)
    """
    results = await service.versions(file_id, result_type)
    return ok(VersionList(file_id=file_id, items=[VersionItem.from_document(r) for r in results]))


@router.get("/{file_id}/export", response_class=FileResponse)
async def export_file(
    service: Cleaning,
    file_id: str,
    # 명세서 경로가 ?format=확장자라 쿼리 이름은 format(내장 함수 이름이라 변수는 fmt)
    fmt: Annotated[ExportFormat, Query(alias="format", description="csv, json, xlsx")],
    version: Annotated[int | None, Query(ge=1, description="정제 버전. 생략하면 최신")] = None,
) -> FileResponse:
    """정제 결과를 파일로 내려받는다.

    파라미터:
        service: 정제 서비스
        file_id: 파일 ID
        fmt: 내려받을 형식(쿼리 이름 format)
        version: 정제 결과 버전(생략하면 최신)
    """
    export = await service.export(file_id, fmt, version)
    # 정제 산출물(CSV)을 json·xlsx로 바꾼 임시 파일은 전송이 끝난 뒤 지운다.
    # CSV는 보관 중인 산출물을 그대로 보내므로(temporary=False) 지우면 안 된다
    cleanup = BackgroundTask(export.path.unlink, missing_ok=True) if export.temporary else None
    return FileResponse(
        export.path, media_type=export.media_type, filename=export.filename, background=cleanup
    )


@router.post(
    "/{file_id}/analysis",
    response_model=ApiResponse[AnalysisStartResult],
    status_code=http_status.HTTP_202_ACCEPTED,
)
async def start_analysis(
    service: Complaints, file_id: str, request: ComplaintAnalysisRequest
) -> ApiResponse[AnalysisStartResult]:
    """분석을 실행한다(민원 분석: 마스킹·임베딩·속성·배정·발견·이슈). 진행은 작업 조회 API로 본다.

    요청 본문 형식은 명세서에 없어 제안 형식을 쓴다(ComplaintAnalysisRequest).

    파라미터:
        service: 민원 분석 서비스
        file_id: 파일 ID
        request: 분석 요청(열 매핑·분리 규칙·속성 추출 방식)
    """
    # 민원 분석은 임베딩·군집화로 오래 걸리므로 작업으로 등록하고 202로 응답한다
    job = await service.start_analysis(file_id, request)
    return ok(
        AnalysisStartResult(
            job_id=str(job.id),
            file_id=file_id,
            status=job.status,
            message="분석 작업이 등록되었습니다.",
        )
    )


@router.get("/{file_id}/analysis", response_model=ApiResponse[AnalysisResult])
async def get_analysis(service: Service, file_id: str) -> ApiResponse[AnalysisResult]:
    """분석 결과를 조회한다(최신 분석 버전. 민원 분석이면 analysis.complaint에 요약).

    파라미터:
        service: 파일 서비스
        file_id: 파일 ID
    """
    # 조회는 작업 없이 저장된 최신 분석 결과를 읽으므로 파일 서비스를 쓴다
    return ok(AnalysisResult.from_document(await service.latest_analysis(file_id)))


@router.post(
    "/{file_id}/report",
    response_model=ApiResponse[ReportStartResult],
    status_code=http_status.HTTP_202_ACCEPTED,
)
async def create_report(service: Reports, file_id: str) -> ApiResponse[ReportStartResult]:
    """AI 분석 리포트 작업을 만든다. 결과는 GET /versions?type=report에서 본다.

    파라미터:
        service: 리포트 서비스
        file_id: 파일 ID
    """
    # LLM 호출이 오래 걸릴 수 있어 작업으로 넘긴다. 결과는 processing_results(resultType=report)
    job = await service.start_report(file_id)
    return ok(
        ReportStartResult(
            job_id=str(job.id),
            file_id=file_id,
            status=job.status,
            message="AI 분석 리포트 작업이 등록되었습니다.",
        )
    )


@router.post("/{file_id}/mapping-suggestion", response_model=ApiResponse[MappingSuggestion])
async def suggest_mapping(service: Datasets, file_id: str) -> ApiResponse[MappingSuggestion]:
    """[명세서 외] 원본 열 → 공통 필드 매핑을 제안한다(AI 또는 열 이름 규칙).

    데이터 값은 AI에 보내지 않는다. 결과를 확인·수정해 POST /files/merge에 넣는다.

    파라미터:
        service: 통합 데이터셋 서비스
        file_id: 파일 ID
    """
    # 제안만 돌려주고 저장하지 않는다. 사용자가 고친 매핑은 merge 요청에 담겨 온다
    return ok(await service.suggest_mapping(file_id))
