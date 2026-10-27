import asyncio
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

from bson import ObjectId
from bson.errors import InvalidId

from app.core.config import Settings
from app.core.errors import AppError
from app.repositories.complaints import ComplaintRepository
from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.repositories.results import ResultRepository
from app.repositories.reviews import ReviewRepository
from app.schemas.file import FileDocument, FileStatus, FileStorage
from app.schemas.job import ANALYZE_JOB_TYPE, JobDocument
from app.schemas.mongo import utc_now
from app.schemas.preview_api import FilePreview
from app.schemas.result import ResultDocument
from app.services.artifacts import job_dir_key, results_dir_key
from app.services.file_validation import CAPABILITIES, ZipLimits, detect_format
from app.services.ocr_setup import ocr_options
from app.services.preview import build_preview
from app.services.storage import LocalStorage

# 업로드 복사 단위(1MB). 큰 파일도 메모리에 통째로 올리지 않고, 크기 초과를 일찍 알아챈다
COPY_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class UploadedFile:
    """업로드 결과. 등록된 파일과 함께 만들어진 처리 작업(내용 분석 미지원 파일은 None)."""

    file: FileDocument
    job: JobDocument | None


def parse_file_id(file_id: str) -> ObjectId:
    """경로의 파일 ID 문자열을 ObjectId로 바꾼다. 형식이 틀리면 404로 본다.

    파라미터:
        file_id: 클라이언트가 보낸 파일 ID(MongoDB _id 문자열)
    """
    # 형식이 틀린 ID는 존재할 수 없는 파일이므로 400 대신 404로 통일한다
    try:
        return ObjectId(file_id)
    except (InvalidId, TypeError) as exc:
        raise AppError("FILE_NOT_FOUND", "파일을 찾을 수 없습니다.", 404) from exc


def require_analyzable(doc: FileDocument) -> None:
    """내용 분석을 지원하는 형식인지 확인한다. other 형식이면 422(ANALYSIS_NOT_SUPPORTED).

    파라미터:
        doc: 파일 문서
    """
    if doc.category == "other":
        raise AppError(
            "ANALYSIS_NOT_SUPPORTED",
            f"{doc.extension or '확장자 없는'} 파일은 내용 분석을 지원하지 않습니다.",
            422,
        )


def clean_file_name(raw: str | None) -> str:
    """업로드 파일명에서 경로 부분을 떼고 파일명만 남긴다. 비어 있으면 오류.

    파라미터:
        raw: 클라이언트가 보낸 파일명(경로가 섞여 있을 수 있음)
    """
    # 일부 브라우저(구형 IE 등)는 "C:\Users\...\a.csv"처럼 전체 경로를 보낸다. 서버가
    # 리눅스여도 \를 구분자로 처리하려고 PureWindowsPath를 쓴다
    name = PureWindowsPath(raw or "").name.strip()  # / 와 \ 모두 경로 구분자로 처리
    # 이름은 originalName으로만 남고 저장 위치에는 쓰지 않지만, 표시·다운로드 이름이
    # 깨지지 않게 막는다
    if name in {"", ".", ".."}:
        raise AppError("INVALID_FILE_NAME", "파일명이 없습니다.", 400)
    return name


def copy_with_limit(source: BinaryIO, target: Path, max_bytes: int) -> int:
    """업로드 스트림을 파일로 복사하면서 크기 제한을 확인한다. 복사한 바이트 수를 돌려준다.

    파라미터:
        source: 업로드 파일 스트림
        target: 저장할 임시 파일 경로
        max_bytes: 허용하는 최대 크기
    """
    size = 0
    with target.open("wb") as out:
        while chunk := source.read(COPY_CHUNK_BYTES):
            size += len(chunk)
            # Content-Length는 클라이언트가 속일 수 있어, 실제로 받은 바이트로 센다.
            # 넘는 순간 멈춰 디스크를 끝까지 채우지 않는다(임시 파일은 호출한 쪽이 지운다)
            if size > max_bytes:
                raise AppError(
                    "FILE_TOO_LARGE",
                    f"파일이 너무 큽니다. 최대 {max_bytes // (1024 * 1024)}MB까지 가능합니다.",
                    413,
                )
            out.write(chunk)
    if size == 0:
        raise AppError("EMPTY_FILE", "빈 파일은 업로드할 수 없습니다.", 400)
    return size


