from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.repositories.base import BaseRepository
from app.schemas.complaint import ReviewDocument


class ReviewRepository(BaseRepository[ReviewDocument]):
    """reviews 컬렉션 저장소(PRD 7.3)."""

    COLLECTION = "reviews"
    INDEXES = [
        # 검토 화면의 대기/완료 조회(status), 오래된 순 정렬(createdAt), 작업별 조회(jobId)용
        IndexModel([("status", ASCENDING)], name="status"),
        IndexModel([("createdAt", DESCENDING)], name="createdAt_desc"),
        IndexModel([("jobId", ASCENDING)], name="jobId"),
    ]

    def __init__(self, db: AsyncDatabase[Document]) -> None:
        """reviews 저장소를 만든다.

        파라미터:
            db: 사용할 MongoDB 데이터베이스
        """
        super().__init__(db, ReviewDocument)

    async def list_page(
        self, status: Literal["pending", "done"] | None, skip: int, limit: int
    ) -> tuple[list[ReviewDocument], int]:
        """검토 목록(오래된 것부터)과 전체 건수.

        파라미터:
            status: pending 또는 done. None이면 전체
            skip: 건너뛸 개수
            limit: 최대 개수
        """
        query: dict[str, object] = {} if status is None else {"status": status}
        # 페이지 계산용 전체 건수는 skip·limit 없이 같은 조건으로 센다
        total = await self.collection.count_documents(query)
        # 오래 기다린 검토부터 처리하도록 오래된 순으로 준다
        return await self._find(query, [("createdAt", ASCENDING)], skip, limit), total

    async def complete(
        self, review_id: ObjectId, category_id: str | None, reviewed_by: str, reviewed_at: datetime
    ) -> bool:
        """대기 중인 검토를 끝낸다. 이미 끝난 검토면 False.

        파라미터:
            review_id: 검토 _id
            category_id: 검토자가 확정한 카테고리(없으면 None)
            reviewed_by: 검토자
            reviewed_at: 검토 일시
        """
        result = await self.collection.update_one(
            # 끝난 검토를 다시 덮어쓰지 않도록 대기 중인 것만 바꾼다
            {"_id": review_id, "status": "pending"},
            {
                "$set": {
                    "status": "done",
                    "reviewedCategoryId": category_id,
                    "reviewedBy": reviewed_by,
                    "reviewedAt": reviewed_at,
                }
            },
        )
        return result.modified_count == 1

    async def delete_by_complaints(self, complaint_ids: Sequence[ObjectId]) -> None:
        """민원들의 검토를 지운다(파일 삭제 때).

        파라미터:
            complaint_ids: 민원 _id 목록
        """
        if complaint_ids:
            await self.collection.delete_many({"complaintId": {"$in": list(complaint_ids)}})

    async def cancel_pending_for(self, complaint_ids: Sequence[ObjectId]) -> None:
        """다시 분석해 사라진 민원의 대기 검토를 지운다.

        파라미터:
            complaint_ids: 민원 _id 목록
        """
        if complaint_ids:
            await self.collection.delete_many(
                # 끝난 검토는 사람이 내린 판단 이력이라 남기고, 대기 건만 지운다
                {"complaintId": {"$in": list(complaint_ids)}, "status": "pending"}
            )
