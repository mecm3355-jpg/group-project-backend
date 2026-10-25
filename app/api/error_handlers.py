import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError
from app.schemas.common import fail

logger = logging.getLogger(__name__)


def _response(status_code: int, code: str, message: str) -> JSONResponse:
    """공통 실패 응답을 JSON으로 만든다.

    파라미터:
        status_code: HTTP 상태 코드
        code: 오류 코드
        message: 사용자에게 보여줄 메시지
    """
    return JSONResponse(status_code=status_code, content=fail(code, message).model_dump())


async def handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    """AppError를 공통 실패 응답으로 바꾼다.

    파라미터:
        request: 실패한 요청
        exc: 발생한 AppError
    """
    assert isinstance(exc, AppError)
    return _response(exc.status_code, exc.code, exc.message)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """요청 값 검증 실패를 422 공통 실패 응답으로 바꾼다. 첫 번째 오류 위치만 알려준다.

    파라미터:
        request: 실패한 요청
        exc: FastAPI의 RequestValidationError
    """
    assert isinstance(exc, RequestValidationError)
    message = "요청 값이 올바르지 않습니다."
    errors = exc.errors()
    if errors:
        location = ".".join(str(part) for part in errors[0].get("loc", ()))
        message = f"{message} ({location}: {errors[0].get('msg', '')})"
    return _response(422, "VALIDATION_ERROR", message)


async def handle_http_error(request: Request, exc: Exception) -> JSONResponse:
    """없는 경로(404), 허용되지 않은 메서드(405) 등 HTTP 오류를 공통 실패 응답으로 바꾼다.

    파라미터:
        request: 실패한 요청
        exc: Starlette의 HTTPException
    """
    assert isinstance(exc, StarletteHTTPException)
    codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
    return _response(
        exc.status_code, codes.get(exc.status_code, f"HTTP_{exc.status_code}"), str(exc.detail)
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """처리하지 못한 예외를 로그로 남기고 500 공통 실패 응답을 돌려준다. 내부 정보는 숨긴다.

    파라미터:
        request: 실패한 요청
        exc: 발생한 예외
    """
    logger.exception("처리되지 않은 오류: %s %s", request.method, request.url.path)
    return _response(500, "INTERNAL_ERROR", "서버 내부 오류가 발생했습니다.")


def register_error_handlers(app: FastAPI) -> None:
    """앱에 예외 처리기를 등록한다.

    파라미터:
        app: 처리기를 등록할 FastAPI 앱
    """
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