class FileService:
    """파일 등록·조회·삭제 업무 로직."""

    def __init__(
        self,
        files: FileRepository,
        jobs: JobRepository,
        results: ResultRepository,
        storage: LocalStorage,
        settings: Settings,
        complaints: ComplaintRepository,
        reviews: ReviewRepository,
    ) -> None:
        """서비스를 만든다.

        파라미터:
            files: files 저장소
            jobs: processing_jobs 저장소
            results: processing_results 저장소
            storage: 원본 파일 저장소
            settings: 업로드 제한 등 설정
            complaints: complaints 저장소(파일 삭제 때 민원도 지운다)
            reviews: reviews 저장소(지운 민원의 검토도 지운다)
        """
        self._files = files
        self._jobs = jobs
        self._results = results
        self._complaints = complaints
        self._reviews = reviews
        self._storage = storage
        self._settings = settings

    async def upload(self, source: BinaryIO, file_name: str | None) -> UploadedFile:
        """파일을 검증·저장하고 files 문서와 기본 처리 작업(queued)을 만든다.

        검증에 실패하면 아무것도 남기지 않는다. 처리 작업은 워커가 가져가 실행한다.
        내용 분석을 지원하지 않는 형식(other)은 작업 없이 기본 정보만 등록하고 completed로 둔다.

        파라미터:
            source: 업로드 파일 스트림
            file_name: 클라이언트가 보낸 원래 파일명
        """
        name = clean_file_name(file_name)
        # 확장자는 대소문자를 무시한다(DATA.CSV). 실제 형식은 아래 detect_format이 내용으로 확인한다
        extension = Path(name).suffix.lstrip(".").lower()
        # DOCX·XLSX는 ZIP이라 압축 폭탄을 막을 한도를 함께 넘긴다
        limits = ZipLimits(
            self._settings.max_zip_uncompressed_bytes, self._settings.max_zip_entries
        )

        # 바로 최종 위치에 쓰지 않고 임시 파일에 받는다. 크기·형식 검증에 실패하면 임시 파일만
        # 지우면 되고, 저장소·DB에는 아무것도 남지 않는다
        temp = self._storage.new_temp_path()
        try:
            # 디스크 쓰기·형식 검사는 동기 작업이라 이벤트 루프를 막지 않게 스레드로 넘긴다
            size = await asyncio.to_thread(
                copy_with_limit, source, temp, self._settings.max_upload_bytes
            )
            # 확장자만 믿지 않고 실제 내용(시그니처·구조)으로 형식을 확인한다(CLAUDE.md 업로드 주의)
            detected = await asyncio.to_thread(detect_format, temp, extension, limits)

            now = utc_now()
            # _id를 미리 만들어 저장 key와 작업의 대상 파일 ID에 같은 값을 쓴다
            file_id = ObjectId()
            analyzable = detected.category != "other"
            # 미지원 형식(other)은 처리할 것이 없으므로 작업을 만들지 않는다
            job = JobDocument(field=file_id, type=ANALYZE_JOB_TYPE) if analyzable else None
            # 연·월 폴더로 나눠 한 폴더에 파일이 몰리지 않게 한다. 원래 이름을 쓰지 않으므로
            # 이름 충돌·경로 조작 걱정이 없다(schema.md storage.key 예시 형식)
            key = f"uploads/{now:%Y}/{now:%m}/{file_id}"
            doc = FileDocument(
                _id=file_id,
                original_name=name,
                extension=detected.extension,
                mime_type=detected.mime_type,
                category=detected.category,
                size_bytes=size,
                storage=FileStorage(provider=self._storage.provider, key=key),
                status="queued" if job else "completed",
                processing_capabilities=CAPABILITIES[detected.category],
                created_at=now,
                updated_at=now,
                last_job_id=job.id if job else None,
            )

            # 원본을 먼저 옮긴 뒤 DB에 등록한다. 순서가 반대면 워커가 원본이 오기 전에 작업을
            # 가져가 SOURCE_MISSING으로 실패할 수 있다
            await asyncio.to_thread(self._storage.put, temp, key)
            # MongoDB 트랜잭션 없이 두 컬렉션에 쓰므로, 중간에 실패하면 직접 되돌린다
            try:
                await self._files.insert(doc)
                if job is not None:
                    await self._jobs.insert(job)
            except Exception:
                # 작업 없는 queued 파일이나 DB에 없는 원본 파일이 남지 않게 정리한 뒤 다시 올린다
                await self._files.delete(file_id)
                await asyncio.to_thread(self._storage.delete, key)
                raise
            return UploadedFile(file=doc, job=job)
        finally:
            # 성공하면 이미 옮겨져 없고, 검증 실패 등으로 남았으면 여기서 지운다
            temp.unlink(missing_ok=True)

    async def search(
        self, status: FileStatus | None, name: str | None, page: int, size: int
    ) -> tuple[list[FileDocument], int]:
        """파일 목록 한 페이지와 전체 개수를 돌려준다.

        파라미터:
            status: 이 상태인 파일만. None이면 전체
            name: 원래 파일명 검색어(부분 일치)
            page: 페이지 번호(1부터)
            size: 페이지 크기
        """
        # 페이지 번호는 1부터라 skip은 (page - 1) * size. 전체 개수는 같은 조건으로 따로 센다
        items = await self._files.search(status, name, skip=(page - 1) * size, limit=size)
        total = await self._files.count(status, name)
        return items, total

    async def get(self, file_id: str) -> FileDocument:
        """파일 하나를 돌려준다. 없으면 404.

        파라미터:
            file_id: 파일 ID 문자열
        """
        doc = await self._files.get(parse_file_id(file_id))
        if doc is None:
            raise AppError("FILE_NOT_FOUND", "파일을 찾을 수 없습니다.", 404)
        return doc

    async def delete(self, file_id: str) -> None:
        """파일과 관련 작업·결과·민원·검토, 원본·산출물·작업 파라미터 파일을 지운다. 없으면 404.

        카테고리·후보·이슈는 여러 파일에 걸친 체계라 남긴다.

        파라미터:
            file_id: 파일 ID 문자열
        """
        doc = await self.get(file_id)
        # 작업 문서를 지우기 전에 목록을 받아 둔다. 작업별 임시 폴더를 지울 때 ID가 필요하다
        jobs = await self._jobs.list_by_file(doc.id)
        # 딸린 데이터부터 지우고 files 문서를 마지막에 지운다. 중간에 실패해도 파일이 남아
        # 있어 다시 삭제를 요청할 수 있다(고아 결과·민원이 남지 않는다)
        await self._results.delete_by_file(doc.id)
        # 민원을 지우며 돌려받은 민원 ID로 그 검토 기록을 지운다(검토는 fileId를 갖지 않는다)
        await self._reviews.delete_by_complaints(await self._complaints.delete_by_file(doc.id))
        await self._jobs.delete_by_file(doc.id)
        await self._files.delete(doc.id)
        # 디스크 파일은 DB 정리 뒤에 지운다. 여기서 실패해도 남는 것은 찾을 수 없는 파일뿐이다
        await asyncio.to_thread(self._storage.delete, doc.storage.key)
        await asyncio.to_thread(self._storage.delete_prefix, results_dir_key(doc.id))
        for job in jobs:
            await asyncio.to_thread(self._storage.delete_prefix, job_dir_key(job.id))

    async def preview(self, file_id: str) -> FilePreview:
        """원본 파일의 앞부분(표 20행 또는 텍스트 20줄)을 돌려준다.

        파라미터:
            file_id: 파일 ID 문자열
        """
        doc = await self.get(file_id)
        path = self._storage.path(doc.storage.key)
        # DB 기록과 저장소가 어긋난 상태라 404(파일 없음)와 구분해 409로 알린다
        if not path.is_file():
            raise AppError("SOURCE_MISSING", "원본 파일이 저장소에 없습니다.", 409)
        # 미리보기는 작업을 만들지 않고 요청 안에서 바로 읽는다(앞부분만 읽어 짧다).
        # 그래도 파일 읽기·OCR이 동기라 스레드에서 돌린다
        return await asyncio.to_thread(
            build_preview, str(doc.id), path, doc.extension, ocr_options(self._settings)
        )

    async def start_processing(self, file_id: str) -> JobDocument:
        """추출·진단 작업을 새로 만든다(재처리). 끝나지 않은 작업이 있으면 409.

        내용 분석을 지원하지 않는 형식(other)은 작업을 만들지 않고 422를 낸다.

        파일 상태는 queued로, lastJobId는 새 작업으로 바꾼다. 실행은 워커가 한다.

        파라미터:
            file_id: 파일 ID 문자열
        """
        doc = await self.get(file_id)
        require_analyzable(doc)
        # 같은 파일에 작업을 겹쳐 돌리면 결과 버전 번호가 겹치거나 파일 상태가 뒤섞이므로 막는다
        if await self._jobs.has_active(doc.id):
            raise AppError("JOB_ALREADY_RUNNING", "이미 처리 중인 작업이 있습니다.", 409)
        # 작업 ID만 바로 돌려주고 실행은 워커에 맡긴다(api-spec.md: 오래 걸리는 작업은 작업 ID 발급)
        job = await self._jobs.insert(JobDocument(field=doc.id, type=ANALYZE_JOB_TYPE))
        await self._files.update_status(doc.id, "queued", last_job_id=job.id)
        return job

    async def latest_analysis(self, file_id: str) -> ResultDocument:
        """가장 최근의 분석 결과(품질 진단 포함)를 돌려준다. 아직 없으면 404, other 형식이면 422.

        파라미터:
            file_id: 파일 ID 문자열
        """
        doc = await self.get(file_id)
        require_analyzable(doc)
        result = await self._results.get_latest(doc.id, "analysis")
        if result is None:
            raise AppError("RESULT_NOT_FOUND", "아직 진단 결과가 없습니다.", 404)
        return result
