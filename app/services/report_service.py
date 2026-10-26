from app.core.config import Settings
from app.core.errors import AppError
from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.repositories.results import ResultRepository
from app.schemas.job import REPORT_JOB_TYPE, JobDocument
from app.services.file_service import parse_file_id


class ReportService:
    """AI 분석 리포트 작업 생성 업무 로직. 리포트 작성은 워커(report_handler)가 한다."""

    def __init__(
        self,
        files: FileRepository,
        jobs: JobRepository,
        results: ResultRepository,
        settings: Settings,
    ) -> None:
        """서비스를 만든다.

        파라미터:
            files: files 저장소
            jobs: processing_jobs 저장소
            results: processing_results 저장소
            settings: 앱 설정
        """
        self._files = files
        self._jobs = jobs
        self._results = results
        self._settings = settings

    async def start_report(self, file_id: str) -> JobDocument:
        """리포트 작업을 만든다. 다음 경우에는 작업을 만들지 않고 거부한다.

        - 파일이 없음(404), 진단 결과가 없음(404), 끝나지 않은 작업이 있음(409)

        파라미터:
            file_id: 파일 ID 문자열
        """
        doc = await self._files.get(parse_file_id(file_id))
        if doc is None:
            raise AppError("FILE_NOT_FOUND", "파일을 찾을 수 없습니다.", 404)
        if await self._results.get_latest(doc.id, "analysis") is None:
            raise AppError(
                "RESULT_NOT_FOUND", "진단 결과가 없습니다. 먼저 추출·진단을 실행하세요.", 404
            )
        if await self._jobs.has_active(doc.id):
            raise AppError("JOB_ALREADY_RUNNING", "이미 처리 중인 작업이 있습니다.", 409)
        job = await self._jobs.insert(JobDocument(field=doc.id, type=REPORT_JOB_TYPE))
        await self._files.update_status(doc.id, "queued", last_job_id=job.id)
        return job
