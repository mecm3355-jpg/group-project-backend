import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.core.config import Settings
from app.core.database import Document
from app.main import create_app

# 실제 DB가 필요한 테스트는 이 환경 변수가 있을 때만 실행한다(운영 DB 주소는 넣지 않는다)
TEST_URI = os.environ.get("MONGODB_TEST_URI")
requires_db = pytest.mark.skipif(
    not TEST_URI, reason="MONGODB_TEST_URI가 없어 DB 통합 테스트를 건너뜀"
)


def make_settings(storage_root: Path, **overrides: int) -> Settings:
    """테스트용 설정을 만든다. 저장소는 임시 폴더, DB는 연결하지 않는 주소를 쓴다.

    파라미터:
        storage_root: 원본 파일 저장소로 쓸 임시 폴더
        overrides: 바꿀 숫자 설정(예: max_upload_bytes=10)
    """
    settings = Settings(
        mongodb_uri="mongodb://localhost:27017",
        mongodb_db_name="test",
        storage_root=storage_root,
    )
    return settings.model_copy(update=overrides)


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """테스트용 앱. 실제 DB에 붙지 않도록 수명 주기를 실행하지 않는다.

    파라미터:
        tmp_path: pytest 임시 폴더(원본 파일 저장소)
    """
    return create_app(make_settings(tmp_path))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """처리되지 않은 예외도 응답으로 받아 보는 테스트 클라이언트.

    파라미터:
        app: 테스트용 앱 픽스처
    """
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def db_client(tmp_path: Path) -> Iterator[TestClient]:
    """실제 테스트 DB에 붙는 클라이언트. 임시 데이터베이스를 만들고 끝나면 지운다.

    수명 주기를 실행하므로 인덱스도 만들어진다.

    파라미터:
        tmp_path: pytest 임시 폴더(원본 파일 저장소)
    """
    assert TEST_URI is not None
    db_name = f"datalens_test_{ObjectId()}"
    settings = make_settings(tmp_path)
    settings.mongodb_uri = TEST_URI
    settings.mongodb_db_name = db_name
    try:
        with TestClient(create_app(settings)) as test_client:
            yield test_client
    finally:
        cleanup: MongoClient[Document]
        with MongoClient(TEST_URI) as cleanup:
            cleanup.drop_database(db_name)
