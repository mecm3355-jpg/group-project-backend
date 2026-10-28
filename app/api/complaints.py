"""[명세서 외 추가 제안, PRD 7.5] 민원 목록·카테고리 체계·후보 승인·검토 API.

명세서에 반영되기 전까지 제안 상태다(사용자 승인, 2026-10-26). 병합·분할(M5)과 통계·인사이트(M4)는
아직 없다.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_complaint_service
from app.schemas.common import ApiResponse, ok
from app.schemas.complaint import AssignStatus, CandidateDecision
from app.schemas.complaint_api import (
    CandidateDecisionRequest,
    CandidateDecisionResult,
    CandidateItem,
    CandidateList,
    CategoryItem,
    ComplaintItem,
    ComplaintList,
    ReviewDecisionRequest,
    ReviewItem,
    ReviewList,
    Taxonomy,
)
from app.services.complaint_service import ComplaintService

router = APIRouter(prefix="/complaints", tags=["complaints"])

Service = Annotated[ComplaintService, Depends(get_complaint_service)]


@router.get("", response_model=ApiResponse[ComplaintList])
async def list_complaints(
    service: Service,
    # 쿼리 이름은 응답 필드와 같은 camelCase(categoryId 등), 변수는 파이썬 관례대로 snake_case.
    # period는 complaints.period와 같은 YYYY-MM 형식만 받아, 틀리면 조회 전에 422로 막는다
    # limit 상한 200: 민원은 파일 하나에 수천 건일 수 있어 한 번에 내려주는 양을 제한한다
    category_id: Annotated[str | None, Query(alias="categoryId")] = None,
    period: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
    region: str | None = None,
    assign_status: Annotated[AssignStatus | None, Query(alias="assignStatus")] = None,
    file_id: Annotated[str | None, Query(alias="fileId")] = None,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ApiResponse[ComplaintList]:
    """민원 목록(카테고리·기간·지역·배정 상태·파일 필터).

    파라미터:
        service: 민원 분석 서비스
        category_id: 카테고리 ID
        period: 처리 월(YYYY-MM)
        region: 행정동
        assign_status: auto, ambiguous, unassigned, confirmed
        file_id: 원본 파일 ID
        skip: 건너뛸 개수
        limit: 최대 개수(1~200)
    """
    # 필터는 모두 선택이라 키워드 인자로 넘겨 순서 실수를 막는다
    items, total = await service.list_complaints(
        category_id=category_id,
        period=period,
        region=region,
        assign_status=assign_status,
        file_id=file_id,
        skip=skip,
        limit=limit,
    )
    return ok(ComplaintList(total=total, items=[ComplaintItem.from_document(c) for c in items]))


@router.get("/categories", response_model=ApiResponse[Taxonomy])
async def get_taxonomy(
    service: Service, version: Annotated[int | None, Query(ge=1)] = None
) -> ApiResponse[Taxonomy]:
    """현재(또는 지정 버전) 카테고리 체계.

    파라미터:
        service: 민원 분석 서비스
        version: 체계 버전(생략하면 최신)
    """
    # 버전을 생략하면 서비스가 최신 버전을 찾으므로, 실제 조회한 버전을 응답에 담는다
    resolved, categories = await service.taxonomy(version)
    return ok(Taxonomy(version=resolved, items=[CategoryItem.from_document(c) for c in categories]))


@router.get("/categories/candidates", response_model=ApiResponse[CandidateList])
async def list_candidates(
    service: Service,
    # 기본은 결정 전 후보: 승인 화면에서 처리할 것만 보여 주기 위함. None이면 전체
    decision: CandidateDecision | None = "pending",
    period: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
) -> ApiResponse[CandidateList]:
    """신규 카테고리 후보(기본: 결정 전 후보)와 대표 민원.

    파라미터:
        service: 민원 분석 서비스
        decision: pending, approved, held, merged
        period: 처리 월(YYYY-MM)
    """
    # 후보마다 대표 민원(exemplarIds)의 마스킹 본문을 함께 읽어 승인 전에 실제 민원을 보게 한다
    found = await service.candidates(decision, period)
    return ok(CandidateList(items=[CandidateItem.from_document(c, ex) for c, ex in found]))


@router.post(
    "/categories/candidates/{candidate_id}/decision",
    response_model=ApiResponse[CandidateDecisionResult],
)
async def decide_candidate(
    service: Service, candidate_id: str, request: CandidateDecisionRequest
) -> ApiResponse[CandidateDecisionResult]:
    """후보를 승인·보류·기존 카테고리에 합친다. 승인하면 체계 버전이 올라간다.

    파라미터:
        service: 민원 분석 서비스
        candidate_id: 후보 ID
        request: 결정 내용
    """
    # 승인 권한·인증은 아직 미정(PRD 12장)이라 별도 권한 확인 없이 결정을 받는다
    return ok(await service.decide(candidate_id, request))


@router.get("/reviews", response_model=ApiResponse[ReviewList])
async def list_reviews(
    service: Service,
    # reviews.status 값 두 가지. 기본은 처리할 대기 건만, None이면 전체
    status: Literal["pending", "done"] | None = "pending",
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ApiResponse[ReviewList]:
    """검토 대기 목록(기본: 대기 중)과 대상 민원.

    파라미터:
        service: 민원 분석 서비스
        status: pending 또는 done
        skip: 건너뛸 개수
        limit: 최대 개수
    """
    found, total = await service.reviews(status, skip, limit)
    return ok(
        ReviewList(
            total=total,
            items=[
                # 대상 민원이 지워졌을 수 있어 없으면 None으로 둔다
                ReviewItem.from_document(r, ComplaintItem.from_document(c) if c else None)
                for r, c in found
            ],
        )
    )


@router.post("/reviews/{review_id}", response_model=ApiResponse[ReviewItem])
async def save_review(
    service: Service, review_id: str, request: ReviewDecisionRequest
) -> ApiResponse[ReviewItem]:
    """검토 결과를 저장한다(카테고리 확정 또는 현재 배정 확인).

    파라미터:
        service: 민원 분석 서비스
        review_id: 검토 ID
        request: 확정 카테고리와 검토자
    """
    # 저장 응답은 검토 결과만 돌려주고 대상 민원은 다시 읽지 않는다(목록 API에서 본다)
    return ok(ReviewItem.from_document(await service.review(review_id, request), None))
