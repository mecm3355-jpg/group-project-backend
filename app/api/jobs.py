from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_job_service
from app.schemas.common import ApiResponse, ok
from app.schemas.job_api import JobDetail
from app.services.job_service import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=ApiResponse[JobDetail])
async def get_job(
    service: Annotated[JobService, Depends(get_job_service)], job_id: str
) -> ApiResponse[JobDetail]:
    """작업 상태·진행률·현재 단계·오류를 조회한다. 프런트엔드가 주기적으로 호출한다.

    파라미터:
        service: 작업 서비스
        job_id: 작업 ID
    """
    # 없는 ID·잘못된 형식은 서비스가 AppError로 던지고, 여기서는 표 필드를 응답 형식으로 옮긴다
    return ok(JobDetail.from_document(await service.get(job_id)))
