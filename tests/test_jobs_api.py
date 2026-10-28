import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import Mongo, get_mongo
from tests.conftest import requires_db


@pytest.mark.parametrize("job_id", ["not-an-id", "123"])
def test_invalid_job_id_is_not_found(app: FastAPI, job_id: str) -> None:
    app.dependency_overrides[get_mongo] = lambda: Mongo(
        "mongodb://localhost:1/?serverSelectionTimeoutMS=100", "offline"
    )
    client = TestClient(app, raise_server_exceptions=False)

    res = client.get(f"/api/v1/jobs/{job_id}")

    assert res.status_code == 404
    assert res.json()["error"]["code"] == "JOB_NOT_FOUND"


@requires_db
def test_get_job_created_by_upload(db_client: TestClient) -> None:
    uploaded = db_client.post(
        "/api/v1/files", files={"file": ("a.csv", "이름\n홍길동\n".encode())}
    ).json()["data"]
    detail = db_client.get(f"/api/v1/files/{uploaded['fileId']}").json()["data"]

    res = db_client.get(f"/api/v1/jobs/{detail['lastJobId']}")

    assert res.status_code == 200
    job = res.json()["data"]
    assert job["jobId"] == detail["lastJobId"]
    assert job["fileId"] == uploaded["fileId"]
    assert (job["type"], job["status"], job["progress"], job["stage"]) == (
        "analyze",
        "queued",
        0,
        "queued",
    )
    assert job["error"] is None
    assert job["startedAt"] is None
    assert job["createdAt"] is not None


@requires_db
def test_unknown_job_is_not_found(db_client: TestClient) -> None:
    res = db_client.get("/api/v1/jobs/68fb12a1aaaaaaaaaaaaaaaa")

    assert res.status_code == 404
