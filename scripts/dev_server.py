"""DB 없이 로컬에서 실행해 보기: 임시 MongoDB + API 서버 + 워커를 함께 띄운다.

실행: python scripts/dev_server.py  (Ctrl+C로 종료)
- MongoDB는 이 PC에 임시로 띄우며, 종료하면 데이터가 사라진다(Atlas 연결 불필요).
- 원본·산출물은 storage-dev/ 폴더에 저장한다(git 제외).
- .env의 AZURE_OPENAI_* 값은 그대로 쓴다. MONGODB_URI는 무시한다.
- 화면: http://127.0.0.1:8000/docs
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

from pymongo_inmemory import MongoClient

ROOT = Path(__file__).resolve().parent.parent
PORT = os.environ.get("DEV_PORT", "8000")


def _stop(signum: int, frame: FrameType | None) -> None:
    """종료 신호(창 닫기·SIGTERM)를 Ctrl+C와 같게 처리해 정리 단계를 거치게 한다.

    파라미터:
        signum: 받은 신호 번호
        frame: 신호를 받은 시점의 실행 프레임(사용하지 않음)
    """
    raise KeyboardInterrupt


def main() -> None:
    """임시 MongoDB를 띄우고 API 서버·워커를 실행한 뒤, Ctrl+C까지 기다렸다가 모두 정리한다.

    파라미터: 없음
    """
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)
    print("임시 MongoDB를 준비하는 중입니다(처음에는 내려받기로 시간이 걸릴 수 있습니다)...")
    mongo: MongoClient = MongoClient()
    env = dict(
        os.environ,
        MONGODB_URI=mongo._mongod.connection_string,
        MONGODB_DB_NAME="datalens_dev",
        STORAGE_ROOT=str(ROOT / "storage-dev"),
        PYTHONIOENCODING="utf-8",
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", PORT],
            cwd=ROOT,
            env=env,
        ),
        subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=ROOT, env=env),
    ]
    print(f"\n실행 중: http://127.0.0.1:{PORT}/docs  (종료: Ctrl+C)\n")
    try:
        while all(p.poll() is None for p in processes):
            time.sleep(1)
        print("API 서버 또는 워커가 종료되었습니다.")
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        mongo.close()
        print("종료했습니다. 임시 DB 데이터는 삭제되었습니다.")


if __name__ == "__main__":
    main()
