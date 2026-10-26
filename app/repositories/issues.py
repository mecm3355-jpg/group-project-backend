from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.repositories.base import BaseRepository
from app.schemas.complaint import IssueDocument
from app.schemas.mongo import utc_now


class IssueRepository(BaseRepository[IssueDocument]):
    """issues 컬렉션 저장소(PRD 7.3)."""

    COLLECTION = "issues"
    INDEXES = [
        IndexModel([("categoryId", ASCENDING)], name="categoryId"),
        IndexModel([("place", ASCENDING)], name="place"),
        IndexModel([("lastReceivedAt", DESCENDING)], name="lastReceivedAt_desc"),
        IndexModel([("location", "2dsphere")], name="location_2dsphere"),
    ]

    def __init__(self, db: AsyncDatabase[Document]) -> None:
        """issues 저장소를 만든다.

        파라미터:
            db: 사용할 MongoDB 데이터베이스
        """
        super().__init__(db, IssueDocument)

    async def open_issues(self) -> list[IssueDocument]:
        """열린 이슈 전체(카테고리 순).

        파라미터: 없음
        """
        return await self._find({"status": "open"}, [("categoryId", ASCENDING)])

    async def replace(self, issue: IssueDocument) -> None:
        """이슈 하나를 통째로 고쳐 쓴다(건수·기간·최고 긴급도 갱신). updatedAt을 바꾼다.

        파라미터:
            issue: 고친 이슈
        """
        issue.updated_at = utc_now()
        await self.collection.replace_one({"_id": issue.id}, issue.to_document())
