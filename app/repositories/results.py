from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.repositories.base import BaseRepository
from app.schemas.result import ResultDocument, ResultType


class ResultRepository(BaseRepository[ResultDocument]):
    """processing_results 컬렉션 저장소. 파일·결과 종류별로 버전을 1부터 매긴다."""

    COLLECTION = "processing_results"
    INDEXES = [
        IndexModel(
            [("field", ASCENDING), ("resultType", ASCENDING), ("version", DESCENDING)],
            name="field_resultType_version",
            unique=True,
        ),
        IndexModel([("jobId", ASCENDING)], name="jobId"),
        IndexModel([("createAt", DESCENDING)], name="createAt_desc"),
    ]

    def __init__(self, db: AsyncDatabase[Document]) -> None:
        """processing_results 저장소를 만든다.

        파라미터:
            db: 사용할 MongoDB 데이터베이스
        """
        super().__init__(db, ResultDocument)

    async def get_latest(self, file_id: ObjectId, result_type: ResultType) -> ResultDocument | None:
        """한 파일의 결과 종류별 최신 버전을 돌려준다. 없으면 None.

        파라미터:
            file_id: 대상 파일 _id
            result_type: analysis, cleaning, report 중 하나
        """
        found = await self._find(
            {"field": file_id, "resultType": result_type}, [("version", DESCENDING)], limit=1
        )
        return found[0] if found else None

    async def next_version(self, file_id: ObjectId, result_type: ResultType) -> int:
        """새 결과에 붙일 버전 번호(최신 버전 + 1, 처음이면 1)를 돌려준다.

        파라미터:
            file_id: 대상 파일 _id
            result_type: analysis, cleaning, report 중 하나
        """
        latest = await self.get_latest(file_id, result_type)
        return 1 if latest is None else latest.version + 1

    async def list_by_file(
        self, file_id: ObjectId, result_type: ResultType | None = None
    ) -> list[ResultDocument]:
        """한 파일의 결과 목록을 종류·최신 버전 순으로 돌려준다. 버전 이력 조회에 쓴다.

        파라미터:
            file_id: 대상 파일 _id
            result_type: 이 종류만. None이면 전체
        """
        query: dict[str, object] = {"field": file_id}
        if result_type is not None:
            query["resultType"] = result_type
        return await self._find(query, [("resultType", ASCENDING), ("version", DESCENDING)])

    async def delete_by_file(self, file_id: ObjectId) -> int:
        """한 파일의 결과를 모두 지우고 지운 개수를 돌려준다.

        파라미터:
            file_id: 대상 파일 _id
        """
        result = await self.collection.delete_many({"field": file_id})
        return result.deleted_count
