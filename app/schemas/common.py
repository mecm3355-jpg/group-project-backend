from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

T = TypeVar("T")


class ApiModel(BaseModel):
    """API 응답 본문의 기반. JSON 필드는 camelCase로 내보낸다(명세서 예시 형식)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ErrorDetail(BaseModel):
    """실패 응답의 오류 정보."""

    code: str
    message: str


class ApiResponse(BaseModel, Generic[T]):
    """모든 API의 공통 응답 형식. 성공이면 data, 실패면 error를 채운다."""

    success: bool
    data: T | None = None
    error: ErrorDetail | None = None


def ok(data: T) -> ApiResponse[T]:
    """성공 응답을 만든다.

    파라미터:
        data: 응답 본문에 담을 데이터
    """
    return ApiResponse[T](success=True, data=data)


def fail(code: str, message: str) -> ApiResponse[None]:
    """실패 응답을 만든다.

    파라미터:
        code: 오류 코드
        message: 사용자에게 보여줄 메시지
    """
    return ApiResponse[None](success=False, error=ErrorDetail(code=code, message=message))
