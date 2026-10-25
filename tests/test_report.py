"""AI 분석 리포트. 실제 LLM 대신 가짜 LLM으로 흐름을 확인한다(실제 MongoDB 필요, 임시 DB 자동)."""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo import MongoClient

from app.core.config import Settings
from app.core.database import Document, Mongo
from app.main import create_app
from app.schemas.file import FileDocument, FileStorage, ProcessingCapabilities
from app.schemas.job import REPORT_JOB_TYPE
from app.schemas.result import QualityIssue, QualityResult, ResultDocument
from app.services.job_handlers import HANDLERS
from app.services.job_runner import JobRunner
from app.services.llm import LlmClient
from app.services.report_handler import build_report_input, make_report_handler
from tests.conftest import TEST_URI, make_settings, requires_db
from tests.test_analyze_flow import CSV, run_worker, upload
from tests.test_llm import FakeBackend

REPORT_JSON = json.dumps(
    {
        "summary": "인적 정보 표로 보이며 일부 결측과 중복이 있다.",
        "keyFindings": ["행 3개", "이메일 결측 1건"],
        "qualityIssues": [
            {
                "issue": "이메일 결측",
                "likelyCause": "입력 누락으로 보인다",
                "impact": "연락 가능 대상 집계가 줄어든다",
                "severity": "medium",
            }
        ],
        "cleaningRecommendations": [
            {"action": "중복 행 제거", "reason": "같은 사람이 두 번 집계됨", "priority": "high"}
        ],
        "analysisSuggestions": ["나이 분포 확인"],
        "caveats": ["통계 요약만 보고 작성함"],
    },
    ensure_ascii=False,
)


def file_doc() -> FileDocument:
    """리포트 입력 테스트용 파일 문서(파일명에 개인정보가 있는 경우).

    파라미터: 없음
    """
    return FileDocument(
        original_name="홍길동_010-1234-5678.csv",
        extension="csv",
        mime_type="text/csv",
        category="tabular",
        size_bytes=100,
        storage=FileStorage(provider="local", key="uploads/x"),
        status="completed",
        processing_capabilities=ProcessingCapabilities(
            preview=True, quality_check=True, cleaning=True, statistics=True, ai_summary=True
        ),
    )


def test_report_input_excludes_file_name_and_masks_pii() -> None:
    file = file_doc()
    analysis = ResultDocument(
        field=file.id,
        job_id=ObjectId(),
        result_type="analysis",
        version=2,
        quality=QualityResult(
            score=80,
            issues=[
                QualityIssue(
                    type="missing_values",
                    severity="low",
                    count=1,
                    location="연락처",
                    message="연락처 필드에 결측치가 있습니다.",
                )
            ],
        ),
        analysis={"format": "csv", "rowCount": 3},
    )
    cleaning = ResultDocument(
        field=file.id,
        job_id=ObjectId(),
        result_type="cleaning",
        version=1,
        cleaning={
            "rules": [
                {
                    "type": "fill_missing",
                    "column": "연락처",
                    "strategy": "constant",
                    "value": "010-1234-5678",
                }
            ]
        },
    )

    text = build_report_input(file, analysis, cleaning)

    assert "홍길동" not in text  # 파일명은 보내지 않는다
    assert "010-1234-5678" not in text  # 요약 안의 개인정보는 마스킹한다
    assert "[전화]" in text
    payload = json.loads(text)
    assert payload["latestAnalysis"]["version"] == 2
    assert payload["latestAnalysis"]["quality"]["issues"][0]["location"] == "연락처"
    assert payload["latestCleaning"]["version"] == 1


def llm_settings(tmp_path: Path, db_name: str) -> Settings:
    """리포트용 LLM이 설정된 것처럼 보이는 테스트 설정(실제 호출은 가짜 LLM이 한다).

    파라미터:
        tmp_path: 저장소 임시 폴더
        db_name: 테스트 DB 이름
    """
    assert TEST_URI is not None
    return make_settings(
        tmp_path,
        mongodb_uri=TEST_URI,
        mongodb_db_name=db_name,
        azure_openai_endpoint="https://example.openai.azure.com/openai/v1",
        azure_openai_api_key=SecretStr("test-key"),
        azure_openai_deployment_deep="gpt-5.6-sol-1",
    )


