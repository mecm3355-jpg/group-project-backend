from typing import Annotated

from fastapi import APIRouter, Depends
from pymongo.errors import PyMongoError

from app.core.database import Mongo, get_mongo
from app.core.errors import AppError
from app.schemas.common import ApiResponse, ok
from app.schemas.health import HealthStatus

router = APIRouter(tags=["health"])


@router.get("/health", response_model=ApiResponse[HealthStatus])
async def health(mongo: Annotated[Mongo, Depends(get_mongo)]) -> ApiResponse[HealthStatus]:
    """서버와 DB 연결 상태를 확인한다. DB에 연결할 수 없으면 503을 돌려준다.

    파라미터:
        mongo: 의존성으로 주입되는 Mongo 인스턴스
    """
    try:
        # 서버가 떠 있어도 DB가 끊기면 쓸 수 없으므로 실제로 ping을 보내 확인한다
        await mongo.ping()
    except PyMongoError as exc:
        # 503: 서버 자체 오류가 아니라 의존 서비스 일시 장애. 로드밸런서가 재시도하거나 빼도록 한다
        raise AppError("DB_UNAVAILABLE", "데이터베이스에 연결할 수 없습니다.", 503) from exc
    return ok(HealthStatus(status="ok", database="ok"))
