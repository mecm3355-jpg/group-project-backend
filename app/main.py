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
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """앱 시작 시 DB 클라이언트를 만들고 인덱스를 확인하며, 종료 시 닫는다.

        파라미터:
            app: 수명 주기를 관리할 FastAPI 앱
        """
        mongo = Mongo(resolved.mongodb_uri, resolved.mongodb_db_name)
        set_mongo(mongo)
        try:
            await ensure_indexes(mongo.db)
            yield
        finally:
            set_mongo(None)
            await mongo.close()

    app = FastAPI(title=resolved.app_name, lifespan=lifespan)
    app.state.settings = resolved
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(files.router, prefix="/api/v1")
    app.include_router(jobs.router, prefix="/api/v1")
    app.include_router(dashboard.router, prefix="/api/v1")
    app.include_router(complaints.router, prefix="/api/v1")
    return app
