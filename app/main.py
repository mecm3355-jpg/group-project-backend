from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.api.error_handlers import register_error_handlers
from app.core.config import Settings, get_settings
from app.core.database import Mongo, set_mongo


def create_app(settings: Settings | None = None) -> FastAPI:
    """FastAPI 앱을 만든다. 실행: uvicorn app.main:create_app --factory

    파라미터:
        settings: 사용할 설정. 생략하면 환경 변수에서 읽는다(테스트에서 직접 넘김)
    """
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """앱 시작 시 DB 클라이언트를 만들고 종료 시 닫는다.

        파라미터:
            app: 수명 주기를 관리할 FastAPI 앱
        """
        mongo = Mongo(resolved.mongodb_uri, resolved.mongodb_db_name)
        set_mongo(mongo)
        try:
            yield
        finally:
            set_mongo(None)
            await mongo.close()

    app = FastAPI(title=resolved.app_name, lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(health.router)
    return app
