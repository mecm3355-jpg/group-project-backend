from bson import ObjectId
from bson.errors import InvalidId

from app.core.errors import AppError
from app.repositories.jobs import JobRepository
from app.schemas.job import JobDocument


class JobService:
    """처리 작업 조회 업무 로직."""

    def __init__(self, jobs: JobRepository) -> None:
        """서비스를 만든다.

        파라미터:
            jobs: processing_jobs 저장소
        """
        self._jobs = jobs

    async def get(self, job_id: str) -> JobDocument:
        """작업 하나를 돌려준다. ID 형식이 틀리거나 없으면 404.

        파라미터:
            job_id: 작업 ID 문자열(MongoDB _id)
        """
        # 형식이 틀린 ID도 400이 아닌 404로 답한다. 어차피 그런 작업은 없고, 프론트는 한 가지
        # 경우만 처리하면 된다
        try:
            oid = ObjectId(job_id)
        except (InvalidId, TypeError) as exc:
            raise AppError("JOB_NOT_FOUND", "작업을 찾을 수 없습니다.", 404) from exc
        doc = await self._jobs.get(oid)
        if doc is None:
            raise AppError("JOB_NOT_FOUND", "작업을 찾을 수 없습니다.", 404)
        return doc
