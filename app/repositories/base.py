from collections.abc import Mapping, Sequence
from typing import ClassVar, Generic, TypeVar

from bson import ObjectId
from pymongo import IndexModel
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.schemas.mongo import MongoDocument

M = TypeVar("M", bound=MongoDocument)

SortSpec = Sequence[tuple[str, int]]


class BaseRepository(Generic[M]):
    """컬렉션 하나를 다루는 저장소의 공통 기능(저장·단건 조회·목록 조회·삭제).

    하위 클래스는 COLLECTION(컬렉션 이름)과 INDEXES(인덱스 정의)를 정한다.
    """

    COLLECTION: ClassVar[str]
    INDEXES: ClassVar[list[IndexModel]] = []

    def __init__(self, db: AsyncDatabase[Document], model: type[M]) -> None:
        """저장소를 만든다.

        파라미터:
            db: 사용할 MongoDB 데이터베이스
            model: 이 컬렉션 문서를 표현하는 모델 클래스
        """
        self.collection: AsyncCollection[Document] = db[self.COLLECTION]
        self._model = model

    async def ensure_indexes(self) -> None:
        """INDEXES에 정의한 인덱스를 만든다. 이미 있으면 그대로 둔다.

        파라미터: 없음
        """
        if self.INDEXES:
            await self.collection.create_indexes(self.INDEXES)

    async def insert(self, doc: M) -> M:
        """문서를 저장하고 그대로 돌려준다. _id는 모델에서 미리 만들어져 있다.

        파라미터:
            doc: 저장할 문서 모델
        """
        await self.collection.insert_one(doc.to_document())
        return doc

    async def insert_many(self, docs: Sequence[M]) -> None:
        """문서 여러 개를 한 번에 저장한다. 비어 있으면 아무것도 하지 않는다.

        파라미터:
            docs: 저장할 문서 모델 목록
        """
        if docs:
            await self.collection.insert_many([doc.to_document() for doc in docs])

    async def get(self, doc_id: ObjectId) -> M | None:
        """_id로 문서 하나를 찾는다. 없으면 None.

        파라미터:
            doc_id: 찾을 문서의 _id
        """
        raw = await self.collection.find_one({"_id": doc_id})
        return None if raw is None else self._model.model_validate(raw)

    async def _find(
        self,
        query: Mapping[str, object],
        sort: SortSpec,
        skip: int = 0,
        limit: int = 0,
    ) -> list[M]:
        """조건에 맞는 문서 목록을 정렬해 돌려준다. 하위 클래스의 목록 메서드가 쓴다.

        파라미터:
            query: MongoDB 조회 조건
            sort: (필드명, 1 오름차순 / -1 내림차순) 목록
            skip: 건너뛸 개수
            limit: 최대 개수. 0이면 제한 없음
        """
        cursor = self.collection.find(dict(query))
        if sort:  # 정렬 조건이 없으면 저장 순서 그대로(pymongo는 빈 정렬을 받지 않는다)
            cursor = cursor.sort(list(sort))
        cursor = cursor.skip(skip).limit(limit)
        return [self._model.model_validate(raw) async for raw in cursor]

    async def count_by(self, field: str) -> dict[str, int]:
        """필드 값별 문서 수를 센다. 값이 없는 문서는 세지 않는다. 대시보드 집계에 쓴다.

        파라미터:
            field: 묶을 필드의 MongoDB 이름(예: status)
        """
        pipeline: list[Document] = [
            {"$match": {field: {"$type": "string"}}},
            {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
        ]
        cursor = await self.collection.aggregate(pipeline)
        counts: dict[str, int] = {}
        async for row in cursor:
            count = row["count"]
            if isinstance(count, int):
                counts[str(row["_id"])] = count
        return counts

    async def delete(self, doc_id: ObjectId) -> bool:
        """_id로 문서 하나를 지운다. 지웠으면 True.

        파라미터:
            doc_id: 지울 문서의 _id
        """
        result = await self.collection.delete_one({"_id": doc_id})
        return result.deleted_count == 1
