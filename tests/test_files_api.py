from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.collection import Collection

from app.core.database import Document, Mongo, get_mongo
from app.main import create_app
from tests.conftest import TEST_URI, make_settings, requires_db

CSV = "이름,나이\n홍길동,30\n".encode()
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def upload(client: TestClient, name: str, content: bytes) -> httpx.Response:
    """파일 하나를 multipart/form-data(필드명 file)로 업로드한다.

    파라미터:
        client: 테스트 클라이언트
        name: 파일명
        content: 파일 내용
    """
    res: httpx.Response = client.post("/api/v1/files", files={"file": (name, content)})
    return res


def stored_files(root: Path) -> list[Path]:
    """저장소에 남은 파일 목록(임시 파일 포함)을 돌려준다.

    파라미터:
        root: 저장소 루트
    """
    return [p for p in root.rglob("*") if p.is_file()]


def offline_mongo() -> Mongo:
    """연결할 수 없는 주소의 Mongo. DB에 접근하는 순간 실패하므로 DB 전 단계 검증에 쓴다.

    파라미터: 없음
    """
    return Mongo("mongodb://localhost:1/?serverSelectionTimeoutMS=100", "offline")


@pytest.fixture
def offline_client(app: FastAPI) -> TestClient:
    """offline_mongo를 주입한 클라이언트.

    파라미터:
        app: 테스트용 앱
    """
    app.dependency_overrides[get_mongo] = offline_mongo
    return TestClient(app, raise_server_exceptions=False)


# ---- DB 없이 확인하는 검증 실패 경로 ----
# DB 작업 전에 거부되고 저장소에 아무것도 남지 않아야 한다


@pytest.mark.parametrize(
    ("name", "content", "status_code", "code"),
    [
        ("virus.exe", b"MZ\x90\x00", 415, "UNSUPPORTED_FILE_TYPE"),
        ("report.csv", b"%PDF-1.7\n...", 415, "FILE_TYPE_MISMATCH"),
        ("empty.csv", b"", 400, "EMPTY_FILE"),
        ("../../", CSV, 400, "INVALID_FILE_NAME"),
    ],
)
def test_upload_rejects_invalid_file(
    offline_client: TestClient,
    tmp_path: Path,
    name: str,
    content: bytes,
    status_code: int,
    code: str,
) -> None:
    res = upload(offline_client, name, content)

    assert res.status_code == status_code
    body = res.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == code
    assert stored_files(tmp_path) == []


def test_upload_rejects_too_large_file(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, max_upload_bytes=10))
    app.dependency_overrides[get_mongo] = offline_mongo
    client = TestClient(app, raise_server_exceptions=False)

    res = upload(client, "big.csv", b"a,b\n" * 10)

    assert res.status_code == 413
    assert res.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert stored_files(tmp_path) == []


def test_upload_requires_file_field(offline_client: TestClient) -> None:
    res = offline_client.post("/api/v1/files", data={"other": "x"})

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("file_id", ["not-an-id", "123"])
def test_invalid_file_id_is_not_found(offline_client: TestClient, file_id: str) -> None:
    res = offline_client.get(f"/api/v1/files/{file_id}")

    assert res.status_code == 404
    assert res.json()["error"]["code"] == "FILE_NOT_FOUND"


@pytest.mark.parametrize("params", [{"size": 1000}, {"page": 0}, {"status": "unknown"}])
def test_list_rejects_bad_query(offline_client: TestClient, params: dict[str, object]) -> None:
    res = offline_client.get("/api/v1/files", params=params)

    assert res.status_code == 422


# ---- 실제 DB 통합 테스트 ----


@requires_db
def test_upload_get_list_delete(db_client: TestClient, tmp_path: Path) -> None:
    res = upload(db_client, "sales_2026.csv", CSV)

    # 명세서 "파일 등록 응답 예시"의 필드
    assert res.status_code == 201
    data = res.json()["data"]
    assert set(data) == {"fileId", "originalName", "fileType", "status", "message"}
    assert data["originalName"] == "sales_2026.csv"
    assert data["fileType"] == "csv"
    assert data["status"] == "queued"
    file_id = data["fileId"]

    # 원본은 저장소에 그대로 저장되고 임시 파일은 남지 않는다
    on_disk = stored_files(tmp_path)
    assert len(on_disk) == 1
    assert on_disk[0].read_bytes() == CSV
    assert on_disk[0].name == file_id

    detail = db_client.get(f"/api/v1/files/{file_id}").json()["data"]
    assert detail["category"] == "tabular"
    assert detail["mimeType"] == "text/csv"
    assert detail["sizeBytes"] == len(CSV)
    assert detail["processingCapabilities"]["cleaning"] is True
    assert detail["lastJobId"] is not None
    assert "storage" not in detail

    assert upload(db_client, "photo.png", PNG).status_code == 201
    found = db_client.get("/api/v1/files", params={"q": "SALES"}).json()["data"]
    assert found["total"] == 1
    assert [item["fileId"] for item in found["items"]] == [file_id]
    first_page = db_client.get("/api/v1/files", params={"size": 1}).json()["data"]
    assert (first_page["total"], len(first_page["items"])) == (2, 1)
    queued = db_client.get("/api/v1/files", params={"status": "queued"}).json()["data"]
    assert queued["total"] == 2

    assert db_client.delete(f"/api/v1/files/{file_id}").status_code == 200
    assert db_client.get(f"/api/v1/files/{file_id}").status_code == 404
    assert db_client.delete(f"/api/v1/files/{file_id}").status_code == 404
    assert len(stored_files(tmp_path)) == 1  # png만 남는다


@requires_db
def test_upload_creates_queued_job_and_delete_removes_it(db_client: TestClient) -> None:
    data = upload(db_client, "a.csv", CSV).json()["data"]
    detail = db_client.get(f"/api/v1/files/{data['fileId']}").json()["data"]
    app = db_client.app
    assert isinstance(app, FastAPI)
    assert TEST_URI is not None

    raw: MongoClient[Document]
    with MongoClient(TEST_URI) as raw:
        db_name: str = app.state.settings.mongodb_db_name
        jobs: Collection[Document] = raw[db_name]["processing_jobs"]
        job = jobs.find_one()
        assert job is not None
        assert str(job["_id"]) == detail["lastJobId"]
        assert str(job["field"]) == data["fileId"]
        assert (job["type"], job["status"], job["progress"]) == ("analyze", "queued", 0)

        db_client.delete(f"/api/v1/files/{data['fileId']}")
        assert jobs.count_documents({}) == 0
