"""업로드 → 워커 처리 → 품질 조회·미리보기·재처리 흐름. 실제 MongoDB 필요(임시 DB 자동)."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import Mongo
from app.services.job_handlers import HANDLERS
from app.services.job_runner import JobRunner
from tests.conftest import requires_db
from tests.document_files import make_docx, make_pdf

pytestmark = requires_db

CSV = "이름,나이,이메일\n홍길동,30,a@x.com\n김철수,모름,\n홍길동,30,a@x.com\n".encode("cp949")


def run_worker(client: TestClient) -> int:
    """테스트 앱과 같은 DB로 워커를 돌려 대기 작업을 모두 처리한다. 처리한 작업 수를 돌려준다.

    파라미터:
        client: 수명 주기를 실행한 테스트 클라이언트
    """
    app = client.app
    assert isinstance(app, FastAPI)
    settings = app.state.settings

    async def drain() -> int:
        """대기 작업이 없을 때까지 실행한다."""
        mongo = Mongo(settings.mongodb_uri, settings.mongodb_db_name)
        try:
            runner = JobRunner(mongo.db, settings, HANDLERS)
            count = 0
            while await runner.run_once():
                count += 1
            return count
        finally:
            await mongo.close()

    return asyncio.run(drain())


def upload(client: TestClient, name: str, content: bytes) -> str:
    """파일을 업로드하고 fileId를 돌려준다.

    파라미터:
        client: 테스트 클라이언트
        name: 파일명
        content: 파일 내용
    """
    res = client.post("/api/v1/files", files={"file": (name, content)})
    assert res.status_code == 201, res.text
    file_id: str = res.json()["data"]["fileId"]
    return file_id


def test_upload_process_quality_and_reprocess(db_client: TestClient) -> None:
    file_id = upload(db_client, "people.csv", CSV)

    # 처리 전에는 진단 결과가 없다
    assert db_client.get(f"/api/v1/files/{file_id}/quality").status_code == 404
    # 끝나지 않은 작업이 있으면 재처리할 수 없다
    assert db_client.post(f"/api/v1/files/{file_id}/process").status_code == 409

    assert run_worker(db_client) == 1

    detail = db_client.get(f"/api/v1/files/{file_id}").json()["data"]
    assert detail["status"] == "completed"
    job = db_client.get(f"/api/v1/jobs/{detail['lastJobId']}").json()["data"]
    assert (job["status"], job["progress"], job["error"]) == ("completed", 100, None)

    quality = db_client.get(f"/api/v1/files/{file_id}/quality").json()["data"]
    assert quality["version"] == 1
    assert quality["jobId"] == detail["lastJobId"]
    issues = {(i["type"], i["location"]) for i in quality["issues"]}
    assert ("missing_values", "이메일") in issues
    assert ("duplicates", None) in issues
    assert ("type_mismatch", "나이") not in issues  # 3개 중 1개라 string 열로 본다
    assert 0 <= quality["score"] < 100

    # 재처리하면 새 작업이 생기고 결과 버전이 올라간다
    started = db_client.post(f"/api/v1/files/{file_id}/process")
    assert started.status_code == 202
    new_job_id = started.json()["data"]["jobId"]
    assert started.json()["data"]["status"] == "queued"
    after = db_client.get(f"/api/v1/files/{file_id}").json()["data"]
    assert (after["status"], after["lastJobId"]) == ("queued", new_job_id)

    assert run_worker(db_client) == 1
    assert db_client.get(f"/api/v1/files/{file_id}/quality").json()["data"]["version"] == 2


def test_preview_table_and_text(db_client: TestClient) -> None:
    csv_id = upload(db_client, "people.csv", CSV)
    log_id = upload(db_client, "app.log", "\n".join(f"INFO {i}" for i in range(30)).encode())

    table = db_client.get(f"/api/v1/files/{csv_id}/preview").json()["data"]
    assert table["kind"] == "table"
    assert table["encoding"] == "cp949"
    assert table["columns"] == ["이름", "나이", "이메일"]
    assert table["rows"][1] == ["김철수", "모름", ""]

    text = db_client.get(f"/api/v1/files/{log_id}/preview").json()["data"]
    assert text["kind"] == "text"
    assert len(text["lines"]) == 20
    assert text["lines"][0] == "INFO 0"


def test_document_preview_and_quality(db_client: TestClient) -> None:
    docx_id = upload(db_client, "minwon.docx", make_docx(["가로등 고장", "수청동 123"]))
    pdf_id = upload(db_client, "scan.pdf", make_pdf([["Street light"], []]))

    preview = db_client.get(f"/api/v1/files/{docx_id}/preview").json()["data"]
    assert (preview["kind"], preview["format"]) == ("text", "docx")
    assert preview["lines"] == ["가로등 고장", "수청동 123"]

    assert run_worker(db_client) == 2
    for file_id in (docx_id, pdf_id):
        detail = db_client.get(f"/api/v1/files/{file_id}").json()["data"]
        assert detail["status"] == "completed"
    quality = db_client.get(f"/api/v1/files/{pdf_id}/quality").json()["data"]
    assert [i["type"] for i in quality["issues"]] == ["no_text_layer"]


def test_image_without_ocr_fails_job_and_preview(db_client: TestClient) -> None:
    # 테스트 설정은 OCR을 꺼 둔다(make_settings). 이미지는 OCR 없이는 읽을 수 없다
    file_id = upload(db_client, "photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    res = db_client.get(f"/api/v1/files/{file_id}/preview")
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "OCR_NOT_AVAILABLE"

    run_worker(db_client)
    detail = db_client.get(f"/api/v1/files/{file_id}").json()["data"]
    job = db_client.get(f"/api/v1/jobs/{detail['lastJobId']}").json()["data"]
    assert detail["status"] == "failed"
    assert job["status"] == "failed"
    assert job["stage"] == "parsing"
    assert job["error"]["code"] == "OCR_NOT_AVAILABLE"


@pytest.mark.parametrize("path", ["preview", "quality"])
def test_unknown_file_is_not_found(db_client: TestClient, path: str) -> None:
    res = db_client.get(f"/api/v1/files/68fb12a1aaaaaaaaaaaaaaaa/{path}")

    assert res.status_code == 404
