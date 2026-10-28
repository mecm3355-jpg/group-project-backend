from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

from app.core.database import Mongo, get_mongo


class FakeMongo(Mongo):
    """ping 결과만 흉내 내는 Mongo 대역."""

    def __init__(self, healthy: bool) -> None:
        """대역을 만든다. 실제 클라이언트는 만들지 않는다.

        파라미터:
            healthy: True면 ping 성공, False면 연결 실패 예외
        """
        self._healthy = healthy

    async def ping(self) -> None:
        """설정된 상태에 따라 성공하거나 연결 실패 예외를 던진다.

        파라미터: 없음
        """
        if not self._healthy:
            raise ServerSelectionTimeoutError("no server")


def test_health_ok(app: FastAPI, client: TestClient) -> None:
    app.dependency_overrides[get_mongo] = lambda: FakeMongo(healthy=True)

    res = client.get("/health")

    assert res.status_code == 200
    assert res.json() == {
        "success": True,
        "data": {"status": "ok", "database": "ok"},
        "error": None,
    }


def test_health_db_unavailable(app: FastAPI, client: TestClient) -> None:
    app.dependency_overrides[get_mongo] = lambda: FakeMongo(healthy=False)

    res = client.get("/health")

    assert res.status_code == 503
    assert res.json()["error"]["code"] == "DB_UNAVAILABLE"


def test_health_db_not_ready(client: TestClient) -> None:
    # 수명 주기를 실행하지 않았으므로 Mongo가 등록되지 않은 상태
    res = client.get("/health")

    assert res.status_code == 503
    assert res.json()["error"]["code"] == "DB_NOT_READY"
