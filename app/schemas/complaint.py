"""민원 전용 컬렉션 문서 모델.

PRD 5.2 categories, 7.3 category_candidates·issues·reviews, 7.4 complaints.
기준 파일(schema.md)에 없는 PRD 제안 컬렉션이다(사용자 승인, 2026-10-26).
필드는 PRD 표를 그대로 따르고, 사유 값에 ocrUncertain(OCR 신뢰도 낮음, PRD 표에 없는 값,
사용자 승인)만 더했다.
"""

from datetime import datetime
from typing import Literal

from bson import ObjectId
from pydantic import Field

from app.schemas.mongo import MongoDocument, MongoModel, utc_now

COMPLAINT_ANALYSIS_JOB_TYPE = "complaint_analysis"

CategoryStatus = Literal["active", "merged", "split"]
CandidateDecision = Literal["pending", "approved", "held", "merged"]
AssignStatus = Literal["auto", "ambiguous", "unassigned", "confirmed"]
ReviewReason = Literal["ambiguous", "urgent", "attributeUncertain", "ocrUncertain"]
ContentType = Literal["body", "title"]
IssueStatus = Literal["open", "resolved"]


class CategoryDocument(MongoDocument):
    """categories — 체계 버전별 카테고리(PRD 5.2). 승인할 때마다 버전이 올라간다."""

    category_id: str
    version: int
    name: str
    group: str
    definition: str
    keywords: list[str]
    exemplar_ids: list[ObjectId]
    # 승인 시점 소속 민원 임베딩 평균. 이후 배정된 민원으로 바꾸지 않는다
    centroid: list[float]
    embedding_model: str
    status: CategoryStatus = "active"
    created_in_version: int
    approved_by: str
    approved_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CandidateDocument(MongoDocument):
    """category_candidates — 신규 카테고리 후보와 승인 결정(PRD 7.3)."""

    job_id: ObjectId
    period: str
    size: int
    keywords: list[str]
    exemplar_ids: list[ObjectId]
    suggested_name: str
    suggested_definition: str | None = None
    suggested_group: str | None = None
    decision: CandidateDecision = "pending"
    merged_into_category_id: str | None = None
    category_id: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class GeoPoint(MongoModel):
    """GeoJSON Point. coordinates는 [경도, 위도] 순서다."""

    type: Literal["Point"] = "Point"
    coordinates: list[float]


class ComplaintAttributes(MongoModel):
    """complaints.attributes — 속성 6종(PRD 5.5). 근거가 없으면 각 값 null."""

    facility: str | None = None
    place: str | None = None
    urgency: str | None = None
    repeat: str | None = None
    request_type: str | None = None
    affected: str | None = None


class ComplaintDocument(MongoDocument):
    """complaints — 민원 단위 분석 결과(PRD 7.4). 임베딩 벡터는 산출물 파일에 둔다."""

    file_id: ObjectId
    job_id: ObjectId
    # 원문 위치. CSV·XLSX {row}, PDF·이미지 {page, block}, DOCX {paragraph, tableRow}
    source_locator: dict[str, int]
    extraction_confidence: float | None = None
    received_at: datetime | None = None
    period: str
    region: str | None = None
    location: GeoPoint | None = None
    content_masked: str
    content_type: ContentType
    source_category: str | None = None
    category_id: str | None = None
    group: str | None = None
    taxonomy_version: int
    assign_status: AssignStatus
    similarity: float | None = None
    runner_up_category_id: str | None = None
    candidate_id: ObjectId | None = None
    attributes: ComplaintAttributes = Field(default_factory=ComplaintAttributes)
    needs_review: bool = False
    review_reason: ReviewReason | None = None
    issue_id: ObjectId | None = None
    analyzer: str
    status: Literal["active", "deleted"] = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ReviewDocument(MongoDocument):
    """reviews — 검토 대기와 검토 결과(PRD 7.3)."""

    complaint_id: ObjectId
    job_id: ObjectId
    reason: ReviewReason
    suggested_category_id: str | None = None
    reviewed_category_id: str | None = None
    status: Literal["pending", "done"] = "pending"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class IssueDocument(MongoDocument):
    """issues — 같은 장소·같은 문제의 반복 민원 묶음(PRD 7.3, 4장 7단계)."""

    category_id: str
    place: str
    location: GeoPoint | None = None
    complaint_count: int
    first_received_at: datetime
    last_received_at: datetime
    # 대표 문장(묶음 중심에 가장 가까운 마스킹본)
    summary: str
    max_urgency: str
    status: IssueStatus = "open"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
