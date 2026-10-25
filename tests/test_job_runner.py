"""워커(JobRunner) 통합 테스트. 실제 MongoDB가 필요하다(conftest가 임시 DB를 띄운다)."""

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.core.errors import AppError
from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.schemas.file import FileDocument
from app.schemas.job import JobDocument, JobError
from app.schemas.mongo import utc_now
from app.services.job_runner import JobContext, JobHandler, JobRunner
from tests.conftest import make_file_doc, make_settings, requires_db

pytestmark = [pytest.mark.anyio, requires_db]

TEST_TYPE = "test_job"


async def seed(
    db: AsyncDatabase[Document], job_type: str = TEST_TYPE
) -> tuple[FileDocument, JobDocument]:
    """파일 하나와 그 파일의 queued 작업 하나를 저장한다.

    파라미터:
        db: 테스트 DB
        job_type: 작업 종류
    """
    file = await FileRepository(db).insert(make_file_doc())
    job = await JobRepository(db).insert(JobDocument(field=file.id, type=job_type))
    return file, job


def runner(db: AsyncDatabase[Document], tmp_path: Path, handler: JobHandler) -> JobRunner:
    """TEST_TYPE 처리기 하나를 등록한 실행기를 만든다.

    파라미터:
        db: 테스트 DB
        tmp_path: 저장소 임시 폴더
        handler: TEST_TYPE 작업 처리기
    """
    return JobRunner(db, make_settings(tmp_path), {TEST_TYPE: handler})


async def reload(
    db: AsyncDatabase[Document], file: FileDocument, job: JobDocument
) -> tuple[FileDocument, JobDocument]:
    """DB에서 파일·작업을 다시 읽는다.

    파라미터:
        db: 테스트 DB
        file: 다시 읽을 파일
        job: 다시 읽을 작업
    """
    new_file = await FileRepository(db).get(file.id)
    new_job = await JobRepository(db).get(job.id)
    assert new_file is not None
    assert new_job is not None
    return new_file, new_job


async def test_successful_job(db: AsyncDatabase[Document], tmp_path: Path) -> None:
    file, job = await seed(db)
    seen: list[tuple[str, str]] = []

    async def handler(ctx: JobContext) -> None:
        seen.append((ctx.file.original_name, ctx.job.status))
        await ctx.report("parsing", 50)

    assert await runner(db, tmp_path, handler).run_once()

    file, job = await reload(db, file, job)
    assert seen == [("sales.csv", "processing")]
    assert (job.status, job.stage, job.progress, job.error) == ("completed", "completed", 100, None)
    assert job.started_at is not None
    assert job.completed_at is not None
    assert file.status == "completed"


async def test_app_error_marks_failed_and_keeps_stage(
    db: AsyncDatabase[Document], tmp_path: Path
) -> None:
    file, job = await seed(db)

    async def handler(ctx: JobContext) -> None:
        await ctx.report("parsing", 30)
        raise AppError("PARSE_ERROR", "CSV를 읽을 수 없습니다.")

    await runner(db, tmp_path, handler).run_once()

    file, job = await reload(db, file, job)
    assert job.status == "failed"
    assert (job.stage, job.progress) == ("parsing", 30)
    assert job.error == JobError(code="PARSE_ERROR", message="CSV를 읽을 수 없습니다.")
    assert file.status == "failed"


async def test_unexpected_error_hides_detail(db: AsyncDatabase[Document], tmp_path: Path) -> None:
    file, job = await seed(db)

    async def handler(ctx: JobContext) -> None:
        raise RuntimeError("secret internal detail")

    await runner(db, tmp_path, handler).run_once()

    _, job = await reload(db, file, job)
    assert job.error is not None
    assert job.error.code == "INTERNAL_ERROR"
    assert "secret" not in job.error.message


