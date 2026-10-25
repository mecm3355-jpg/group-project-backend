from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def app() -> FastAPI:
    """테스트용 앱. 실제 DB에 붙지 않도록 수명 주기를 실행하지 않는다.

    파라미터: 없음
    """
    return create_app(Settings(mongodb_uri="mongodb://localhost:27017", mongodb_db_name="test"))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """처리되지 않은 예외도 응답으로 받아 보는 테스트 클라이언트.

    파라미터:
        app: 테스트용 앱 픽스처
    """
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()
