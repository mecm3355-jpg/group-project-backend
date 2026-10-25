from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.repositories.results import ResultRepository


async def ensure_indexes(db: AsyncDatabase[Document]) -> None:
    """기준 컬렉션 3개(files, processing_jobs, processing_results)의 인덱스를 만든다.

    앱 시작 시 한 번 호출한다. 이미 있는 인덱스는 그대로 둔다.

    파라미터:
        db: 인덱스를 만들 MongoDB 데이터베이스
    """
    await FileRepository(db).ensure_indexes()
    await JobRepository(db).ensure_indexes()
    await ResultRepository(db).ensure_indexes()
