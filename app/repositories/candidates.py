from datetime import datetime

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.repositories.base import BaseRepository
from app.schemas.complaint import CandidateDecision, CandidateDocument


class CandidateRepository(BaseRepository[CandidateDocument]):
    """category_candidates 컬렉션 저장소(PRD 7.3)."""

    COLLECTION = "category_candidates"
    INDEXES = [
        IndexModel([("jobId", ASCENDING)], name="jobId"),
        IndexModel([("period", ASCENDING)], name="period"),
        IndexModel([("decision", ASCENDING)], name="decision"),
    ]

    def __init__(self, db: AsyncDatabase[Document]) -> None:
        """category_candidates 저장소를 만든다.

        파라미터:
            db: 사용할 MongoDB 데이터베이스
        """
        super().__init__(db, CandidateDocument)

    async def list_by(
        self, decision: CandidateDecision | None = None, period: str | None = None
    ) -> list[CandidateDocument]:
        """후보 목록(큰 후보부터). 조건을 주면 그 결정·처리 월만.

        파라미터:
            decision: pending, approved, held, merged 중 하나
            period: 처리 월(YYYY-MM)
        """
        query: dict[str, object] = {}
        if decision is not None:
            query["decision"] = decision
        if period is not None:
            query["period"] = period
        return await self._find(query, [("size", DESCENDING), ("createdAt", DESCENDING)])

    async def delete_pending(self) -> list[ObjectId]:
        """결정 전(pending) 후보를 지우고 지운 _id를 돌려준다.

        새 분석 작업이 미배정 풀 전체를 다시 군집화하므로, 결정 전 후보는 새 후보로 바뀐다.
        결정된 후보(approved·held·merged)는 이력으로 남긴다.

        파라미터: 없음
        """
        pending = [doc.id for doc in await self._find({"decision": "pending"}, [])]
        if pending:
            await self.collection.delete_many({"_id": {"$in": pending}})
        return pending

    async def decide(
        self,
        candidate_id: ObjectId,
        decision: CandidateDecision,
        decided_by: str,
        decided_at: datetime,
        *,
        category_id: str | None = None,
        merged_into: str | None = None,
    ) -> bool:
        """결정 전 후보에 결정을 기록한다. 이미 결정된 후보면 바꾸지 않고 False.

        파라미터:
            candidate_id: 후보 _id
            decision: approved, held, merged 중 하나
            decided_by: 결정자
            decided_at: 결정 일시
            category_id: 승인으로 생긴 categoryId
            merged_into: 합친 기존 categoryId
        """
        result = await self.collection.update_one(
            {"_id": candidate_id, "decision": "pending"},
            {
                "$set": {
                    "decision": decision,
                    "decidedBy": decided_by,
                    "decidedAt": decided_at,
                    "categoryId": category_id,
                    "mergedIntoCategoryId": merged_into,
                }
            },
        )
        return result.modified_count == 1
