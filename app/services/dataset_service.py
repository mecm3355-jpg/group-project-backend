"""통합 데이터셋 업무 로직: 공통 필드 매핑 제안과 통합 작업 생성.

API 명세서에 없는 기능이다. 실제 합치기는 오래 걸릴 수 있어 워커(merge_handler)가 한다.
"""

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
    # 행은 읽지 않고 머리글만 본다. 큰 파일도 요청 시점에 빠르게 매핑 검증을 할 수 있다.
    # 진단 결과의 열 이름 대신 원본을 다시 읽는 것은, 워커가 실제로 읽을 열 이름과
    # 맞춰 보기 위해서다
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
        # 통합은 열 단위 매핑이라 표 파일만 받는다(텍스트·문서는 열이 없다)
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
        # 원본을 다시 읽지 않고 진단 결과의 열 정보·통계를 쓴다(값 없이 열 요약만으로 제안)
        analysis = await self._results.get_latest(doc.id, "analysis")
        if analysis is None:
            raise AppError(
                "RESULT_NOT_FOUND", "진단 결과가 없습니다. 먼저 추출·진단을 실행하세요.", 404
            )
        # analysis는 자유 형식 dict라 형태를 확인하며 꺼낸다
        # (옛 결과가 형식이 달라도 500이 나지 않게)
        column_info = analysis.analysis.get("columns")
        info: list[JsonValue] = column_info if isinstance(column_info, list) else []
        columns = [
            str(c["name"]) for c in info if isinstance(c, dict) and isinstance(c.get("name"), str)
        ]
        # 열별 값 빈도(topValues)는 원본 값이라 LLM에 보내지 않는다
        stats = without_raw_values(analysis.analysis).get("statistics")
        statistics: dict[str, JsonValue] = stats if isinstance(stats, dict) else {}

        # 열 매핑은 정형 작업이라 fast 배포로 충분하다
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
            # 매핑 제안은 담당자가 어차피 확인·수정하는 초안이라, AI가 실패해도 요청을
            # 실패시키지 않고 규칙 제안으로 대신한다. 실패 원인은 메모와 로그에 남긴다
            # (로그에는 코드만 남긴다: 메시지에 입력 일부가 섞일 수 있다)
            except AppError as exc:
                logger.warning("AI 매핑 제안 실패, 규칙으로 대신함: %s", exc.code)
                fallback_note = f"AI 제안에 실패해({exc.code}) 열 이름 규칙으로 제안했습니다."
        else:
            fallback_note = "AI 연결 정보가 없어 열 이름 규칙으로 제안했습니다."

        # 규칙 방식은 외부로 보내지 않으므로 원래 파일명을 그대로 써도 된다.
        # 대신한 이유를 메모 맨 앞에 두어 화면에서 method와 함께 바로 보이게 한다
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
        # 워커에서 실패하면 빈 통합 파일 문서가 남으므로, 막을 수 있는 오류는
        # 요청 시점에 모두 막는다
        seen: set[str] = set()
        for item in request.sources:
            # 같은 파일을 두 번 넣으면 모든 행이 중복이 된다
            if item.file_id in seen:
                raise AppError(
                    "DUPLICATE_SOURCE", f"같은 파일을 두 번 넣었습니다: {item.file_id}", 422
                )
            seen.add(item.file_id)
            doc = await self._tabular_file(item.file_id)
            mapped = item.mapping.mapped()
            if not mapped:
                raise AppError("EMPTY_MAPPING", f"{doc.original_name}의 매핑이 비어 있습니다.", 422)
            # files 문서는 있는데 원본이 지워진 경우: 요청 잘못이 아니라 서버 상태 문제라 409
            path = self._storage.path(doc.storage.key)
            if not path.is_file():
                raise AppError("SOURCE_MISSING", f"{doc.original_name} 원본이 없습니다.", 409)
            # 파일 읽기는 막히는 작업이라 스레드에서 한다(API 이벤트 루프를 붙잡지 않게)
            columns = await asyncio.to_thread(read_columns, path, doc.extension)
            unknown = [c for c in mapped.values() if c not in columns]
            if unknown:
                raise AppError(
                    "UNKNOWN_COLUMN",
                    f"{doc.original_name}에 {', '.join(unknown)} 열이 없습니다.",
                    422,
                )

        now = utc_now()
        # 파일 ID를 먼저 정해 작업(field)과 저장 위치(storage.key)에 같은 ID를 쓴다
        file_id = ObjectId()
        # 이름을 안 주면 원본 수·시각으로 지어 목록에서 구분되게 한다. 결과는 항상 CSV다
        name = request.name or f"통합_데이터셋_{len(request.sources)}개_{now:%Y%m%d_%H%M}"
        if not name.lower().endswith(".csv"):
            name += ".csv"
        job = JobDocument(field=file_id, type=MERGE_JOB_TYPE)
        # 통합 결과도 일반 업로드와 같은 files 문서로 만들어 진단·정제·리포트를 그대로 쓴다.
        # 저장 위치 규칙도 업로드와 같다(uploads/연/월/파일ID)
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
        # 원본 목록·매핑은 processing_jobs에 넣을 필드가 없어(설계 파일 필드 추가 금지)
        # 작업 파라미터 파일로 저장하고 워커가 읽는다. 작업보다 먼저 써서
        # 워커가 아직 없는 파일을 읽지 않게 한다
        await asyncio.to_thread(
            self._storage.write_text, job_params_key(job.id), request.model_dump_json()
        )
        # 파일 문서를 작업보다 먼저 넣는다: 워커가 작업을 가져갔을 때 대상 파일이 이미 있어야 한다
        await self._files.insert(merged)
        await self._jobs.insert(job)
        return merged, job
