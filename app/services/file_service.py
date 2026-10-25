import asyncio
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

from bson import ObjectId
from bson.errors import InvalidId

from app.core.config import Settings
from app.core.errors import AppError
from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.repositories.results import ResultRepository
from app.schemas.file import FileDocument, FileStatus, FileStorage
from app.schemas.job import JobDocument
from app.schemas.mongo import utc_now
from app.services.file_validation import CAPABILITIES, ZipLimits, detect_format
from app.services.storage import LocalStorage

COPY_CHUNK_BYTES = 1024 * 1024
# 업로드와 함께 만드는 기본 처리 작업(추출·진단)의 종류(PRD 7.2)
UPLOAD_JOB_TYPE = "analyze"


@dataclass(frozen=True)
class UploadedFile:
    """업로드 결과. 등록된 파일과 함께 만들어진 처리 작업."""

    file: FileDocument
    job: JobDocument


def parse_file_id(file_id: str) -> ObjectId:
    """경로의 파일 ID 문자열을 ObjectId로 바꾼다. 형식이 틀리면 404로 본다.

    파라미터:
        file_id: 클라이언트가 보낸 파일 ID(MongoDB _id 문자열)
    """
    try:
        return ObjectId(file_id)
    except (InvalidId, TypeError) as exc:
        raise AppError("FILE_NOT_FOUND", "파일을 찾을 수 없습니다.", 404) from exc


def clean_file_name(raw: str | None) -> str:
    """업로드 파일명에서 경로 부분을 떼고 파일명만 남긴다. 비어 있으면 오류.

    파라미터:
        raw: 클라이언트가 보낸 파일명(경로가 섞여 있을 수 있음)
    """
    name = PureWindowsPath(raw or "").name.strip()  # / 와 \ 모두 경로 구분자로 처리
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
    ) -> None:
        """서비스를 만든다.

        파라미터:
            files: files 저장소
            jobs: processing_jobs 저장소
            results: processing_results 저장소
            storage: 원본 파일 저장소
            settings: 업로드 제한 등 설정
        """
        self._files = files
        self._jobs = jobs
        self._results = results
        self._storage = storage
        self._settings = settings

    async def upload(self, source: BinaryIO, file_name: str | None) -> UploadedFile:
        """파일을 검증·저장하고 files 문서와 기본 처리 작업(queued)을 만든다.

        검증에 실패하면 아무것도 남기지 않는다. 처리 작업은 워커가 가져가 실행한다.

        파라미터:
            source: 업로드 파일 스트림
            file_name: 클라이언트가 보낸 원래 파일명
        """
        name = clean_file_name(file_name)
        extension = Path(name).suffix.lstrip(".").lower()
        limits = ZipLimits(
            self._settings.max_zip_uncompressed_bytes, self._settings.max_zip_entries
        )

        temp = self._storage.new_temp_path()
        try:
            size = await asyncio.to_thread(
                copy_with_limit, source, temp, self._settings.max_upload_bytes
            )
            detected = await asyncio.to_thread(detect_format, temp, extension, limits)

            now = utc_now()
            file_id = ObjectId()
            job = JobDocument(field=file_id, type=UPLOAD_JOB_TYPE)
            key = f"uploads/{now:%Y}/{now:%m}/{file_id}"
            doc = FileDocument(
                _id=file_id,
                original_name=name,
                extension=detected.extension,
                mime_type=detected.mime_type,
                category=detected.category,
                size_bytes=size,
                storage=FileStorage(provider=self._storage.provider, key=key),
                status="queued",
                processing_capabilities=CAPABILITIES[detected.category],
                created_at=now,
                updated_at=now,
                last_job_id=job.id,
            )

            await asyncio.to_thread(self._storage.put, temp, key)
            try:
                await self._files.insert(doc)
                await self._jobs.insert(job)
            except Exception:
                await self._files.delete(file_id)
                await asyncio.to_thread(self._storage.delete, key)
                raise
            return UploadedFile(file=doc, job=job)
        finally:
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
        """파일과 관련 작업·결과·원본 파일을 지운다. 없으면 404.

        파라미터:
            file_id: 파일 ID 문자열
        """
        doc = await self.get(file_id)
        await self._results.delete_by_file(doc.id)
        await self._jobs.delete_by_file(doc.id)
        await self._files.delete(doc.id)
        await asyncio.to_thread(self._storage.delete, doc.storage.key)
