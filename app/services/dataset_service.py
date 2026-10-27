import asyncio
import logging
from pathlib import Path

from bson import ObjectId
from pydantic import JsonValue

from app.core.config import Settings
from app.core.errors import AppError
from app.processors.tabular import open_table
from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.repositories.results import ResultRepository
from app.schemas.dataset import MappingSuggestion, MergeRequest
from app.schemas.file import FileDocument, FileStorage
from app.schemas.job import MERGE_JOB_TYPE, JobDocument
from app.schemas.mongo import utc_now
from app.services.artifacts import job_params_key
from app.services.file_service import parse_file_id
from app.services.file_validation import CAPABILITIES
from app.services.llm import create_llm_client, llm_configured
from app.services.mapping import suggest_by_ai, suggest_by_rules
from app.services.profiling import without_raw_values
from app.services.storage import LocalStorage

logger = logging.getLogger(__name__)


def read_columns(path: Path, extension: str) -> list[str]:
    """원본 파일의 머리글(정리된 열 이름)만 읽는다. 스레드에서 실행한다.

    파라미터:
        path: 원본 파일 경로
        extension: 원본 확장자
    """
    with open_table(path, extension) as table:
        return list(table.columns)


class DatasetService:
    """공통 필드 매핑 제안과 통합 데이터셋 작업 생성 업무 로직."""

    def __init__(
        self,
        files: FileRepository,
        jobs: JobRepository,
        results: ResultRepository,
        storage: LocalStorage,
        settings: Settings,
    ) -> None:
        """서비스를 만든다.

        파라미터:
            files: files 저장소
            jobs: processing_jobs 저장소
            results: processing_results 저장소
            storage: 원본 저장소
            settings: 앱 설정(LLM 연결 정보)
        """
        self._files = files
        self._jobs = jobs
        self._results = results
        self._storage = storage
        self._settings = settings

    async def _tabular_file(self, file_id: str) -> FileDocument:
        """표 형태 원본 파일을 찾는다. 없으면 404, 표가 아니면 422.

        파라미터:
            file_id: 파일 ID 문자열
        """
        doc = await self._files.get(parse_file_id(file_id))
        if doc is None:
            raise AppError("FILE_NOT_FOUND", f"파일을 찾을 수 없습니다: {file_id}", 404)
        if doc.category != "tabular":
            raise AppError(
                "NOT_TABULAR",
                f"{doc.original_name}은 표 형태 파일이 아니어서 합칠 수 없습니다.",
                422,
            )
        return doc

    async def suggest_mapping(self, file_id: str) -> MappingSuggestion:
        """원본 열 → 공통 필드 매핑을 제안한다. 진단 결과(열 정보)가 있어야 한다.

        LLM(fast) 연결 정보가 있으면 AI로, 없거나 AI 호출이 실패하면 열 이름 규칙으로 제안한다.

        파라미터:
            file_id: 파일 ID 문자열
        """
        doc = await self._tabular_file(file_id)
        analysis = await self._results.get_latest(doc.id, "analysis")
        if analysis is None:
            raise AppError(
                "RESULT_NOT_FOUND", "진단 결과가 없습니다. 먼저 추출·진단을 실행하세요.", 404
            )
        column_info = analysis.analysis.get("columns")
        info: list[JsonValue] = column_info if isinstance(column_info, list) else []
        columns = [
            str(c["name"]) for c in info if isinstance(c, dict) and isinstance(c.get("name"), str)
        ]
        # 열별 값 빈도(topValues)는 원본 값이라 LLM에 보내지 않는다
        stats = without_raw_values(analysis.analysis).get("statistics")
        statistics: dict[str, JsonValue] = stats if isinstance(stats, dict) else {}

        if llm_configured(self._settings, "fast"):
            try:
                llm = create_llm_client(self._settings, "fast")
                mapping, constants, notes = await suggest_by_ai(
                    llm, columns, info, statistics, doc.original_name
                )
                return MappingSuggestion(
                    file_id=file_id,
                    method="ai",
                    columns=columns,
                    mapping=mapping,
                    constants=constants,
                    notes=notes,
                )
            except AppError as exc:
                logger.warning("AI 매핑 제안 실패, 규칙으로 대신함: %s", exc.code)
                fallback_note = f"AI 제안에 실패해({exc.code}) 열 이름 규칙으로 제안했습니다."
        else:
            fallback_note = "AI 연결 정보가 없어 열 이름 규칙으로 제안했습니다."

        mapping, constants, notes = suggest_by_rules(columns, statistics, doc.original_name)
        return MappingSuggestion(
            file_id=file_id,
            method="rule",
            columns=columns,
            mapping=mapping,
            constants=constants,
            notes=[fallback_note, *notes],
        )

    async def start_merge(self, request: MergeRequest) -> tuple[FileDocument, JobDocument]:
        """통합 파일 문서(queued)와 통합 작업을 만든다. 실제 합치기는 워커가 한다.

        같은 파일을 두 번 넣거나, 매핑이 비었거나, 매핑한 열이 원본에 없으면 거부한다.

        파라미터:
            request: 합칠 파일과 파일별 매핑
        """
        seen: set[str] = set()
        for item in request.sources:
            if item.file_id in seen:
                raise AppError(
                    "DUPLICATE_SOURCE", f"같은 파일을 두 번 넣었습니다: {item.file_id}", 422
                )
            seen.add(item.file_id)
            doc = await self._tabular_file(item.file_id)
            mapped = item.mapping.mapped()
            if not mapped:
                raise AppError("EMPTY_MAPPING", f"{doc.original_name}의 매핑이 비어 있습니다.", 422)
            path = self._storage.path(doc.storage.key)
            if not path.is_file():
                raise AppError("SOURCE_MISSING", f"{doc.original_name} 원본이 없습니다.", 409)
            columns = await asyncio.to_thread(read_columns, path, doc.extension)
            unknown = [c for c in mapped.values() if c not in columns]
            if unknown:
                raise AppError(
                    "UNKNOWN_COLUMN",
                    f"{doc.original_name}에 {', '.join(unknown)} 열이 없습니다.",
                    422,
                )

        now = utc_now()
        file_id = ObjectId()
        name = request.name or f"통합_데이터셋_{len(request.sources)}개_{now:%Y%m%d_%H%M}"
        if not name.lower().endswith(".csv"):
            name += ".csv"
        job = JobDocument(field=file_id, type=MERGE_JOB_TYPE)
        merged = FileDocument(
            _id=file_id,
            original_name=name,
            extension="csv",
            mime_type="text/csv",
            category="tabular",
            size_bytes=0,  # 워커가 통합 파일을 만든 뒤 채운다
            storage=FileStorage(
                provider=self._storage.provider, key=f"uploads/{now:%Y}/{now:%m}/{file_id}"
            ),
            status="queued",
            processing_capabilities=CAPABILITIES["tabular"],
            created_at=now,
            updated_at=now,
            last_job_id=job.id,
        )
        await asyncio.to_thread(
            self._storage.write_text, job_params_key(job.id), request.model_dump_json()
        )
        await self._files.insert(merged)
        await self._jobs.insert(job)
        return merged, job
