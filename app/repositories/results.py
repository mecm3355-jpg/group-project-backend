from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.repositories.base import BaseRepository
from app.schemas.result import QualityResult, ResultDocument, ResultType


class ResultRepository(BaseRepository[ResultDocument]):
    """processing_results 컬렉션 저장소. 파일·결과 종류별로 버전을 1부터 매긴다."""

    COLLECTION = "processing_results"
    INDEXES = [
        # unique: 같은 파일·종류에 같은 버전이 두 번 생기지 않게 한다(동시에 저장해도 한쪽은 실패).
        # version 내림차순이라 최신 버전 조회(get_latest)가 인덱스 첫 항목으로 끝난다
        IndexModel(
            [("field", ASCENDING), ("resultType", ASCENDING), ("version", DESCENDING)],
            name="field_resultType_version",
            unique=True,
        ),
        # 작업에서 그 작업이 만든 결과를 찾을 때용(CLAUDE.md 인덱스 규칙)
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
        # 버전은 파일·결과 종류별로 따로 매긴다(분석 v2와 정제 v1이 함께 있을 수 있음)
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
        # 종류별로 묶어 보이고, 종류 안에서는 최신 버전이 먼저 오도록 정렬한다
        return await self._find(query, [("resultType", ASCENDING), ("version", DESCENDING)])

    async def latest_qualities(self, result_type: ResultType) -> list[QualityResult]:
        """파일마다 결과 종류별 최신 버전의 품질 진단 결과를 모아 돌려준다. 대시보드 집계에 쓴다.

        파라미터:
            result_type: analysis, cleaning, report 중 하나
        """
        pipeline: list[Document] = [
            {"$match": {"resultType": result_type}},
            # 파일별 최신 버전만 집계해야 재처리한 파일이 여러 번 세어지지 않는다.
            # 정렬 후 $first로 파일마다 가장 높은 버전 하나를 고른다
            {"$sort": {"field": ASCENDING, "version": DESCENDING}},
            {"$group": {"_id": "$field", "quality": {"$first": "$quality"}}},
        ]
        cursor = await self.collection.aggregate(pipeline)
        return [QualityResult.model_validate(row["quality"]) async for row in cursor]

    async def delete_by_file(self, file_id: ObjectId) -> int:
        """한 파일의 결과를 모두 지우고 지운 개수를 돌려준다.

        파라미터:
            file_id: 대상 파일 _id
        """
        # 산출물 파일은 서비스가 저장소에서 따로 지운다. 여기서는 DB 문서만 지운다
        result = await self.collection.delete_many({"field": file_id})
        return result.deleted_count
