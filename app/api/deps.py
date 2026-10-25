from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.database import Mongo, get_mongo
from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.repositories.results import ResultRepository
from app.services.file_service import FileService
from app.services.storage import LocalStorage


def get_app_settings(request: Request) -> Settings:
    """create_app에 넘긴 설정을 돌려준다.

    파라미터:
        request: 현재 요청(앱 상태에서 설정을 꺼낸다)
    """
    settings = request.app.state.settings
    assert isinstance(settings, Settings)
    return settings


def get_file_service(
    mongo: Annotated[Mongo, Depends(get_mongo)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> FileService:
    """요청마다 파일 서비스를 만든다.

    파라미터:
        mongo: Mongo 인스턴스
        settings: 앱 설정
    """
    return FileService(
        FileRepository(mongo.db),
        JobRepository(mongo.db),
        ResultRepository(mongo.db),
        LocalStorage(settings.storage_root),
        settings,
    )
