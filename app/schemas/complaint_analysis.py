"""민원 분석기 공통 인터페이스(PRD 6장)의 입출력 자료형.

discover(records) -> list[CandidateCategory]
assign(records, taxonomy) -> list[Assignment]
extract(records) -> list[Attributes]

민원 전용 컬렉션(categories·complaints)은 확정 전이라 DB에 저장하지 않는다.
M2 시뮬레이션은 이 자료형을 메모리에서만 쓰고 결과는 산출물 파일로 남긴다.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 속성 값은 PRD 5.5 표의 한국어 값을 그대로 쓴다(LLM 출력과 화면 표시가 같은 값)
Urgency = Literal["긴급", "높음", "보통", "낮음"]
Repeat = Literal["최초", "재신고"]
RequestType = Literal["수리·조치", "단속", "신설·개선", "질의", "불만"]
Affected = Literal["어린이", "노인", "장애인", "일반"]
# 분석기 내부 상태. DB에 저장할 때는 complaints.assignStatus(auto 등)로 바꾼다
AssignmentStatus = Literal["assigned", "ambiguous", "unassigned"]
# 후보 기준: size(최소 규모 이상) 또는 consecutive(2개월 연속 등장)
CandidateReason = Literal["size", "consecutive"]


class ComplaintRecord(BaseModel):
    """분석 대상 민원 1건. text는 마스킹본이어야 한다(PRD 4장 2단계)."""

    # 여러 단계(배정·발견·속성)가 같은 레코드를 나눠 쓰므로 중간에 바뀌지 않게 고정한다
    model_config = ConfigDict(frozen=True)

    record_id: str
    text: str
    # 접수월(YYYY-MM). 월별 시뮬레이션과 "2개월 연속 등장" 판정에 쓴다
    month: str | None = None


class CandidateCategory(BaseModel):
    """미배정 풀 군집 하나에서 나온 신규 카테고리 후보(PRD 4장 5단계)."""

    candidate_id: str
    # 군집에 속한 민원 record_id 전체와, 그중 중심에 가까운 대표 민원(exemplar_ids).
    # centroid는 소속 민원 임베딩 평균으로, 승인되면 카테고리 중심이 된다
    member_ids: list[str]
    keywords: list[str]
    exemplar_ids: list[str]
    centroid: list[float]
    reason: CandidateReason
    # 소속 민원과 중심의 평균 코사인 유사도
    cohesion: float
    analyzer: str
    embedding_model: str


class Category(BaseModel):
    """승인된 카테고리. 중심은 승인 시점 값으로 고정한다(PRD 5.3)."""

    category_id: str
    name: str
    centroid: list[float]
    created_in_version: int


class Assignment(BaseModel):
    """배정 결과 1건. 유사도는 상위 두 카테고리만 남긴다."""

    record_id: str
    status: AssignmentStatus
    # 1위 카테고리. status가 assigned일 때만 배정이 확정된 것이다
    category_id: str | None
    # 카테고리가 하나도 없으면(첫 실행) 유사도를 계산할 수 없어 None
    top1_similarity: float | None
    second_category_id: str | None
    top2_similarity: float | None
    analyzer: str


class Place(BaseModel):
    """장소 속성: 행정동 + 지물 표현(PRD 5.5)."""

    dong: str | None = None
    landmark: str | None = None


class Attributes(BaseModel):
    """속성 6종(PRD 5.5). 본문에 근거가 없으면 None으로 둔다(추측 금지)."""

    record_id: str
    facility: str | None = None
    place: Place = Place()
    urgency: Urgency | None = None
    repeat: Repeat | None = None
    request_type: RequestType | None = None
    affected: Affected | None = None
    analyzer: str


class LlmAttributeItem(BaseModel):
    """LLM 구조화 출력의 민원 1건 속성. 근거가 없으면 null."""

    model_config = ConfigDict(extra="forbid")

    # extra=forbid: 구조화 출력 스키마가 additionalProperties=false가 되어 모르는 키를 거부한다.
    # Field description은 JSON 스키마로 LLM에 전달되어 추출 지시문 역할을 한다
    id: str = Field(description="입력에 주어진 민원 번호 그대로")
    facility: str | None = Field(description="문제가 생긴 물건·시설(예: 가로등, 보도블록)")
    dong: str | None = Field(description="본문에 적힌 동·읍·면 이름. 없으면 null")
    landmark: str | None = Field(description="본문에 적힌 지물 표현(예: 놀이터 옆). 없으면 null")
    urgency: Urgency | None
    repeat: Repeat | None
    request_type: RequestType | None
    affected: Affected | None


class LlmAttributeBatch(BaseModel):
    """LLM 속성 추출 응답(민원 여러 건을 한 번에)."""

    model_config = ConfigDict(extra="forbid")

    items: list[LlmAttributeItem]
