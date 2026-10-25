import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import AppError
from app.processors.tabular import open_table
from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.repositories.results import ResultRepository
from app.schemas.cleaning import CleanRequest, CleanRule
from app.schemas.file import FileDocument
from app.schemas.job import CLEAN_JOB_TYPE, JobDocument
from app.schemas.result import ResultDocument, ResultType
from app.services.artifacts import job_params_key
from app.services.cleaning import check_columns
from app.services.export import MEDIA_TYPES, ExportFormat, convert
from app.services.file_service import parse_file_id
from app.services.storage import LocalStorage
from app.services.suggestions import Suggestion, suggest_rules


@dataclass(frozen=True)
class ExportFile:
    """내려보낼 파일. temporary면 응답을 보낸 뒤 지운다."""

    path: Path
    filename: str
    media_type: str
    temporary: bool


def check_rule_columns(path: Path, extension: str, rules: list[CleanRule]) -> None:
    """원본 머리글을 읽어 규칙의 열이 모두 있는지 확인한다. 스레드에서 실행한다.

    파라미터:
        path: 원본 파일 경로
        extension: 원본 확장자
        rules: 확인할 규칙
    """
    with open_table(path, extension) as table:
        check_columns(table.columns, rules)


class CleaningService:
    """정제 규칙 추천·정제 작업 생성·버전 이력·결과 다운로드 업무 로직."""

    def __init__(
        self,
        files: FileRepository,
        jobs: JobRepository,
        results: ResultRepository,
        storage: LocalStorage,
    ) -> None:
        """서비스를 만든다.

        파라미터:
            files: files 저장소
            jobs: processing_jobs 저장소
            results: processing_results 저장소
            storage: 원본·산출물 저장소
        """
        self._files = files
        self._jobs = jobs
        self._results = results
        self._storage = storage

    async def _get_file(self, file_id: str) -> FileDocument:
        """파일을 찾는다. 없으면 404.

        파라미터:
            file_id: 파일 ID 문자열
        """
        doc = await self._files.get(parse_file_id(file_id))
        if doc is None:
            raise AppError("FILE_NOT_FOUND", "파일을 찾을 수 없습니다.", 404)
        return doc

    @staticmethod
    def _require_cleaning(doc: FileDocument) -> None:
        """정제를 지원하는 형식(표)인지 확인한다.

        파라미터:
            doc: 파일 문서
        """
        if not doc.processing_capabilities.cleaning:
            raise AppError(
                "CLEANING_NOT_SUPPORTED", f"{doc.extension} 파일은 정제를 지원하지 않습니다.", 422
            )

    async def suggest(self, file_id: str) -> tuple[ResultDocument, list[Suggestion]]:
        """최신 품질 진단을 근거로 정제 규칙을 추천한다. 진단 결과가 없으면 404.

        파라미터:
            file_id: 파일 ID 문자열
        """
        doc = await self._get_file(file_id)
        self._require_cleaning(doc)
        analysis = await self._results.get_latest(doc.id, "analysis")
        if analysis is None:
            raise AppError(
                "RESULT_NOT_FOUND", "진단 결과가 없습니다. 먼저 추출·진단을 실행하세요.", 404
            )
        return analysis, suggest_rules(analysis)

    async def start_cleaning(self, file_id: str, request: CleanRequest) -> JobDocument:
        """정제 작업을 만든다. 규칙의 열이 원본에 없거나 끝나지 않은 작업이 있으면 거부한다.

        규칙은 작업 파라미터 파일로 저장한 뒤 작업을 등록한다(워커가 먼저 가져가도 읽을 수 있게).

        파라미터:
            file_id: 파일 ID 문자열
            request: 승인한 정제 규칙
        """
        doc = await self._get_file(file_id)
        self._require_cleaning(doc)
        if await self._jobs.has_active(doc.id):
            raise AppError("JOB_ALREADY_RUNNING", "이미 처리 중인 작업이 있습니다.", 409)
        source = self._storage.path(doc.storage.key)
        if not source.is_file():
            raise AppError("SOURCE_MISSING", "원본 파일이 저장소에 없습니다.", 409)
        await asyncio.to_thread(check_rule_columns, source, doc.extension, request.rules)

        job = JobDocument(field=doc.id, type=CLEAN_JOB_TYPE)
        await asyncio.to_thread(
            self._storage.write_text, job_params_key(job.id), request.model_dump_json()
        )
        await self._jobs.insert(job)
        await self._files.update_status(doc.id, "queued", last_job_id=job.id)
        return job

    async def versions(self, file_id: str, result_type: ResultType | None) -> list[ResultDocument]:
        """파일의 결과 버전 이력을 돌려준다.

        파라미터:
            file_id: 파일 ID 문자열
            result_type: 이 종류만(analysis, cleaning, report). None이면 전체
        """
        doc = await self._get_file(file_id)
        return await self._results.list_by_file(doc.id, result_type)

    async def export(self, file_id: str, fmt: ExportFormat, version: int | None) -> ExportFile:
        """정제 결과 파일을 원하는 형식으로 준비한다. version이 없으면 최신 정제 결과.

        파라미터:
            file_id: 파일 ID 문자열
            fmt: csv, json, xlsx 중 하나
            version: 정제 결과 버전. None이면 최신
        """
        doc = await self._get_file(file_id)
        history = await self._results.list_by_file(doc.id, "cleaning")
        result = next((r for r in history if version is None or r.version == version), None)
        if result is None:
            raise AppError("RESULT_NOT_FOUND", "다운로드할 정제 결과가 없습니다.", 404)

        artifacts = result.cleaning.get("artifacts")
        key = None
        if isinstance(artifacts, list) and artifacts and isinstance(artifacts[0], dict):
            storage_key = artifacts[0].get("storageKey")
            key = storage_key if isinstance(storage_key, str) else None
        source = self._storage.path(key) if key else None
        if source is None or not source.is_file():
            raise AppError("ARTIFACT_MISSING", "정제 결과 파일이 저장소에 없습니다.", 409)

        filename = f"{Path(doc.original_name).stem}_cleaned_v{result.version}.{fmt}"
        if fmt == "csv":
            return ExportFile(source, filename, MEDIA_TYPES[fmt], temporary=False)
        target = self._storage.new_temp_path()
        await asyncio.to_thread(convert, source, target, fmt)
        return ExportFile(target, filename, MEDIA_TYPES[fmt], temporary=True)
