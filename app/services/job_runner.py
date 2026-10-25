"""processing_jobs 큐에서 작업을 가져와 실행하는 워커 로직(PRD 7.2, 12장의 MongoDB 기반 작업 큐)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta

from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings
from app.core.database import Document
from app.core.errors import AppError
from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.schemas.file import FileDocument
from app.schemas.job import JobDocument, JobError
from app.schemas.mongo import utc_now
from app.services.storage import LocalStorage

logger = logging.getLogger(__name__)


@dataclass
class JobContext:
    """작업 처리기에 넘기는 실행 정보와 진행 보고 수단."""

    job: JobDocument
    file: FileDocument
    db: AsyncDatabase[Document]
    storage: LocalStorage
    settings: Settings
    jobs: JobRepository

    async def report(self, stage: str, progress: int) -> None:
        """현재 단계와 진행률(0~100)을 기록한다. 실패하면 마지막으로 기록한 단계가 남는다.

        파라미터:
            stage: 단계 이름(예: validating, parsing, quality_check)
            progress: 진행률(0~100)
        """
        await self.jobs.update_progress(self.job.id, stage, progress)


JobHandler = Callable[[JobContext], Awaitable[None]]


class JobRunner:
    """작업 종류(type)별 처리기를 등록해 두고, 큐의 작업을 하나씩 실행한다.

    처리기가 정상 종료하면 completed, AppError를 던지면 그 코드·메시지로 failed,
    그 밖의 예외는 INTERNAL_ERROR로 failed가 된다. 파일 상태(files.status)도 함께 바꾼다.
    """

    def __init__(
        self,
        db: AsyncDatabase[Document],
        settings: Settings,
        handlers: Mapping[str, JobHandler],
    ) -> None:
        """실행기를 만든다.

        파라미터:
            db: MongoDB 데이터베이스
            settings: 앱 설정(저장소 위치, 폴링 간격, 작업 제한 시간)
            handlers: 작업 종류 → 처리기. 여기 없는 종류의 작업은 가져가지 않는다
        """
        self._db = db
        self._settings = settings
        self._handlers = dict(handlers)
        self._jobs = JobRepository(db)
        self._files = FileRepository(db)
        self._storage = LocalStorage(settings.storage_root)

    async def run_once(self) -> bool:
        """대기 작업 하나를 가져와 실행한다. 실행할 작업이 있었으면 True.

        파라미터: 없음
        """
        if not self._handlers:
            return False
        job = await self._jobs.claim_next(self._handlers.keys())
        if job is None:
            return False
        await self._execute(job)
        return True

    async def _execute(self, job: JobDocument) -> None:
        """가져온 작업 하나를 실행하고 결과에 따라 작업·파일 상태를 기록한다.

        파라미터:
            job: processing 상태로 가져온 작업
        """
        file = await self._files.get(job.field)
        if file is None:
            await self._jobs.mark_failed(
                job.id, JobError(code="FILE_NOT_FOUND", message="대상 파일이 없습니다.")
            )
            return

        await self._files.update_status(file.id, "processing")
        context = JobContext(
            job=job,
            file=file,
            db=self._db,
            storage=self._storage,
            settings=self._settings,
            jobs=self._jobs,
        )
        try:
            await self._handlers[job.type](context)
        except asyncio.CancelledError:
            # 워커 종료로 끊긴 작업은 실패로 남겨 다시 처리할 수 있게 한다
            error = JobError(
                code="WORKER_STOPPED", message="워커가 종료되어 작업이 중단되었습니다."
            )
            await asyncio.shield(self._fail(job, error))
            raise
        except AppError as exc:
            await self._fail(job, JobError(code=exc.code, message=exc.message))
        except Exception:
            logger.exception("작업 실행 실패: job=%s type=%s", job.id, job.type)
            await self._fail(
                job, JobError(code="INTERNAL_ERROR", message="작업 처리 중 오류가 발생했습니다.")
            )
        else:
            if await self._jobs.mark_completed(job.id):
                await self._files.update_status(job.field, "completed")

    async def _fail(self, job: JobDocument, error: JobError) -> None:
        """작업과 대상 파일을 실패 상태로 기록한다.

        파라미터:
            job: 실패한 작업
            error: 오류 코드와 메시지
        """
        if await self._jobs.mark_failed(job.id, error):
            await self._files.update_status(job.field, "failed")

    async def fail_stale_jobs(self) -> int:
        """제한 시간(job_timeout_seconds)을 넘겨 processing에 남은 작업을 실패 처리한다.

        워커가 비정상 종료해 끝나지 못한 작업을 정리한다. 실패 처리한 개수를 돌려준다.

        파라미터: 없음
        """
        cutoff = utc_now() - timedelta(seconds=self._settings.job_timeout_seconds)
        count = 0
        for job in await self._jobs.list_stale(cutoff):
            error = JobError(code="JOB_TIMEOUT", message="작업 시간이 초과되어 중단되었습니다.")
            if await self._jobs.mark_failed(job.id, error):
                await self._files.update_status(job.field, "failed")
                count += 1
        return count

    async def run_forever(self, stop: asyncio.Event) -> None:
        """stop이 설정될 때까지 작업을 계속 실행한다. 대기 작업이 없으면 잠시 쉰다.

        파라미터:
            stop: 설정되면 다음 작업을 가져가지 않고 끝낸다
        """
        logger.info("워커 시작: 처리 작업 종류=%s", sorted(self._handlers))
        while not stop.is_set():
            await self.fail_stale_jobs()
            if await self.run_once():
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._settings.worker_poll_seconds)
            except TimeoutError:
                pass
        logger.info("워커 종료")
