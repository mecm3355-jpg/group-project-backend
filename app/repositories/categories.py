from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.repositories.base import BaseRepository
from app.schemas.complaint import CategoryDocument


class CategoryRepository(BaseRepository[CategoryDocument]):
    """categories 컬렉션 저장소. 체계 버전마다 그 버전의 카테고리 전체를 저장한다(PRD 5.2)."""

    COLLECTION = "categories"
    INDEXES = [
        IndexModel(
            [("categoryId", ASCENDING), ("version", ASCENDING)],
            name="categoryId_version",
            unique=True,
        ),
        IndexModel([("status", ASCENDING)], name="status"),
        IndexModel([("group", ASCENDING)], name="group"),
    ]

    def __init__(self, db: AsyncDatabase[Document]) -> None:
        """categories 저장소를 만든다.

        파라미터:
            db: 사용할 MongoDB 데이터베이스
        """
        super().__init__(db, CategoryDocument)

    async def latest_version(self) -> int:
        """가장 최근 체계 버전. 카테고리가 하나도 없으면 0.

        파라미터: 없음
        """
        found = await self._find({}, [("version", DESCENDING)], limit=1)
        return found[0].version if found else 0

    async def list_version(
        self, version: int, *, active_only: bool = True
    ) -> list[CategoryDocument]:
        """한 체계 버전의 카테고리 목록(categoryId 순).

        파라미터:
            version: 체계 버전
            active_only: True면 active 상태만
        """
        query: dict[str, object] = {"version": version}
        if active_only:
            query["status"] = "active"
        return await self._find(query, [("categoryId", ASCENDING)])

    async def get_in_version(self, category_id: str, version: int) -> CategoryDocument | None:
        """한 버전의 카테고리 하나. 없으면 None.

        파라미터:
            category_id: 카테고리 ID(예: CAT-0012)
            version: 체계 버전
        """
        found = await self._find({"categoryId": category_id, "version": version}, [], limit=1)
        return found[0] if found else None

    async def max_category_number(self) -> int:
        """지금까지 쓴 카테고리 번호의 최댓값(CAT-0012 → 12). 없으면 0.

        파라미터: 없음
        """
        found = await self._find({}, [("categoryId", DESCENDING)], limit=1)
        return int(found[0].category_id.split("-")[-1]) if found else 0
