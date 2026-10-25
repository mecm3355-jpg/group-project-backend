from bson import ObjectId

from app.schemas.file import FileDocument, FileStorage, ProcessingCapabilities
from app.schemas.job import JobDocument
from app.schemas.result import QualityIssue, QualityResult, ResultDocument


def _file() -> FileDocument:
    """테스트용 files 문서를 만든다.

    파라미터: 없음
    """
    return FileDocument(
        original_name="sales_2026.csv",
        extension="csv",
        mime_type="text/csv",
        category="tabular",
        size_bytes=245760,
        storage=FileStorage(provider="local", key="uploads/2026/10/abc123"),
        status="queued",
        processing_capabilities=ProcessingCapabilities(
            preview=True, quality_check=True, cleaning=True, statistics=True, ai_summary=True
        ),
    )


def test_file_document_uses_schema_field_names() -> None:
    doc = _file().to_document()

    # docs/db-schema/schema.md files 표의 필드와 정확히 같아야 한다
    assert set(doc) == {
        "_id",
        "originalName",
        "extension",
        "mimeType",
        "category",
        "sizeBytes",
        "storage",
        "status",
        "processingCapabilities",
        "createdAt",
        "updatedAt",
        "lastJobId",
    }
    assert isinstance(doc["_id"], ObjectId)
    assert doc["processingCapabilities"] == {
        "preview": True,
        "qualityCheck": True,
        "cleaning": True,
        "statistics": True,
        "aiSummary": True,
    }
    assert doc["lastJobId"] is None


def test_file_document_round_trip() -> None:
    original = _file()

    restored = FileDocument.model_validate(original.to_document())

    assert restored == original


def test_job_document_uses_table_field_names() -> None:
    doc = JobDocument(field=ObjectId(), type="analyze").to_document()

    # 표 기준: field, createAt (예시의 fileId, createdAt 아님 — PRD 7.7)
    assert set(doc) == {
        "_id",
        "field",
        "type",
        "status",
        "progress",
        "stage",
        "error",
        "startedAt",
        "completedAt",
        "createAt",
    }
    assert doc["status"] == "queued"
    assert doc["progress"] == 0
    assert doc["error"] is None


def test_result_document_keeps_all_result_objects() -> None:
    doc = ResultDocument(
        field=ObjectId(),
        job_id=ObjectId(),
        result_type="analysis",
        version=1,
        quality=QualityResult(
            score=92,
            issues=[
                QualityIssue(
                    type="duplicates",
                    severity="low",
                    count=3,
                    message="중복 행 3개가 발견되었습니다.",
                )
            ],
        ),
        analysis={"format": "csv", "rowCount": 1000},
    ).to_document()

    assert set(doc) == {
        "_id",
        "field",
        "jobId",
        "resultType",
        "version",
        "quality",
        "analysis",
        "cleaning",
        "ai",
        "createAt",
    }
    assert doc["quality"] == {
        "score": 92,
        "issues": [
            {
                "type": "duplicates",
                "severity": "low",
                "count": 3,
                "location": None,
                "message": "중복 행 3개가 발견되었습니다.",
            }
        ],
    }
    # 해당 없는 결과 객체는 빈 객체로 저장
    assert doc["cleaning"] == {}
    assert doc["ai"] == {}
