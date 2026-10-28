from collections.abc import Sequence

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel, UpdateOne
from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.repositories.base import BaseRepository
from app.schemas.complaint import AssignStatus, ComplaintDocument
from app.schemas.mongo import utc_now


class ComplaintRepository(BaseRepository[ComplaintDocument]):
    """complaints 컬렉션 저장소(PRD 7.4)."""

    COLLECTION = "complaints"
    INDEXES = [
        # PRD 7.4 인덱스 목록 그대로. 목록 API 필터(파일·월·카테고리·배정 상태·지역)와
        # 이슈별 조회가 모두 인덱스를 타도록 필드마다 단일 인덱스를 둔다
        IndexModel([("fileId", ASCENDING)], name="fileId"),
        IndexModel([("jobId", ASCENDING)], name="jobId"),
        IndexModel([("period", ASCENDING)], name="period"),
        IndexModel([("categoryId", ASCENDING)], name="categoryId"),
        IndexModel([("assignStatus", ASCENDING)], name="assignStatus"),
        IndexModel([("region", ASCENDING)], name="region"),
        IndexModel([("issueId", ASCENDING)], name="issueId"),
        # GeoJSON Point 위치 조회용. 좌표가 없는 민원(location=null)은 인덱스에서 빠진다
        IndexModel([("location", "2dsphere")], name="location_2dsphere"),
    ]

    def __init__(self, db: AsyncDatabase[Document]) -> None:
        """complaints 저장소를 만든다.

        파라미터:
            db: 사용할 MongoDB 데이터베이스
        """
        super().__init__(db, ComplaintDocument)

    async def retire_file(self, file_id: ObjectId) -> int:
        """한 파일의 기존 민원을 deleted로 바꾼다(같은 파일을 다시 분석할 때). 바꾼 수를 돌려준다.

        파라미터:
            file_id: 파일 _id
        """
        result = await self.collection.update_many(
            # 지우지 않고 deleted로 바꾼다: 끝난 검토 이력이 옛 민원을 가리켜도 깨지지 않게
            {"fileId": file_id, "status": "active"},
            {"$set": {"status": "deleted", "updatedAt": utc_now()}},
        )
        return result.modified_count

    async def unassigned_pool(self) -> list[ComplaintDocument]:
        """미배정 풀: 아직 카테고리가 없는 활성 민원 전체(접수 순). 다음 발견에 다시 들어간다.

        파라미터: 없음
        """
        return await self._find(
            # 이전 파일의 미배정 민원까지 모아야 여러 달에 걸쳐 생긴 새 유형을 찾는다.
            # _id를 둘째 정렬 키로 넣어 같은 월 안 순서도 고정한다(군집 결과 재현)
            {"status": "active", "assignStatus": "unassigned"},
            [("period", ASCENDING), ("_id", ASCENDING)],
        )

    async def list_filtered(
        self,
        *,
        category_id: str | None = None,
        period: str | None = None,
        region: str | None = None,
        assign_status: AssignStatus | None = None,
        file_id: ObjectId | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[ComplaintDocument], int]:
        """활성 민원 목록과 전체 건수(필터: 카테고리·기간·지역·배정 상태·파일).

        파라미터:
            category_id: 카테고리 ID
            period: 처리 월(YYYY-MM)
            region: 행정동
            assign_status: auto, ambiguous, unassigned, confirmed 중 하나
            file_id: 원본 파일 _id
            skip: 건너뛸 개수
            limit: 최대 개수
        """
        # 재분석으로 deleted가 된 옛 민원은 목록에서 항상 뺀다
        query: dict[str, object] = {"status": "active"}
        for key, value in (
            ("categoryId", category_id),
            ("period", period),
            ("region", region),
            ("assignStatus", assign_status),
            ("fileId", file_id),
        ):
            if value is not None:
                query[key] = value
        total = await self.collection.count_documents(query)
        # 최근 월부터. _id로 순서를 고정해 페이지를 넘길 때 항목이 겹치거나 빠지지 않게 한다
        items = await self._find(query, [("period", DESCENDING), ("_id", ASCENDING)], skip, limit)
        return items, total

    async def get_many(self, ids: Sequence[ObjectId]) -> list[ComplaintDocument]:
        """_id 목록의 민원(순서는 보장하지 않는다).

        파라미터:
            ids: 민원 _id 목록
        """
        # 빈 $in 조회로 DB를 왕복하지 않도록 바로 돌려준다
        if not ids:
            return []
        return await self._find({"_id": {"$in": list(ids)}}, [])

    async def by_candidate(self, candidate_id: ObjectId) -> list[ComplaintDocument]:
        """한 후보에 묶인 활성 민원.

        파라미터:
            candidate_id: category_candidates._id
        """
        return await self._find({"candidateId": candidate_id, "status": "active"}, [])

    async def clear_candidates(self, candidate_ids: Sequence[ObjectId]) -> None:
        """지운 후보를 가리키던 민원의 candidateId를 비운다.

        파라미터:
            candidate_ids: 지운 후보 _id 목록
        """
        if candidate_ids:
            await self.collection.update_many(
                {"candidateId": {"$in": list(candidate_ids)}}, {"$set": {"candidateId": None}}
            )

    async def update_fields(self, updates: Sequence[tuple[ObjectId, dict[str, object]]]) -> None:
        """민원마다 다른 값을 한 번에 고친다(필드명은 DB 이름). updatedAt도 함께 바꾼다.

        파라미터:
            updates: (민원 _id, {필드: 값}) 목록
        """
        if not updates:
            return
        now = utc_now()
        # 민원마다 값이 달라 update_many를 못 쓰므로 UpdateOne 묶음을 한 번에 보낸다
        await self.collection.bulk_write(
            [
                UpdateOne({"_id": oid}, {"$set": {**fields, "updatedAt": now}})
                for oid, fields in updates
            ]
        )

    async def delete_by_file(self, file_id: ObjectId) -> list[ObjectId]:
        """한 파일의 민원을 모두 지우고 지운 _id를 돌려준다(파일 삭제 때).

        파라미터:
            file_id: 파일 _id
        """
        # deleted 상태까지 모두 지운다. 지운 _id는 서비스가 그 민원의 검토를 지우는 데 쓴다
        ids = [doc.id for doc in await self._find({"fileId": file_id}, [])]
        if ids:
            await self.collection.delete_many({"_id": {"$in": ids}})
        return ids
