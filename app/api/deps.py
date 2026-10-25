from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.database import Mongo, get_mongo
from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.repositories.results import ResultRepository
from app.services.cleaning_service import CleaningService
from app.services.dataset_service import DatasetService
from app.services.file_service import FileService
from app.services.job_service import JobService
from app.services.report_service import ReportService
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


def get_job_service(mongo: Annotated[Mongo, Depends(get_mongo)]) -> JobService:
    """요청마다 작업 서비스를 만든다.

    파라미터:
        mongo: Mongo 인스턴스
    """
    return JobService(JobRepository(mongo.db))


def get_cleaning_service(
    mongo: Annotated[Mongo, Depends(get_mongo)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> CleaningService:
    """요청마다 정제 서비스를 만든다.

    파라미터:
        mongo: Mongo 인스턴스
        settings: 앱 설정
    """
    return CleaningService(
        FileRepository(mongo.db),
        JobRepository(mongo.db),
        ResultRepository(mongo.db),
        LocalStorage(settings.storage_root),
    )


def get_report_service(
    mongo: Annotated[Mongo, Depends(get_mongo)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> ReportService:
    """요청마다 리포트 서비스를 만든다.

    파라미터:
        mongo: Mongo 인스턴스
        settings: 앱 설정
    """
    return ReportService(
        FileRepository(mongo.db),
        JobRepository(mongo.db),
        ResultRepository(mongo.db),
        settings,
    )


def get_dataset_service(
    mongo: Annotated[Mongo, Depends(get_mongo)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> DatasetService:
    """요청마다 통합 데이터셋 서비스를 만든다.

    파라미터:
        mongo: Mongo 인스턴스
        settings: 앱 설정
    """
    return DatasetService(
        FileRepository(mongo.db),
        JobRepository(mongo.db),
        ResultRepository(mongo.db),
        LocalStorage(settings.storage_root),
        settings,
    )
