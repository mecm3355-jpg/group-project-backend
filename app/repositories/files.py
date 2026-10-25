import re

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.repositories.base import BaseRepository
from app.schemas.file import FileDocument, FileStatus
from app.schemas.mongo import utc_now


class FileRepository(BaseRepository[FileDocument]):
    """files 컬렉션 저장소."""

    COLLECTION = "files"
    INDEXES = [
        IndexModel([("createdAt", DESCENDING)], name="createdAt_desc"),
        IndexModel([("status", ASCENDING), ("createdAt", DESCENDING)], name="status_createdAt"),
        IndexModel([("lastJobId", ASCENDING)], name="lastJobId"),
    ]

    def __init__(self, db: AsyncDatabase[Document]) -> None:
        """files 저장소를 만든다.

        파라미터:
            db: 사용할 MongoDB 데이터베이스
        """
        super().__init__(db, FileDocument)

    async def total_size_bytes(self) -> int:
        """모든 파일의 크기 합(바이트)을 돌려준다.

        파라미터: 없음
        """
        pipeline: list[Document] = [{"$group": {"_id": None, "total": {"$sum": "$sizeBytes"}}}]
        cursor = await self.collection.aggregate(pipeline)
        rows = [row async for row in cursor]
        total = rows[0]["total"] if rows else 0
        return total if isinstance(total, int) else 0

    @staticmethod
    def _query(status: FileStatus | None, name: str | None) -> dict[str, object]:
        """search·count에 공통으로 쓰는 조건을 만든다.

        파라미터:
            status: 이 상태인 파일만. None이면 전체
            name: 원래 파일명에 이 글자가 들어간 파일만(대소문자 무시). None이면 전체
        """
        query: dict[str, object] = {}
        if status is not None:
            query["status"] = status
        if name:
            query["originalName"] = {"$regex": re.escape(name), "$options": "i"}
        return query

    async def search(
        self,
        status: FileStatus | None = None,
        name: str | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[FileDocument]:
        """파일 목록을 최신 업로드 순으로 돌려준다.

        파라미터:
            status: 이 상태인 파일만. None이면 전체
            name: 원래 파일명 검색어(부분 일치, 대소문자 무시)
            skip: 건너뛸 개수
            limit: 최대 개수
        """
        return await self._find(
            self._query(status, name), [("createdAt", DESCENDING)], skip=skip, limit=limit
        )

    async def count(self, status: FileStatus | None = None, name: str | None = None) -> int:
        """search와 같은 조건의 전체 파일 수를 센다.

        파라미터:
            status: 이 상태인 파일만. None이면 전체
            name: 원래 파일명 검색어
        """
        return await self.collection.count_documents(self._query(status, name))

    async def update_status(
        self, file_id: ObjectId, status: FileStatus, last_job_id: ObjectId | None = None
    ) -> bool:
        """파일 상태를 바꾸고 updatedAt을 갱신한다. 문서가 있었으면 True.

        파라미터:
            file_id: 바꿀 파일 _id
            status: 새 상태
            last_job_id: 함께 기록할 마지막 작업 ID. None이면 기존 값 유지
        """
        changes: dict[str, object] = {"status": status, "updatedAt": utc_now()}
        if last_job_id is not None:
            changes["lastJobId"] = last_job_id
        result = await self.collection.update_one({"_id": file_id}, {"$set": changes})
        return result.matched_count == 1
