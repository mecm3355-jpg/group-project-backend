"""민원 분석 API 요청·응답 자료형.

POST /api/v1/files/{file_id}/analysis는 명세서 경로이고, 본문 형식은 명세서에 없어 제안 형식을
쓴다(사용자 승인, 2026-10-26). /api/v1/complaints/* 는 PRD 7.5 "명세서 외 추가 제안"이다.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue

from app.schemas.common import ApiModel
from app.schemas.complaint import (
    AssignStatus,
    CandidateDecision,
    CandidateDocument,
    CategoryDocument,
    ComplaintAttributes,
    ComplaintDocument,
    ReviewDocument,
    ReviewReason,
)
from app.schemas.dataset import FieldConstants, FieldMapping
from app.schemas.job import JobStatus
from app.schemas.result import QualityResult, ResultDocument

AttributeMethod = Literal["rule", "llm"]


class ComplaintAnalysisRequest(ApiModel):
    """POST /files/{file_id}/analysis 요청.

    - 표 파일(CSV·XLSX): mapping.content(또는 title)가 필요하다. 통합(merge) API와 같은 매핑 형식
    - 문서·스캔 파일(PDF·DOCX·이미지): split_rule(config/split_rules.toml의 규칙 이름)로 나눈다.
      없으면 파일 하나를 민원 한 건으로 본다(PRD 7.6 3단계)
    - TXT: 빈 줄로 나눈다
    """

    type: Literal["complaint"] = "complaint"
    mapping: FieldMapping = Field(default_factory=FieldMapping)
    constants: FieldConstants = Field(default_factory=FieldConstants)
    split_rule: str | None = Field(default=None, max_length=50)
    # 속성 추출 방식. llm은 마스킹본을 fast 배포로 보낸다(기본, 사용자 결정 2026-10-26)
    attribute_method: AttributeMethod = "llm"


class AnalysisStartResult(ApiModel):
    """분석 작업 등록 응답."""

    job_id: str
    file_id: str
    status: JobStatus
    message: str


class AnalysisResult(ApiModel):
    """GET /files/{file_id}/analysis 응답: 최신 분석 결과(resultType=analysis)."""

    file_id: str
    job_id: str
    version: int
    quality: QualityResult
    analysis: dict[str, JsonValue]
    ai: dict[str, JsonValue]
    create_at: datetime

    @classmethod
    def from_document(cls, doc: ResultDocument) -> "AnalysisResult":
        """결과 문서를 응답으로 바꾼다.

        파라미터:
            doc: processing_results 문서
        """
        return cls(
            file_id=str(doc.field),
            job_id=str(doc.job_id),
            version=doc.version,
            quality=doc.quality,
            analysis=doc.analysis,
            ai=doc.ai,
            create_at=doc.create_at,
        )


class ComplaintItem(ApiModel):
    """민원 목록의 한 건."""

    complaint_id: str
    file_id: str
    period: str
    received_at: datetime | None
    region: str | None
    content_masked: str
    content_type: str
    source_category: str | None
    category_id: str | None
    group: str | None
    assign_status: AssignStatus
    similarity: float | None
    attributes: ComplaintAttributes
    needs_review: bool
    review_reason: ReviewReason | None
    extraction_confidence: float | None
    source_locator: dict[str, int]

    @classmethod
    def from_document(cls, doc: ComplaintDocument) -> "ComplaintItem":
        """민원 문서를 응답으로 바꾼다.

        파라미터:
            doc: complaints 문서
        """
        return cls(
            complaint_id=str(doc.id),
            file_id=str(doc.file_id),
            period=doc.period,
            received_at=doc.received_at,
            region=doc.region,
            content_masked=doc.content_masked,
            content_type=doc.content_type,
            source_category=doc.source_category,
            category_id=doc.category_id,
            group=doc.group,
            assign_status=doc.assign_status,
            similarity=doc.similarity,
            attributes=doc.attributes,
            needs_review=doc.needs_review,
            review_reason=doc.review_reason,
            extraction_confidence=doc.extraction_confidence,
            source_locator=doc.source_locator,
        )


class ComplaintList(ApiModel):
    """GET /complaints 응답."""

    total: int
    items: list[ComplaintItem]


class CategoryItem(ApiModel):
    """카테고리 한 개(중심 벡터는 내보내지 않는다)."""

    category_id: str
    version: int
    name: str
    group: str
    definition: str
    keywords: list[str]
    exemplar_ids: list[str]
    status: str
    created_in_version: int
    approved_by: str
    approved_at: datetime

    @classmethod
    def from_document(cls, doc: CategoryDocument) -> "CategoryItem":
        """카테고리 문서를 응답으로 바꾼다.

        파라미터:
            doc: categories 문서
        """
        return cls(
            category_id=doc.category_id,
            version=doc.version,
            name=doc.name,
            group=doc.group,
            definition=doc.definition,
            keywords=doc.keywords,
            exemplar_ids=[str(i) for i in doc.exemplar_ids],
            status=doc.status,
            created_in_version=doc.created_in_version,
            approved_by=doc.approved_by,
            approved_at=doc.approved_at,
        )


class Taxonomy(ApiModel):
    """GET /complaints/categories 응답."""

    version: int
    items: list[CategoryItem]


class CandidateItem(ApiModel):
    """신규 카테고리 후보 한 개와 대표 민원(마스킹본)."""

    candidate_id: str
    job_id: str
    period: str
    size: int
    keywords: list[str]
    exemplars: list[str]
    suggested_name: str
    suggested_definition: str | None
    suggested_group: str | None
    decision: CandidateDecision
    category_id: str | None
    merged_into_category_id: str | None
    decided_by: str | None
    decided_at: datetime | None

    @classmethod
    def from_document(cls, doc: CandidateDocument, exemplars: list[str]) -> "CandidateItem":
        """후보 문서를 응답으로 바꾼다.

        파라미터:
            doc: category_candidates 문서
            exemplars: 대표 민원 본문(마스킹본)
        """
        return cls(
            candidate_id=str(doc.id),
            job_id=str(doc.job_id),
            period=doc.period,
            size=doc.size,
            keywords=doc.keywords,
            exemplars=exemplars,
            suggested_name=doc.suggested_name,
            suggested_definition=doc.suggested_definition,
            suggested_group=doc.suggested_group,
            decision=doc.decision,
            category_id=doc.category_id,
            merged_into_category_id=doc.merged_into_category_id,
            decided_by=doc.decided_by,
            decided_at=doc.decided_at,
        )


class CandidateList(ApiModel):
    """GET /complaints/categories/candidates 응답."""

    items: list[CandidateItem]


class CandidateDecisionRequest(ApiModel):
    """POST /complaints/categories/candidates/{id}/decision 요청.

    승인 권한(PRD 12장)은 미정이라 결정자는 요청 값으로만 받는다.
    """

    decision: Literal["approved", "held", "merged"]
    decided_by: str = Field(min_length=1, max_length=100)
    # 승인: 이름·상위 그룹(필수)·정의. 비우면 제안값을 쓴다
    name: str | None = Field(default=None, max_length=100)
    group: str | None = Field(default=None, max_length=50)
    definition: str | None = Field(default=None, max_length=500)
    # 합치기: 대상 기존 카테고리
    merged_into_category_id: str | None = Field(default=None, max_length=20)


class CandidateDecisionResult(ApiModel):
    """후보 결정 응답."""

    candidate_id: str
    decision: CandidateDecision
    category_id: str | None
    taxonomy_version: int
    # 소속 민원 수, 승인 뒤 미배정 풀에서 새로 배정된 민원 수(소급 배정)
    member_count: int
    retro_assigned: int


class ReviewItem(ApiModel):
    """검토 대기 한 건과 대상 민원."""

    review_id: str
    reason: ReviewReason
    status: str
    suggested_category_id: str | None
    reviewed_category_id: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    complaint: ComplaintItem | None

    @classmethod
    def from_document(cls, doc: ReviewDocument, complaint: ComplaintItem | None) -> "ReviewItem":
        """검토 문서를 응답으로 바꾼다.

        파라미터:
            doc: reviews 문서
            complaint: 대상 민원(지워졌으면 None)
        """
        return cls(
            review_id=str(doc.id),
            reason=doc.reason,
            status=doc.status,
            suggested_category_id=doc.suggested_category_id,
            reviewed_category_id=doc.reviewed_category_id,
            reviewed_by=doc.reviewed_by,
            reviewed_at=doc.reviewed_at,
            complaint=complaint,
        )


class ReviewList(ApiModel):
    """GET /complaints/reviews 응답."""

    total: int
    items: list[ReviewItem]


class ReviewDecisionRequest(ApiModel):
    """POST /complaints/reviews/{review_id} 요청. categoryId를 비우면 미배정으로 확정한다."""

    category_id: str | None = Field(default=None, max_length=20)
    reviewed_by: str = Field(min_length=1, max_length=100)