async def test_ignores_job_types_without_handler(
    db: AsyncDatabase[Document], tmp_path: Path
) -> None:
    file, job = await seed(db, job_type="analyze")

    async def handler(ctx: JobContext) -> None:
        raise AssertionError("호출되면 안 됨")

    assert not await runner(db, tmp_path, handler).run_once()
    _, job = await reload(db, file, job)
    assert job.status == "queued"


async def test_missing_file_fails_job(db: AsyncDatabase[Document], tmp_path: Path) -> None:
    job = await JobRepository(db).insert(JobDocument(field=ObjectId(), type=TEST_TYPE))

    async def handler(ctx: JobContext) -> None:
        raise AssertionError("호출되면 안 됨")

    await runner(db, tmp_path, handler).run_once()

    failed = await JobRepository(db).get(job.id)
    assert failed is not None
    assert failed.error is not None
    assert failed.error.code == "FILE_NOT_FOUND"


async def test_oldest_first_and_no_double_execution(
    db: AsyncDatabase[Document], tmp_path: Path
) -> None:
    jobs = [(await seed(db))[1] for _ in range(5)]
    executed: list[ObjectId] = []

    async def handler(ctx: JobContext) -> None:
        executed.append(ctx.job.id)
        await asyncio.sleep(0.01)

    # 워커 3개가 동시에 큐를 비운다
    workers = [runner(db, tmp_path, handler) for _ in range(3)]

    async def drain(worker: JobRunner) -> None:
        while await worker.run_once():
            pass

    await asyncio.gather(*(drain(w) for w in workers))

    assert sorted(executed) == sorted(j.id for j in jobs)
    assert len(set(executed)) == len(executed)


async def test_claim_order_is_oldest_first(db: AsyncDatabase[Document], tmp_path: Path) -> None:
    jobs = [(await seed(db))[1] for _ in range(3)]
    executed: list[ObjectId] = []

    async def handler(ctx: JobContext) -> None:
        executed.append(ctx.job.id)

    worker = runner(db, tmp_path, handler)
    while await worker.run_once():
        pass

    assert executed == [j.id for j in jobs]


async def test_stale_job_is_failed_and_not_completed_later(
    db: AsyncDatabase[Document], tmp_path: Path
) -> None:
    file, job = await seed(db)
    repo = JobRepository(db)
    await repo.claim_next([TEST_TYPE])
    two_hours_ago = utc_now() - timedelta(hours=2)
    await repo.collection.update_one({"_id": job.id}, {"$set": {"startedAt": two_hours_ago}})

    async def handler(ctx: JobContext) -> None:
        pass

    assert await runner(db, tmp_path, handler).fail_stale_jobs() == 1

    file, job = await reload(db, file, job)
    assert job.status == "failed"
    assert job.error is not None
    assert job.error.code == "JOB_TIMEOUT"
    assert file.status == "failed"
    # 늦게 끝난 워커가 완료로 덮어쓰지 못한다
    assert not await repo.mark_completed(job.id)


async def test_cancelled_job_is_marked_stopped(db: AsyncDatabase[Document], tmp_path: Path) -> None:
    file, job = await seed(db)
    started = asyncio.Event()

    async def handler(ctx: JobContext) -> None:
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(runner(db, tmp_path, handler).run_once())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _, job = await reload(db, file, job)
    assert job.status == "failed"
    assert job.error is not None
    assert job.error.code == "WORKER_STOPPED"


async def test_run_forever_processes_and_stops(db: AsyncDatabase[Document], tmp_path: Path) -> None:
    file, job = await seed(db)
    stop = asyncio.Event()

    async def handler(ctx: JobContext) -> None:
        stop.set()

    worker = JobRunner(
        db,
        make_settings(tmp_path).model_copy(update={"worker_poll_seconds": 0.05}),
        {TEST_TYPE: handler},
    )
    await asyncio.wait_for(worker.run_forever(stop), timeout=5)

    _, job = await reload(db, file, job)
    assert job.status == "completed"
