"""백그라운드 작업 워커. 실행: python -m app.worker

API 서버와 별도 프로세스로 띄운다. 처리량을 늘리려면 여러 개를 띄워도 된다.
Ctrl+C로 끄면 진행 중이던 작업은 WORKER_STOPPED로 실패 처리된다.
"""

import asyncio
import contextlib
import logging

from app.core.config import get_settings
from app.core.database import Mongo
from app.repositories.indexes import ensure_indexes
from app.services.job_handlers import HANDLERS
from app.services.job_runner import JobRunner


async def main() -> None:
    """DB에 연결해 워커를 실행한다.

    파라미터: 없음. 설정은 환경 변수와 .env에서 읽는다.
    """
    settings = get_settings()
    mongo = Mongo(settings.mongodb_uri, settings.mongodb_db_name)
    try:
        # 워커가 API보다 먼저 떠도 작업 선점 조회에 쓰는 인덱스가 있도록 여기서도 확인한다
        await ensure_indexes(mongo.db)
        # 멈춤 신호(Event)는 쓰지 않고 Ctrl+C(KeyboardInterrupt)로 끈다. 테스트는 Event로 멈춘다
        await JobRunner(mongo.db, settings, HANDLERS).run_forever(asyncio.Event())
    finally:
        await mongo.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Ctrl+C로 끌 때 긴 traceback 대신 조용히 끝나도록 한다
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
