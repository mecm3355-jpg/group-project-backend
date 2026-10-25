"""여러 원본 파일을 공통 필드로 합친 통합 데이터셋(PRD 3장 "소스별 컬럼 매핑").

기관마다 열 이름이 달라(신고상담내용·민원제목·민원내용·제목 등) 공통 필드로 맞춘 뒤 하나의
CSV 파일로 합친다. 통합 파일은 일반 파일과 같아서 진단·정제·AI 리포트 API를 그대로 쓴다.
"""

from typing import Literal

from pydantic import Field

from app.schemas.common import ApiModel
from app.schemas.job import JobStatus

CanonicalField = Literal[
    "source_id",
    "title",
    "content",
    "received_at",
    "region",
    "address",
    "latitude",
    "longitude",
    "agency",
    "source_category",
    "result",
]

# 통합 CSV의 열 이름(순서대로). 앞의 두 열은 원문 위치(PRD 3장: 원본으로 되돌아갈 수 있게)
SOURCE_COLUMNS = ("출처파일ID", "원본행")
FIELD_LABELS: dict[CanonicalField, str] = {
    "source_id": "원본ID",
    "title": "제목",
    "content": "본문",
    "received_at": "접수일시",
    "region": "지역",
    "address": "주소",
    "latitude": "위도",
    "longitude": "경도",
    "agency": "처리기관",
    "source_category": "기존분류",
    "result": "처리결과",
}
# 열 대신 고정값으로 채울 수 있는 필드(예: 아산 파일 전체의 지역 = 아산시)
ConstantField = Literal["region", "agency", "source_category"]


class FieldMapping(ApiModel):
    """공통 필드 → 원본 열 이름. 해당 열이 없으면 비워 둔다(None)."""

    source_id: str | None = None
    title: str | None = None
    content: str | None = None
    received_at: str | None = None
    region: str | None = None
    address: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    agency: str | None = None
    source_category: str | None = None
    result: str | None = None

    def mapped(self) -> dict[CanonicalField, str]:
        """비어 있지 않은 매핑만 돌려준다.

        파라미터: 없음
        """
        result: dict[CanonicalField, str] = {}
        for name in FIELD_LABELS:
            column = getattr(self, name)
            if isinstance(column, str) and column:
                result[name] = column
        return result


class FieldConstants(ApiModel):
    """파일 전체에 같은 값을 넣을 필드. 같은 필드에 열 매핑이 있으면 열 값이 우선이다."""

    region: str | None = Field(default=None, max_length=100)
    agency: str | None = Field(default=None, max_length=100)
    source_category: str | None = Field(default=None, max_length=100)


class MergeSource(ApiModel):
    """통합에 넣을 원본 파일 하나와 그 매핑."""

    file_id: str
    mapping: FieldMapping
    constants: FieldConstants = Field(default_factory=FieldConstants)
    # 매핑 제안 메모(예: 좌표 열을 바꿔 매핑한 이유). 출처 이력에 남겨 리포트가 참고한다
    notes: list[str] = Field(default_factory=list, max_length=20)


class MergeRequest(ApiModel):
    """POST /api/v1/files/merge 요청 — 합칠 파일과 파일별 매핑(확인을 거친 것)."""

    name: str | None = Field(default=None, max_length=100, description="통합 파일 이름")
    sources: list[MergeSource] = Field(min_length=1, max_length=50)


# ---- LLM 응답 형식(구조화 출력은 모든 필드가 필수여야 하므로 기본값 없이 따로 둔다) ----


class AiFieldMapping(ApiModel):
    """LLM이 제안한 매핑. 해당 열이 없으면 null."""

    source_id: str | None
    title: str | None
    content: str | None
    received_at: str | None
    region: str | None
    address: str | None
    latitude: str | None
    longitude: str | None
    agency: str | None
    source_category: str | None
    result: str | None


class AiFieldConstants(ApiModel):
    """LLM이 제안한 고정값. 근거가 없으면 null."""

    region: str | None
    agency: str | None
    source_category: str | None


class AiMappingProposal(ApiModel):
    """LLM 매핑 제안 전체."""

    mapping: AiFieldMapping
    constants: AiFieldConstants
    notes: list[str]


# ---- API 응답 ----


class MappingSuggestion(ApiModel):
    """POST /api/v1/files/{file_id}/mapping-suggestion 응답.

    확인·수정한 뒤 /files/merge에 넣는다.
    """

    file_id: str
    method: Literal["ai", "rule"]
    columns: list[str]
    mapping: FieldMapping
    constants: FieldConstants
    notes: list[str]


class MergeStartResult(ApiModel):
    """POST /api/v1/files/merge 응답 — 만들어질 통합 파일과 그 작업."""

    file_id: str
    job_id: str
    status: JobStatus
    message: str
