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

# processing_jobs.type 값. 일반 분석(analyze)과 처리기가 달라 별도 이름을 쓴다
COMPLAINT_ANALYSIS_JOB_TYPE = "complaint_analysis"

# merged·split은 분기 정리(PRD 5.3) 결과로 남는 이력 상태. 배정에는 active만 쓴다
CategoryStatus = Literal["active", "merged", "split"]
# held: 보류(다음 달에 다시 판단), merged: 기존 카테고리에 합침
CandidateDecision = Literal["pending", "approved", "held", "merged"]
# auto: 기준 이상 자동 배정, ambiguous: 1·2위 차이가 작아 검토 대기,
# unassigned: 기준 미달(미배정 풀), confirmed: 사람이 확정
AssignStatus = Literal["auto", "ambiguous", "unassigned", "confirmed"]
ReviewReason = Literal["ambiguous", "urgent", "attributeUncertain", "ocrUncertain"]
# 본문이 없고 제목만 있는 원본도 분석하도록 어느 쪽을 썼는지 남긴다
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
    # centroid를 만든 임베딩 모델. 모델이 바뀌면 중심을 비교할 수 없으므로 함께 기록한다
    embedding_model: str
    status: CategoryStatus = "active"
    # 이 카테고리가 처음 생긴 체계 버전(version은 이 문서가 속한 버전)
    created_in_version: int
    approved_by: str
    approved_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CandidateDocument(MongoDocument):
    """category_candidates — 신규 카테고리 후보와 승인 결정(PRD 7.3)."""

    job_id: ObjectId
    period: str
    # 후보 군집에 묶인 민원 수
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
    # 위도·경도 순으로 착각하기 쉬워 주의: MongoDB 2dsphere는 [경도, 위도]만 받는다
    coordinates: list[float]


class ComplaintAttributes(MongoModel):
    """complaints.attributes — 속성 6종(PRD 5.5). 근거가 없으면 각 값 null."""

    facility: str | None = None
    place: str | None = None
    urgency: str | None = None
    repeat: str | None = None
    # DB에는 requestType으로 저장된다(camelCase 별칭)
    request_type: str | None = None
    affected: str | None = None


class ComplaintDocument(MongoDocument):
    """complaints — 민원 단위 분석 결과(PRD 7.4). 임베딩 벡터는 산출물 파일에 둔다."""

    file_id: ObjectId
    job_id: ObjectId
    # 원문 위치. CSV·XLSX {row}, PDF·이미지 {page, block}, DOCX {paragraph, tableRow}
    source_locator: dict[str, int]
    # OCR·분리 신뢰도(0~1). 텍스트를 그대로 추출했으면 None
    extraction_confidence: float | None = None
    received_at: datetime | None = None
    period: str
    region: str | None = None
    location: GeoPoint | None = None
    # 개인정보를 가린 본문만 저장한다(원문은 원본 파일에만 있다)
    content_masked: str
    content_type: ContentType
    # 기관이 붙여 온 기존 분류값(PRD 5.4). 배정 결과와 비교하는 참고값
    source_category: str | None = None
    category_id: str | None = None
    group: str | None = None
    # 배정에 쓴 체계 버전. 체계가 바뀐 뒤에도 어떤 기준으로 배정했는지 알 수 있다
    taxonomy_version: int
    assign_status: AssignStatus
    # 배정 카테고리 중심과의 코사인 유사도, 2위 카테고리(애매 판정 근거)
    similarity: float | None = None
    runner_up_category_id: str | None = None
    # 미배정 민원이 묶인 신규 후보(category_candidates._id)
    candidate_id: ObjectId | None = None
    attributes: ComplaintAttributes = Field(default_factory=ComplaintAttributes)
    needs_review: bool = False
    review_reason: ReviewReason | None = None
    issue_id: ObjectId | None = None
    # 분석기·임베딩 모델 버전('임베딩 모델 / 속성 추출기' 형식). 결과 비교·재현용
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
    # 묶음 안 민원 수. repeat_min_count 이상이면 반복 이슈(PRD 8.2)
    complaint_count: int
    first_received_at: datetime
    last_received_at: datetime
    # 대표 문장(묶음 중심에 가장 가까운 마스킹본)
    summary: str
    # 묶음 안 최고 긴급도(긴급 > 높음 > 보통 > 낮음)
    max_urgency: str
    status: IssueStatus = "open"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
