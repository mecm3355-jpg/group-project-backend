"""저장소 통합 테스트. 환경 변수 MONGODB_TEST_URI가 있을 때만 실행한다.

테스트마다 임시 데이터베이스를 만들고 끝나면 지운다. 운영 DB 연결 문자열은 쓰지 않는다.
"""

import pytest
from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.core.database import Document
from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.repositories.results import ResultRepository
from app.schemas.job import JobDocument, JobError
from app.schemas.result import ResultDocument
from tests.conftest import make_file_doc, requires_db

pytestmark = [pytest.mark.anyio, requires_db]

_file = make_file_doc


async def test_indexes_created(db: AsyncDatabase[Document]) -> None:
    names = {
        "files": {"createdAt_desc", "status_createdAt", "lastJobId"},
        "processing_jobs": {"field_createAt", "status_createAt", "type", "createAt_desc"},
        "processing_results": {"field_resultType_version", "jobId", "createAt_desc"},
    }
    for collection, expected in names.items():
        info = await db[collection].index_information()
        assert expected <= set(info), collection


async def test_file_insert_get_search_update_delete(db: AsyncDatabase[Document]) -> None:
    repo = FileRepository(db)
    sales = await repo.insert(_file("Sales_2026.csv"))
    await repo.insert(_file("photo.jpg"))

    assert await repo.get(sales.id) == sales
    assert [f.original_name for f in await repo.search(name="sales")] == ["Sales_2026.csv"]
    assert await repo.count() == 2
    # 정규식 특수문자는 글자 그대로 검색
    assert await repo.search(name=".*") == []

    job_id = ObjectId()
    assert await repo.update_status(sales.id, "processing", last_job_id=job_id)
    updated = await repo.get(sales.id)
    assert updated is not None
    assert updated.status == "processing"
    assert updated.last_job_id == job_id
    assert updated.updated_at >= sales.updated_at
    assert await repo.count(status="processing") == 1

    assert await repo.delete(sales.id)
    assert await repo.get(sales.id) is None
    assert not await repo.delete(sales.id)


async def test_job_lifecycle(db: AsyncDatabase[Document]) -> None:
    repo = JobRepository(db)
    file_id = ObjectId()
    job = await repo.insert(JobDocument(field=file_id, type="analyze"))

    await repo.mark_processing(job.id, "validating")
    await repo.update_progress(job.id, "parsing", 40)
    await repo.mark_failed(job.id, JobError(code="PARSE_ERROR", message="CSV를 읽을 수 없습니다."))

    failed = await repo.get(job.id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.stage == "parsing"  # 실패한 단계가 남는다
    assert failed.progress == 40
    assert failed.error == JobError(code="PARSE_ERROR", message="CSV를 읽을 수 없습니다.")
    assert failed.started_at is not None
    assert failed.completed_at is not None

    retry = await repo.insert(JobDocument(field=file_id, type="analyze"))
    await repo.mark_processing(retry.id, "validating")
    await repo.mark_completed(retry.id)
    done = await repo.get(retry.id)
    assert done is not None
    assert (done.status, done.stage, done.progress) == ("completed", "completed", 100)

    history = await repo.list_by_file(file_id)
    assert [j.id for j in history] == [retry.id, job.id]
    assert await repo.delete_by_file(file_id) == 2


async def test_update_progress_rejects_out_of_range(db: AsyncDatabase[Document]) -> None:
    repo = JobRepository(db)
    job = await repo.insert(JobDocument(field=ObjectId(), type="analyze"))

    with pytest.raises(ValueError):
        await repo.update_progress(job.id, "parsing", 101)


async def test_result_versions(db: AsyncDatabase[Document]) -> None:
    repo = ResultRepository(db)
    file_id = ObjectId()

    assert await repo.next_version(file_id, "cleaning") == 1
    for _ in range(2):
        version = await repo.next_version(file_id, "cleaning")
        await repo.insert(
            ResultDocument(
                field=file_id, job_id=ObjectId(), result_type="cleaning", version=version
            )
        )
    await repo.insert(
        ResultDocument(field=file_id, job_id=ObjectId(), result_type="analysis", version=1)
    )

    latest = await repo.get_latest(file_id, "cleaning")
    assert latest is not None
    assert latest.version == 2
    assert [r.version for r in await repo.list_by_file(file_id, "cleaning")] == [2, 1]
    assert len(await repo.list_by_file(file_id)) == 3

    # 같은 파일·종류·버전은 중복 저장할 수 없다
    with pytest.raises(DuplicateKeyError):
        await repo.insert(
            ResultDocument(field=file_id, job_id=ObjectId(), result_type="cleaning", version=2)
        )

    assert await repo.delete_by_file(file_id) == 3
