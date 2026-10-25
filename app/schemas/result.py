from datetime import datetime
from typing import Literal

from bson import ObjectId
from pydantic import Field, JsonValue

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
    """processing_results.quality — 데이터 품질 진단 결과. score는 예시에 없을 수 있다."""

    score: int | None = None
    issues: list[QualityIssue] = Field(default_factory=list)


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
