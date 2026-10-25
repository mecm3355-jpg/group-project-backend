from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.repositories.base import BaseRepository
from app.schemas.job import JobDocument, JobError
from app.schemas.mongo import utc_now


class JobRepository(BaseRepository[JobDocument]):
    """processing_jobs 컬렉션 저장소. 대상 파일 ID는 표 기준 `field`, 생성 시간은 `createAt`."""

    COLLECTION = "processing_jobs"
    INDEXES = [
        IndexModel([("field", ASCENDING), ("createAt", DESCENDING)], name="field_createAt"),
        IndexModel([("status", ASCENDING), ("createAt", ASCENDING)], name="status_createAt"),
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
        if not 0 <= progress <= 100:
            raise ValueError("progress는 0~100 사이여야 합니다.")
        return await self._set(job_id, {"stage": stage, "progress": progress})

    async def mark_completed(self, job_id: ObjectId) -> bool:
        """작업을 완료 상태(진행률 100)로 바꾸고 완료 시간을 기록한다.

        파라미터:
            job_id: 작업 _id
        """
        return await self._set(
            job_id,
            {
                "status": "completed",
                "stage": "completed",
                "progress": 100,
                "completedAt": utc_now(),
            },
        )

    async def mark_failed(self, job_id: ObjectId, error: JobError) -> bool:
        """작업을 실패 상태로 바꾼다. 실패한 단계(stage)는 그대로 남긴다.

        파라미터:
            job_id: 작업 _id
            error: 오류 코드와 메시지
        """
        return await self._set(
            job_id,
            {"status": "failed", "error": error.to_document(), "completedAt": utc_now()},
        )

    async def delete_by_file(self, file_id: ObjectId) -> int:
        """한 파일의 작업을 모두 지우고 지운 개수를 돌려준다.

        파라미터:
            file_id: 대상 파일 _id
        """
        result = await self.collection.delete_many({"field": file_id})
        return result.deleted_count
