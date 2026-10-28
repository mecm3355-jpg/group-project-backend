from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.core.errors import AppError

# MongoDB 문서 자료형. Any 금지 규칙 때문에 값은 object로 두고 쓰는 곳에서 좁힌다
Document = dict[str, object]


class Mongo:
    """MongoDB 클라이언트와 기본 데이터베이스를 묶어 관리한다."""

    def __init__(self, uri: str, db_name: str) -> None:
        """클라이언트를 만든다. 실제 연결은 첫 요청 때 이루어진다.

        파라미터:
            uri: MongoDB 연결 문자열(.env의 MONGODB_URI)
            db_name: 사용할 데이터베이스 이름
        """
        # tz_aware=True: 읽은 날짜에 UTC 시각대를 붙여 naive/aware 비교 오류를 막는다
        self._client: AsyncMongoClient[Document] = AsyncMongoClient(uri, tz_aware=True)
        self.db: AsyncDatabase[Document] = self._client[db_name]

    async def ping(self) -> None:
        """DB 연결 상태를 확인한다. 실패하면 pymongo 예외가 그대로 올라간다.

        파라미터: 없음
        """
        await self._client.admin.command("ping")

    async def close(self) -> None:
        """클라이언트 연결을 닫는다. 앱 종료 시 호출한다.

        파라미터: 없음
        """
        await self._client.close()


# 프로세스당 클라이언트(연결 풀) 하나를 공유하기 위한 전역. create_app의 lifespan이 채운다
_mongo: Mongo | None = None


def set_mongo(mongo: Mongo | None) -> None:
    """앱 수명 주기에서 사용할 Mongo 인스턴스를 등록하거나 해제한다.

    파라미터:
        mongo: 등록할 인스턴스. 해제할 때는 None
    """
    global _mongo
    _mongo = mongo


def get_mongo() -> Mongo:
    """라우터 의존성으로 쓰는 Mongo 인스턴스를 돌려준다.

    파라미터: 없음
    """
    # lifespan 시작 전이나 종료 후 요청이 들어온 경우. 일시적 상태라 503으로 알린다
    if _mongo is None:
        raise AppError("DB_NOT_READY", "데이터베이스가 준비되지 않았습니다.", 503)
    return _mongo
