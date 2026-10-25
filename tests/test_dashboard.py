from fastapi.testclient import TestClient

from app.schemas.result import QualityIssue, QualityResult, Severity
from app.services.dashboard_service import summarize_quality, with_zeros
from tests.conftest import requires_db
from tests.test_analyze_flow import CSV, run_worker, upload

LOG = b"INFO start\n\nERROR boom\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def issue(kind: str, severity: Severity) -> QualityIssue:
    """테스트용 품질 문제 항목을 만든다.

    파라미터:
        kind: 문제 유형
        severity: 심각도
    """
    return QualityIssue(type=kind, severity=severity, count=1, message=kind)


def test_with_zeros_keeps_unknown_values() -> None:
    assert with_zeros({"failed": 2, "legacy": 1}, ("queued", "failed")) == {
        "queued": 0,
        "failed": 2,
        "legacy": 1,
    }


def test_summarize_quality() -> None:
    summary = summarize_quality(
        [
            QualityResult(score=90, issues=[issue("missing_values", "medium")]),
            QualityResult(
                score=71, issues=[issue("missing_values", "high"), issue("duplicates", "low")]
            ),
            QualityResult(issues=[issue("empty_lines", "low")]),
        ]
    )

    assert summary.analyzed_file_count == 3
    assert summary.scored_file_count == 2
    assert summary.average_score == 80.5
    assert summary.issues_by_type == {"missing_values": 2, "duplicates": 1, "empty_lines": 1}
    assert summary.issues_by_severity == {"low": 2, "medium": 1, "high": 1}


def test_summarize_quality_without_results() -> None:
    summary = summarize_quality([])

    assert (summary.analyzed_file_count, summary.average_score) == (0, None)
    assert summary.issues_by_severity == {"low": 0, "medium": 0, "high": 0}


@requires_db
def test_dashboard_summary(db_client: TestClient) -> None:
    empty = db_client.get("/api/v1/dashboard/summary").json()["data"]
    assert empty["files"]["total"] == 0
    assert empty["files"]["byCategory"]["tabular"] == 0
    assert empty["quality"]["averageScore"] is None

    csv_id = upload(db_client, "people.csv", CSV)
    upload(db_client, "app.log", b"INFO start\n\nERROR boom\n")
    upload(db_client, "photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    run_worker(db_client)
    # 재처리해도 파일마다 최신 결과 하나만 센다
    db_client.post(f"/api/v1/files/{csv_id}/process")
    run_worker(db_client)

    res = db_client.get("/api/v1/dashboard/summary")

    assert res.status_code == 200
    data = res.json()["data"]
    files = data["files"]
    assert files["total"] == 3
    assert files["totalSizeBytes"] == len(CSV) + len(LOG) + len(PNG)
    assert files["byCategory"] == {
        "tabular": 1,
        "text": 1,
        "image": 1,
        "document": 0,
        "other": 0,
    }
    assert files["byStatus"] == {"queued": 0, "processing": 0, "completed": 2, "failed": 1}
    jobs = data["jobs"]
    assert jobs["total"] == 4
    assert jobs["byType"] == {"analyze": 4}
    assert (jobs["byStatus"]["completed"], jobs["byStatus"]["failed"]) == (3, 1)
    quality = data["quality"]
    assert (quality["analyzedFileCount"], quality["scoredFileCount"]) == (2, 1)
    assert quality["issuesByType"]["empty_lines"] == 1
    assert quality["issuesByType"]["duplicates"] == 1