@pytest.fixture
def llm_client(tmp_path: Path) -> Iterator[TestClient]:
    """LLM 설정이 있는 앱의 테스트 클라이언트. 임시 DB를 만들고 끝나면 지운다.

    파라미터:
        tmp_path: pytest 임시 폴더
    """
    db_name = f"datalens_test_{ObjectId()}"
    try:
        with TestClient(create_app(llm_settings(tmp_path, db_name))) as client:
            yield client
    finally:
        assert TEST_URI is not None
        cleanup: MongoClient[Document]
        with MongoClient(TEST_URI) as cleanup:
            cleanup.drop_database(db_name)


def run_report_worker(client: TestClient, backend: FakeBackend) -> int:
    """리포트 처리기에 가짜 LLM을 넣은 워커로 대기 작업을 모두 처리한다.

    파라미터:
        client: 수명 주기를 실행한 테스트 클라이언트
        backend: 가짜 LLM 백엔드
    """
    app = client.app
    assert isinstance(app, FastAPI)
    settings: Settings = app.state.settings
    handlers = {
        **HANDLERS,
        REPORT_JOB_TYPE: make_report_handler(lambda s: LlmClient(backend, None)),
    }

    async def drain() -> int:
        """대기 작업이 없을 때까지 실행한다."""
        mongo = Mongo(settings.mongodb_uri, settings.mongodb_db_name)
        try:
            runner = JobRunner(mongo.db, settings, handlers)
            count = 0
            while await runner.run_once():
                count += 1
            return count
        finally:
            await mongo.close()

    return asyncio.run(drain())


@requires_db
def test_report_flow(llm_client: TestClient) -> None:
    file_id = upload(llm_client, "people.csv", CSV)

    # 진단 전에는 리포트를 만들 수 없다
    early = llm_client.post(f"/api/v1/files/{file_id}/report")
    assert early.status_code == 404
    assert early.json()["error"]["code"] == "RESULT_NOT_FOUND"

    backend = FakeBackend([REPORT_JSON])
    assert run_report_worker(llm_client, backend) == 1  # analyze

    started = llm_client.post(f"/api/v1/files/{file_id}/report")
    assert started.status_code == 202, started.text
    job_id = started.json()["data"]["jobId"]
    assert run_report_worker(llm_client, backend) == 1  # report

    job = llm_client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert (job["type"], job["status"]) == ("report", "completed"), job
    # LLM에는 원본이 아니라 진단 요약만 갔다
    [(system, sent)] = backend.sent
    assert "홍길동" not in sent and "a@x.com" not in sent
    assert '"rowCount"' in sent

    items = llm_client.get(f"/api/v1/files/{file_id}/versions", params={"type": "report"}).json()[
        "data"
    ]["items"]
    assert len(items) == 1
    ai = items[0]["ai"]
    assert ai["report"]["summary"].startswith("인적 정보 표")
    assert ai["report"]["cleaningRecommendations"][0]["priority"] == "high"
    assert ai["basedOn"] == {"analysisVersion": 1, "cleaningVersion": None}
    assert ai["promptVersion"] == "file-report@1"
    assert ai["usage"]["calls"] == 1
    assert ai["usage"]["deployment"] == "fake-deployment"


@requires_db
def test_report_requires_llm_configuration(db_client: TestClient) -> None:
    file_id = upload(db_client, "people.csv", CSV)
    run_worker(db_client)

    res = db_client.post(f"/api/v1/files/{file_id}/report")

    assert res.status_code == 503
    assert res.json()["error"]["code"] == "LLM_NOT_CONFIGURED"


@requires_db
def test_report_llm_failure_is_recorded(llm_client: TestClient) -> None:
    file_id = upload(llm_client, "people.csv", CSV)
    backend = FakeBackend(['{"summary": "필드 부족"}'])
    run_report_worker(llm_client, backend)

    job_id = llm_client.post(f"/api/v1/files/{file_id}/report").json()["data"]["jobId"]
    run_report_worker(llm_client, backend)

    job = llm_client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["stage"] == "ai_summary"
    assert job["error"]["code"] == "LLM_INVALID_RESPONSE"
