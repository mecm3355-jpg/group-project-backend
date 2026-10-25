"""정제 규칙. 명세서의 규칙 형식(type, column / columns, strategy)을 그대로 따른다.

AI가 만든 자유 형식 코드 대신 미리 정의된 규칙만 받는다(명세서 처리 정제 API 안내).
명세서 예시의 3종만 지원하고, PRD 7.5의 추가 규칙(swap_columns 등)은 확정 전이라 받지 않는다.
"""

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

FillStrategy = Literal["mean", "median", "mode", "constant"]


class RuleModel(BaseModel):
    """정제 규칙의 기반. 값이 없는(None) 필드는 내보내지 않는다(명세서 예시 형식)."""

    @model_serializer(mode="wrap")
    def _drop_none(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """기본 직렬화 결과에서 None 필드를 뺀다.

        파라미터:
            handler: pydantic 기본 직렬화 함수
        """
        data: dict[str, object] = handler(self)
        return {k: v for k, v in data.items() if v is not None}


class TrimWhitespaceRule(RuleModel):
    """열 값의 앞뒤 공백을 지운다."""

    type: Literal["trim_whitespace"]
    column: str


class RemoveDuplicatesRule(RuleModel):
    """중복 행을 지우고 처음 나온 행만 남긴다. columns가 없으면 모든 열이 같을 때 중복으로 본다."""

    type: Literal["remove_duplicates"]
    columns: list[str] | None = Field(default=None, min_length=1)


class FillMissingRule(RuleModel):
    """결측값을 채운다. mean·median은 숫자 열만, constant는 value가 필요하다."""

    type: Literal["fill_missing"]
    column: str
    strategy: FillStrategy
    value: str | int | float | None = None

    @model_validator(mode="after")
    def _value_for_constant(self) -> Self:
        """constant 전략이면 value가 있어야 한다.

        파라미터: 없음(검증할 규칙 자신)
        """
        if self.strategy == "constant" and self.value is None:
            raise ValueError("strategy가 constant이면 value가 필요합니다.")
        return self


CleanRule = Annotated[
    TrimWhitespaceRule | RemoveDuplicatesRule | FillMissingRule, Field(discriminator="type")
]


class CleanRequest(BaseModel):
    """POST /api/v1/files/{file_id}/clean 요청 본문 — 승인한 정제 규칙 목록(적용 순서대로)."""

    rules: list[CleanRule] = Field(min_length=1, max_length=50)
