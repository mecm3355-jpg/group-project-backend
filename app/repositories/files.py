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
        # 목록 API의 기본 정렬(최신 업로드 순)용
        IndexModel([("createdAt", DESCENDING)], name="createdAt_desc"),
        # 상태 필터 + 최신순 정렬을 한 인덱스로 처리하도록 동등 조건(status)을 앞에 둔다
        IndexModel([("status", ASCENDING), ("createdAt", DESCENDING)], name="status_createdAt"),
        # CLAUDE.md 인덱스 규칙(jobId 등). 작업 ID로 대상 파일을 거꾸로 찾을 때를 대비한다
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
        # 문서를 모두 읽지 않고 DB에서 합계만 계산한다(대시보드 전체 용량)
        pipeline: list[Document] = [{"$group": {"_id": None, "total": {"$sum": "$sizeBytes"}}}]
        cursor = await self.collection.aggregate(pipeline)
        rows = [row async for row in cursor]
        total = rows[0]["total"] if rows else 0
        # 파일이 없으면 결과 행이 없으므로 0. 합계가 int가 아니면(이상 데이터) 0으로 둔다
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
            # 검색어를 정규식으로 해석하지 않도록 escape한다
            # ('a.csv'의 '.'이 아무 글자와 맞는 것과, 악의적 패턴으로 DB가 느려지는 것을 막음)
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
        # schema.md: updatedAt은 최종 수정 일시이므로 상태가 바뀔 때마다 함께 갱신한다
        changes: dict[str, object] = {"status": status, "updatedAt": utc_now()}
        # 작업 없이 상태만 바꾸는 경우(예: 실패 처리) 기존 lastJobId를 지우지 않도록 넘긴 때만 쓴다
        if last_job_id is not None:
            changes["lastJobId"] = last_job_id
        result = await self.collection.update_one({"_id": file_id}, {"$set": changes})
        # 값이 같아 modified가 0이어도 문서는 있으므로 matched로 판단한다
        return result.matched_count == 1
