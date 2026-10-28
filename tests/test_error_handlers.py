from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import AppError


def _add_test_routes(app: FastAPI) -> None:
    """예외 처리기 확인용 경로를 테스트 앱에만 추가한다.

    파라미터:
        app: 경로를 추가할 테스트 앱
    """

    @app.get("/_test/app-error")
    async def raise_app_error() -> None:
        """AppError를 던진다."""
        raise AppError("FILE_NOT_FOUND", "파일을 찾을 수 없습니다.", 404)

    @app.get("/_test/unexpected")
    async def raise_unexpected() -> None:
        """처리되지 않는 예외를 던진다."""
        raise RuntimeError("secret internal detail")

    @app.get("/_test/validation")
    async def needs_int(page: int) -> int:
        """정수 쿼리 파라미터를 요구한다.

        파라미터:
            page: 정수 쿼리 값
        """
        return page


def test_app_error_uses_common_format(app: FastAPI, client: TestClient) -> None:
    _add_test_routes(app)

    res = client.get("/_test/app-error")

    assert res.status_code == 404
    assert res.json() == {
        "success": False,
        "data": None,
        "error": {"code": "FILE_NOT_FOUND", "message": "파일을 찾을 수 없습니다."},
    }


def test_unexpected_error_hides_detail(app: FastAPI, client: TestClient) -> None:
    _add_test_routes(app)

    res = client.get("/_test/unexpected")

    assert res.status_code == 500
    assert res.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in res.text


def test_validation_error(app: FastAPI, client: TestClient) -> None:
    _add_test_routes(app)

    res = client.get("/_test/validation", params={"page": "abc"})

    assert res.status_code == 422
    body = res.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "page" in body["error"]["message"]


def test_unknown_route(client: TestClient) -> None:
    res = client.get("/no-such-path")

    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"
