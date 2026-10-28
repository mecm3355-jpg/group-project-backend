from pymongo.asynchronous.database import AsyncDatabase

from app.core.database import Document
from app.repositories.candidates import CandidateRepository
from app.repositories.categories import CategoryRepository
from app.repositories.complaints import ComplaintRepository
from app.repositories.files import FileRepository
from app.repositories.issues import IssueRepository
from app.repositories.jobs import JobRepository
from app.repositories.results import ResultRepository
from app.repositories.reviews import ReviewRepository


async def ensure_indexes(db: AsyncDatabase[Document]) -> None:
    """기준 컬렉션 3개와 민원 전용 컬렉션 5개(PRD 7.3·7.4 제안)의 인덱스를 만든다.

    앱 시작 시 한 번 호출한다. 이미 있는 인덱스는 그대로 둔다.

    파라미터:
        db: 인덱스를 만들 MongoDB 데이터베이스
    """
    # 기준 컬렉션(docs/db-schema/schema.md) 먼저: 민원 컬렉션이 없어도 파일 처리는 돌아야 한다
    await FileRepository(db).ensure_indexes()
    await JobRepository(db).ensure_indexes()
    await ResultRepository(db).ensure_indexes()
    # 민원 전용 컬렉션(PRD 5.2·7.3·7.4 제안). 저장소마다 생성자 인자가 같아 반복문으로 처리한다
    for repository in (
        CategoryRepository(db),
        CandidateRepository(db),
        ComplaintRepository(db),
        ReviewRepository(db),
        IssueRepository(db),
    ):
        await repository.ensure_indexes()
