from datetime import datetime

from pydantic import JsonValue

from app.schemas.cleaning import CleanRule
from app.schemas.common import ApiModel
from app.schemas.job import JobStatus
from app.schemas.result import ResultDocument, ResultType


class SuggestedRule(ApiModel):
    """추천 규칙 하나. rule은 그대로 /clean 요청의 rules에 넣을 수 있다."""

    rule: CleanRule
    reason: str


class CleaningSuggestions(ApiModel):
    """POST /api/v1/files/{file_id}/cleaning-suggestions 응답."""

    file_id: str
    based_on_version: int
    suggestions: list[SuggestedRule]


class CleanStartResult(ApiModel):
    """POST /api/v1/files/{file_id}/clean 응답 — 새로 만든 정제 작업."""

    job_id: str
    file_id: str
    status: JobStatus
    message: str


class VersionItem(ApiModel):
    """결과 버전 하나의 요약. 정제 결과면 cleaning(규칙·변경 내역·전후 요약)도 담는다."""

    result_type: ResultType
    version: int
    job_id: str
    created_at: datetime
    score: int | None
    issue_count: int
    row_count: JsonValue
    cleaning: dict[str, JsonValue] | None

    @classmethod
    def from_document(cls, doc: ResultDocument) -> "VersionItem":
        """결과 문서를 버전 목록 항목으로 바꾼다. 산출물 저장 위치는 내보내지 않는다.

        파라미터:
            doc: processing_results 문서
        """
        cleaning: dict[str, JsonValue] | None = None
        if doc.result_type == "cleaning":
            cleaning = {k: v for k, v in doc.cleaning.items() if k != "artifacts"}
        return cls(
            result_type=doc.result_type,
            version=doc.version,
            job_id=str(doc.job_id),
            created_at=doc.create_at,
            score=doc.quality.score,
            issue_count=len(doc.quality.issues),
            row_count=doc.analysis.get("rowCount"),
            cleaning=cleaning,
        )


class VersionList(ApiModel):
    """GET /api/v1/files/{file_id}/versions 응답 — 결과 종류별 최신 버전 순."""

    file_id: str
    items: list[VersionItem]
