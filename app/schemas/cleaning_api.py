from datetime import datetime

from pydantic import JsonValue

from app.schemas.cleaning import CleanRule
from app.schemas.common import ApiModel
from app.schemas.job import JobStatus
from app.schemas.result import ResultDocument, ResultType


class SuggestedRule(ApiModel):
    """추천 규칙 하나. rule은 그대로 /clean 요청의 rules에 넣을 수 있다.

    expectedChanges는 예상 변경 건수(바뀌는 칸 수 또는 지워지는 행 수)다.
    """

    rule: CleanRule
    reason: str
    expected_changes: int


class CleaningSuggestions(ApiModel):
    """POST /api/v1/files/{file_id}/cleaning-suggestions 응답."""

    file_id: str
    # 추천 근거가 된 분석 결과 버전
    based_on_version: int
    suggestions: list[SuggestedRule]


class CleanStartResult(ApiModel):
    """POST /api/v1/files/{file_id}/clean 응답 — 새로 만든 정제 작업."""

    job_id: str
    file_id: str
    status: JobStatus
    message: str


class ReportStartResult(ApiModel):
    """POST /api/v1/files/{file_id}/report 응답 — 새로 만든 리포트 작업."""

    job_id: str
    file_id: str
    status: JobStatus
    message: str


class VersionItem(ApiModel):
    """결과 버전 하나의 요약.

    정제 결과면 cleaning(규칙·변경 내역·전후 요약),
    리포트면 ai(리포트 본문·근거 버전·사용 내역)도 담는다.
    """

    result_type: ResultType
    version: int
    job_id: str
    created_at: datetime
    score: int | None
    issue_count: int
    # analysis.rowCount를 그대로 옮긴다. 표 형식이 아니면 없어서 null일 수 있다
    row_count: JsonValue
    cleaning: dict[str, JsonValue] | None
    ai: dict[str, JsonValue] | None

    @classmethod
    def from_document(cls, doc: ResultDocument) -> "VersionItem":
        """결과 문서를 버전 목록 항목으로 바꾼다. 산출물 저장 위치는 내보내지 않는다.

        파라미터:
            doc: processing_results 문서
        """
        cleaning: dict[str, JsonValue] | None = None
        if doc.result_type == "cleaning":
            # artifacts에는 서버 저장 경로가 들어 있어 응답에서 뺀다. 내려받기는 export API로 한다
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
            # 빈 객체({})로 저장된 비해당 결과는 null로 내보내 프런트엔드가 쉽게 구분하게 한다
            ai=doc.ai or None,
        )


class VersionList(ApiModel):
    """GET /api/v1/files/{file_id}/versions 응답 — 결과 종류별 최신 버전 순."""

    file_id: str
    items: list[VersionItem]
