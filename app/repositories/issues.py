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
        # 좌표가 없는 민원은 장소 표현(place)이 같을 때 같은 이슈로 본다
        IndexModel([("place", ASCENDING)], name="place"),
        # 최근에 다시 접수된 이슈를 먼저 보는 조회용
        IndexModel([("lastReceivedAt", DESCENDING)], name="lastReceivedAt_desc"),
        # GeoJSON 좌표 위치 조회용 구면 인덱스(지도·반경 조회 대비).
        # 이슈 묶기의 거리 비교는 issue_grouping이 직접 계산한다
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
        # 닫힌 이슈에는 새 민원을 붙이지 않으므로 열린 이슈만 비교 대상으로 읽는다
        return await self._find({"status": "open"}, [("categoryId", ASCENDING)])

    async def replace(self, issue: IssueDocument) -> None:
        """이슈 하나를 통째로 고쳐 쓴다(건수·기간·최고 긴급도 갱신). updatedAt을 바꾼다.

        파라미터:
            issue: 고친 이슈
        """
        issue.updated_at = utc_now()
        # 여러 필드(건수·기간·긴급도·민원 목록)가 함께 바뀌므로 $set 대신 문서 전체를 교체한다
        await self.collection.replace_one({"_id": issue.id}, issue.to_document())
