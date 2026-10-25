"""진단 → 규칙 추천 → 정제 → 버전 이력 → 다운로드 흐름. 실제 MongoDB 필요(임시 DB 자동)."""

import csv
import io
import json
from pathlib import Path

import openpyxl
from fastapi.testclient import TestClient

from tests.conftest import requires_db
from tests.test_analyze_flow import run_worker, upload

pytestmark = requires_db

CSV = (
    "id,name,age,city\n1, 홍길동 ,30,포항\n2,김철수,,포항\n3,이영희,40,\n1, 홍길동 ,30,포항\n"
).encode()


def analyzed(client: TestClient) -> str:
    """CSV를 올리고 진단까지 끝낸 뒤 fileId를 돌려준다.

    파라미터:
        client: 테스트 클라이언트
    """
    file_id = upload(client, "people.csv", CSV)
    assert run_worker(client) == 1
    return file_id


def test_suggest_clean_versions_export(db_client: TestClient, tmp_path: Path) -> None:
    file_id = analyzed(db_client)

    suggested = db_client.post(f"/api/v1/files/{file_id}/cleaning-suggestions").json()["data"]
    assert suggested["basedOnVersion"] == 1
    rules = [s["rule"] for s in suggested["suggestions"]]
    assert {"type": "trim_whitespace", "column": "name"} in rules
    assert {"type": "remove_duplicates"} in rules
    assert {"type": "fill_missing", "column": "age", "strategy": "median"} in rules

    # 추천 규칙을 그대로 적용한다
    started = db_client.post(f"/api/v1/files/{file_id}/clean", json={"rules": rules})
    assert started.status_code == 202, started.text
    job_id = started.json()["data"]["jobId"]
    assert (tmp_path / "jobs" / job_id / "params.json").is_file()

    assert run_worker(db_client) == 1
    job = db_client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert (job["type"], job["status"]) == ("clean", "completed"), job
    assert not (tmp_path / "jobs" / job_id).exists()  # 작업 파라미터는 정리된다

    versions = db_client.get(f"/api/v1/files/{file_id}/versions").json()["data"]["items"]
    assert [(v["resultType"], v["version"]) for v in versions] == [
        ("analysis", 1),
        ("cleaning", 1),
    ]
    cleaning = versions[1]
    assert cleaning["rowCount"] == 3
    assert cleaning["cleaning"]["before"]["rowCount"] == 4
    assert cleaning["cleaning"]["after"]["rowCount"] == 3
    assert cleaning["cleaning"]["after"]["score"] > cleaning["cleaning"]["before"]["score"]
    affected = {c["type"]: c["affected"] for c in cleaning["cleaning"]["changes"]}
    assert affected["remove_duplicates"] == 1
    assert "artifacts" not in cleaning["cleaning"]  # 내부 저장 위치는 숨긴다
    only_cleaning = db_client.get(
        f"/api/v1/files/{file_id}/versions", params={"type": "cleaning"}
    ).json()["data"]["items"]
    assert len(only_cleaning) == 1

    exported = db_client.get(f"/api/v1/files/{file_id}/export", params={"format": "csv"})
    assert exported.status_code == 200
    assert "people_cleaned_v1.csv" in exported.headers["content-disposition"]
    table = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
    # 추천 순서: 공백 제거 → age 중앙값(30, 40, 30 → 30) → city 최빈값 → 중복 제거
    assert table == [
        ["id", "name", "age", "city"],
        ["1", "홍길동", "30", "포항"],
        ["2", "김철수", "30", "포항"],
        ["3", "이영희", "40", "포항"],
    ]

    as_json = db_client.get(f"/api/v1/files/{file_id}/export", params={"format": "json"})
    assert json.loads(as_json.content)[1] == {
        "id": "2",
        "name": "김철수",
        "age": "30",
        "city": "포항",
    }
    as_xlsx = db_client.get(f"/api/v1/files/{file_id}/export", params={"format": "xlsx"})
    sheet = openpyxl.load_workbook(io.BytesIO(as_xlsx.content)).worksheets[0]
    assert next(sheet.iter_rows(values_only=True)) == ("id", "name", "age", "city")
    assert not list((tmp_path / "tmp").iterdir())  # 변환용 임시 파일은 지워진다

    missing = db_client.get(
        f"/api/v1/files/{file_id}/export", params={"format": "csv", "version": 2}
    )
    assert missing.status_code == 404

    # 파일을 지우면 산출물도 지워진다
    assert db_client.delete(f"/api/v1/files/{file_id}").status_code == 200
    assert not (tmp_path / "results" / file_id).exists()


def test_clean_validation_errors(db_client: TestClient) -> None:
    file_id = analyzed(db_client)
    url = f"/api/v1/files/{file_id}/clean"

    unknown = db_client.post(url, json={"rules": [{"type": "trim_whitespace", "column": "x"}]})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "UNKNOWN_COLUMN"

    bad_rule = db_client.post(url, json={"rules": [{"type": "run_python", "code": "..."}]})
    assert bad_rule.status_code == 422
    assert bad_rule.json()["error"]["code"] == "VALIDATION_ERROR"

    ok_rule = {"rules": [{"type": "remove_duplicates"}]}
    assert db_client.post(url, json=ok_rule).status_code == 202
    busy = db_client.post(url, json=ok_rule)
    assert busy.status_code == 409


def test_clean_failure_is_recorded(db_client: TestClient) -> None:
    file_id = analyzed(db_client)

    started = db_client.post(
        f"/api/v1/files/{file_id}/clean",
        json={"rules": [{"type": "fill_missing", "column": "city", "strategy": "median"}]},
    ).json()["data"]
    run_worker(db_client)

    job = db_client.get(f"/api/v1/jobs/{started['jobId']}").json()["data"]
    assert job["status"] == "failed"
    assert job["stage"] == "cleaning"
    assert job["error"]["code"] == "NOT_NUMERIC_COLUMN"
    export = db_client.get(f"/api/v1/files/{file_id}/export", params={"format": "csv"})
    assert export.status_code == 404


def test_cleaning_not_supported_for_text(db_client: TestClient) -> None:
    file_id = upload(db_client, "app.log", b"INFO start\n")
    run_worker(db_client)

    res = db_client.post(f"/api/v1/files/{file_id}/cleaning-suggestions")
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "CLEANING_NOT_SUPPORTED"
    res = db_client.post(
        f"/api/v1/files/{file_id}/clean", json={"rules": [{"type": "remove_duplicates"}]}
    )
    assert res.status_code == 422


def test_suggestions_require_analysis(db_client: TestClient) -> None:
    file_id = upload(db_client, "people.csv", CSV)

    res = db_client.post(f"/api/v1/files/{file_id}/cleaning-suggestions")

    assert res.status_code == 404
    assert res.json()["error"]["code"] == "RESULT_NOT_FOUND"
