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


# 처리기는 결과를 돌려주지 않는다. 결과는 처리기가 직접 저장하고, 성공·실패는 예외 여부로 판단한다
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
        # 처리기가 하나도 없으면 $in: []로 조회할 필요 없이 바로 쉰다
        if not self._handlers:
            return False
        # 처리할 수 있는 종류만 가져간다. 다른 종류는 그 처리기를 가진 워커가 가져가도록 남긴다
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
        # 작업이 큐에 있는 동안 파일이 삭제됐을 수 있다. 처리기를 부르기 전에 실패로 정리한다
        # (파일이 없으니 files.status는 바꿀 대상이 없다)
        file = await self._files.get(job.field)
        if file is None:
            await self._jobs.mark_failed(
                job.id, JobError(code="FILE_NOT_FOUND", message="대상 파일이 없습니다.")
            )
            return

        # 목록 화면이 파일 상태만 보고도 처리 중임을 알 수 있게 files.status를 함께 바꾼다
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
            # shield: 취소 중에도 실패 기록 DB 호출만은 끝까지 가게 한다.
            # 그 뒤 다시 올려 워커 종료 흐름(취소 전파)을 막지 않는다
            await asyncio.shield(self._fail(job, error))
            raise
        except AppError as exc:
            # 처리기가 의도한 오류: 코드·메시지를 그대로 사용자에게 보여 줄 수 있다
            await self._fail(job, JobError(code=exc.code, message=exc.message))
        except Exception:
            # 예상 못 한 오류: 내부 정보가 새지 않게 메시지는 일반 문구로 두고, 자세한 내용은
            # 스택과 함께 서버 로그에만 남긴다. 예외를 삼켜 워커가 다음 작업을 계속 하게 한다
            logger.exception("작업 실행 실패: job=%s type=%s", job.id, job.type)
            await self._fail(
                job, JobError(code="INTERNAL_ERROR", message="작업 처리 중 오류가 발생했습니다.")
            )
        else:
            # 처리 중에 시간 초과로 이미 실패 처리됐다면 완료로 덮어쓰지 않으며,
            # 파일 상태도 그대로 둔다
            if await self._jobs.mark_completed(job.id):
                await self._files.update_status(job.field, "completed")

    async def _fail(self, job: JobDocument, error: JobError) -> None:
        """작업과 대상 파일을 실패 상태로 기록한다.

        파라미터:
            job: 실패한 작업
            error: 오류 코드와 메시지
        """
        # 작업을 실제로 실패로 바꾼 경우에만 파일 상태를 바꾼다. 이미 다른 경로(시간 초과 등)로
        # 끝난 작업이면 파일 상태를 두 번 건드리지 않는다
        if await self._jobs.mark_failed(job.id, error):
            await self._files.update_status(job.field, "failed")

    async def fail_stale_jobs(self) -> int:
        """제한 시간(job_timeout_seconds)을 넘겨 processing에 남은 작업을 실패 처리한다.

        워커가 비정상 종료해 끝나지 못한 작업을 정리한다. 실패 처리한 개수를 돌려준다.

        파라미터: 없음
        """
        # startedAt이 이 시각보다 이른 processing 작업은 제한 시간을 넘긴 것으로 본다
        cutoff = utc_now() - timedelta(seconds=self._settings.job_timeout_seconds)
        count = 0
        for job in await self._jobs.list_stale(cutoff):
            error = JobError(code="JOB_TIMEOUT", message="작업 시간이 초과되어 중단되었습니다.")
            # 조회와 갱신 사이에 작업이 끝났을 수 있어 갱신 결과로 다시 확인한다
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
            # 별도 감시 프로세스 없이, 작업을 가져가기 전마다 멈춘 작업을 정리한다
            await self.fail_stale_jobs()
            # 작업이 있었으면 쉬지 않고 바로 다음 작업을 본다(밀린 큐를 빨리 비운다)
            if await self.run_once():
                continue
            # sleep 대신 stop.wait에 제한 시간을 걸어, 쉬는 중에도 종료 신호에 바로 반응한다
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._settings.worker_poll_seconds)
            except TimeoutError:
                pass
        logger.info("워커 종료")
