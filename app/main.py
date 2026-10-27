from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import complaints, dashboard, files, health, jobs
from app.api.error_handlers import register_error_handlers
from app.core.config import Settings, get_settings
from app.core.database import Mongo, set_mongo
from app.repositories.indexes import ensure_indexes


def create_app(settings: Settings | None = None) -> FastAPI:
    """FastAPI 앱을 만든다. 실행: uvicorn app.main:create_app --factory

    파라미터:
        settings: 사용할 설정. 생략하면 환경 변수에서 읽는다(테스트에서 직접 넘김)
    """
    # 테스트는 임시 DB 설정을 직접 넘기므로, 넘긴 값이 있으면 환경 변수보다 우선한다
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """앱 시작 시 DB 클라이언트를 만들고 인덱스를 확인하며, 종료 시 닫는다.

        파라미터:
            app: 수명 주기를 관리할 FastAPI 앱
        """
        # 요청 처리 전에 클라이언트를 만들어 전역에 등록해야 get_mongo 의존성이 동작한다
        mongo = Mongo(resolved.mongodb_uri, resolved.mongodb_db_name)
        set_mongo(mongo)
        try:
            # 인덱스 생성은 같은 정의면 아무 일도 하지 않으므로 시작할 때마다 호출해도 안전하다
            await ensure_indexes(mongo.db)
            yield
        finally:
            # 시작 중 실패해도 등록을 풀고 연결을 닫도록 finally에서 정리한다
            set_mongo(None)
            await mongo.close()

    app = FastAPI(title=resolved.app_name, lifespan=lifespan)
    # 라우터 의존성(get_app_settings)이 같은 설정 객체를 쓰도록 앱 상태에 보관한다
    app.state.settings = resolved
    # 모든 오류를 공통 실패 응답으로 통일(API 명세서 '성공·실패 응답 규격 통일')
    register_error_handlers(app)
    # 헬스 체크는 버전과 무관하게 /health에 두고, 업무 API만 /api/v1 아래에 둔다
    app.include_router(health.router)
    app.include_router(files.router, prefix="/api/v1")
    app.include_router(jobs.router, prefix="/api/v1")
    app.include_router(dashboard.router, prefix="/api/v1")
    app.include_router(complaints.router, prefix="/api/v1")
    return app
