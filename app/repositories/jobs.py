from collections.abc import Collection
from datetime import datetime

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.repositories.base import BaseRepository
from app.schemas.job import JobDocument, JobError
from app.schemas.mongo import utc_now


class JobRepository(BaseRepository[JobDocument]):
    """processing_jobs 컬렉션 저장소. 대상 파일 ID는 표 기준 `field`, 생성 시간은 `createAt`."""

    COLLECTION = "processing_jobs"
    INDEXES = [
        # 필드명은 schema.md 표 기준(field·createAt). 예시의 fileId·createdAt과 차이는 PRD 7.7
        # 파일별 작업 이력(최신순)과 진행 중 작업 확인(has_active)용
        IndexModel([("field", ASCENDING), ("createAt", DESCENDING)], name="field_createAt"),
        # 워커의 작업 선점(queued 중 가장 오래된 것)이 정렬 없이 인덱스 순서로 바로 찾도록 오름차순
        IndexModel([("status", ASCENDING), ("createAt", ASCENDING)], name="status_createAt"),
        # 작업 종류(analyze·clean·report 등)별 조회·집계용
        IndexModel([("type", ASCENDING)], name="type"),
        IndexModel([("createAt", DESCENDING)], name="createAt_desc"),
    ]

    def __init__(self, db: AsyncDatabase[Document]) -> None:
        """processing_jobs 저장소를 만든다.

        파라미터:
            db: 사용할 MongoDB 데이터베이스
        """
        super().__init__(db, JobDocument)

    async def list_by_file(self, file_id: ObjectId) -> list[JobDocument]:
        """한 파일의 작업 목록을 최신순으로 돌려준다. 재처리 이력 확인에 쓴다.

        파라미터:
            file_id: 대상 파일 _id
        """
        return await self._find({"field": file_id}, [("createAt", DESCENDING)])

    async def claim_next(self, types: Collection[str]) -> JobDocument | None:
        """가장 오래된 queued 작업 하나를 processing으로 바꿔 가져간다. 없으면 None.

        한 번의 원자적 갱신으로 가져가므로 워커가 여러 개여도 같은 작업을 두 번 실행하지 않는다.

        파라미터:
            types: 이 워커가 처리할 수 있는 작업 종류(type) 목록
        """
        raw = await self.collection.find_one_and_update(
            # 이 워커에 처리기가 없는 종류는 다른 워커가 가져가도록 남겨 둔다
            {"status": "queued", "type": {"$in": list(types)}},
            {"$set": {"status": "processing", "stage": "started", "startedAt": utc_now()}},
            # 먼저 들어온 작업부터 처리(FIFO)해 오래 기다리는 작업이 없게 한다
            sort=[("createAt", ASCENDING)],
            # 바뀐 뒤 문서를 받아 status·startedAt이 갱신된 상태로 처리기에 넘긴다
            return_document=ReturnDocument.AFTER,
        )
        return None if raw is None else JobDocument.model_validate(raw)

    async def has_active(self, file_id: ObjectId) -> bool:
        """한 파일에 아직 끝나지 않은(queued·processing) 작업이 있는지 본다.

        파라미터:
            file_id: 대상 파일 _id
        """
        # 존재 여부만 보면 되므로 _id만 가져온다. 같은 파일에 작업이 겹치는 것을 막는 데 쓴다
        found = await self.collection.find_one(
            {"field": file_id, "status": {"$in": ["queued", "processing"]}}, {"_id": 1}
        )
        return found is not None

    async def list_stale(self, started_before: datetime) -> list[JobDocument]:
        """started_before보다 먼저 시작해 아직 processing인 작업(멈춘 워커의 작업)을 찾는다.

        파라미터:
            started_before: 이 시각 이전에 시작한 작업만
        """
        return await self._find(
            # 워커가 죽으면 processing에 머문 채 남으므로, 시작한 지 오래된 것을 시간 초과로 찾는다
            {"status": "processing", "startedAt": {"$lt": started_before}},
            [("startedAt", ASCENDING)],
        )

    async def _set(self, job_id: ObjectId, changes: dict[str, object]) -> bool:
        """작업 문서의 필드를 바꾼다. 문서가 있었으면 True.

        파라미터:
            job_id: 바꿀 작업 _id
            changes: DB 필드명(camelCase) → 새 값
        """
        result = await self.collection.update_one({"_id": job_id}, {"$set": changes})
        return result.matched_count == 1

    async def mark_processing(self, job_id: ObjectId, stage: str) -> bool:
        """작업을 시작 상태로 바꾸고 시작 시간을 기록한다.

        파라미터:
            job_id: 작업 _id
            stage: 첫 단계 이름(예: validating)
        """
        return await self._set(
            job_id, {"status": "processing", "stage": stage, "startedAt": utc_now()}
        )

    async def update_progress(self, job_id: ObjectId, stage: str, progress: int) -> bool:
        """진행 중인 작업의 단계와 진행률을 바꾼다.

        파라미터:
            job_id: 작업 _id
            stage: 현재 단계 이름
            progress: 진행률(0~100)
        """
        # schema.md: progress는 0~100. 잘못된 값이 DB에 들어가기 전에 호출한 코드의 버그로 드러낸다
        if not 0 <= progress <= 100:
            raise ValueError("progress는 0~100 사이여야 합니다.")
        return await self._set(job_id, {"stage": stage, "progress": progress})

    async def mark_completed(self, job_id: ObjectId) -> bool:
        """처리 중인 작업을 완료 상태(진행률 100)로 바꾸고 완료 시간을 기록한다.

        이미 실패 처리된 작업(예: 시간 초과)은 바꾸지 않는다. 바꿨으면 True.

        파라미터:
            job_id: 작업 _id
        """
        result = await self.collection.update_one(
            # 조건에 status를 넣어, 처리 중 시간 초과로 실패 처리된 작업을 완료로 덮어쓰지 않는다
            {"_id": job_id, "status": "processing"},
            {
                "$set": {
                    "status": "completed",
                    "stage": "completed",
                    "progress": 100,
                    "completedAt": utc_now(),
                }
            },
        )
        return result.modified_count == 1

    async def mark_failed(self, job_id: ObjectId, error: JobError) -> bool:
        """처리 중인 작업을 실패 상태로 바꾼다. 실패한 단계(stage)는 그대로 남긴다.

        이미 끝난(completed·failed) 작업은 바꾸지 않는다. 바꿨으면 True.

        파라미터:
            job_id: 작업 _id
            error: 오류 코드와 메시지
        """
        result = await self.collection.update_one(
            # 끝난 작업의 결과·오류를 덮어쓰지 않도록 아직 끝나지 않은 작업만 실패로 바꾼다.
            # stage는 건드리지 않아 어느 단계에서 실패했는지 남는다(processing_jobs 실패 원인 추적)
            {"_id": job_id, "status": {"$in": ["queued", "processing"]}},
            {"$set": {"status": "failed", "error": error.to_document(), "completedAt": utc_now()}},
        )
        return result.modified_count == 1

    async def delete_by_file(self, file_id: ObjectId) -> int:
        """한 파일의 작업을 모두 지우고 지운 개수를 돌려준다.

        파라미터:
            file_id: 대상 파일 _id
        """
        # 파일 삭제 시 함께 지운다(명세서: 파일 및 관련 결과 삭제)
        result = await self.collection.delete_many({"field": file_id})
        return result.deleted_count
