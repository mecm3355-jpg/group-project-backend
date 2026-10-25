from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_dashboard_service
from app.schemas.common import ApiResponse, ok
from app.schemas.dashboard_api import DashboardSummary
from app.services.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=ApiResponse[DashboardSummary])
async def get_summary(
    service: Annotated[DashboardService, Depends(get_dashboard_service)],
) -> ApiResponse[DashboardSummary]:
    """전체 통계(파일 수·용량, 작업 상태, 품질 진단 요약)를 조회한다.

    파라미터:
        service: 전체 통계 서비스
    """
    return ok(await service.summary())
