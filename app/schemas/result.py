from datetime import datetime
from typing import Literal

from bson import ObjectId
from pydantic import Field, JsonValue, SerializerFunctionWrapHandler, model_serializer

from app.schemas.mongo import MongoDocument, MongoModel, utc_now

ResultType = Literal["analysis", "cleaning", "report"]
Severity = Literal["low", "medium", "high"]


class QualityIssue(MongoModel):
    """processing_results.quality.issues의 항목(설계 파일 예시 형식)."""

    type: str
    severity: Severity
    count: int
    location: str | None = None
    message: str


class QualityResult(MongoModel):
    """processing_results.quality — 데이터 품질 진단 결과. score는 예시에 없을 수 있다.

    표 형식만 metrics(완전성·유일성·유효성·일관성, 0~100)와 scoreBasis(점수 계산 기준)를 채운다.
    둘은 값이 없으면 저장하지 않는다(설계 파일 예시 형식 유지).
    """

    score: int | None = None
    issues: list[QualityIssue] = Field(default_factory=list)
    metrics: dict[str, float] | None = None
    score_basis: str | None = None

    @model_serializer(mode="wrap")
    def _drop_empty_extras(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """기본 직렬화 결과에서 값이 없는 metrics·scoreBasis를 뺀다.

        파라미터:
            handler: pydantic 기본 직렬화 함수
        """
        data: dict[str, object] = handler(self)
        # by_alias 여부에 따라 키가 score_basis 또는 scoreBasis다
        for key in ("metrics", "score_basis", "scoreBasis"):
            if key in data and data[key] is None:
                del data[key]
        return data


class ResultDocument(MongoDocument):
    """processing_results 컬렉션 문서(schema.md의 processing_results 표).

    quality·analysis·cleaning·ai는 표에서 필수지만 비고에 "필요한 경우에만 저장"이라
    키는 항상 두고, 해당 없는 작업은 빈 객체로 저장한다(PRD 7.7 확인 대기).
    analysis·cleaning·ai는 파일 유형·작업마다 구조가 달라 JSON 객체로 둔다.
    """

    field: ObjectId
    job_id: ObjectId
    result_type: ResultType
    version: int = Field(ge=1)
    quality: QualityResult = Field(default_factory=QualityResult)
    analysis: dict[str, JsonValue] = Field(default_factory=dict)
    cleaning: dict[str, JsonValue] = Field(default_factory=dict)
    ai: dict[str, JsonValue] = Field(default_factory=dict)
    create_at: datetime = Field(default_factory=utc_now)
