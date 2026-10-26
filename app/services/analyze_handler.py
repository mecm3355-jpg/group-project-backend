"""업로드·재처리 때 만드는 "analyze" 작업(데이터 추출·품질 진단) 처리기."""

import asyncio
from pathlib import Path

from app.core.errors import AppError
from app.processors.document import DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS, OcrOptions, open_document
from app.processors.tabular import TABULAR_EXTENSIONS, open_table
from app.processors.text import TEXT_EXTENSIONS, open_text
from app.repositories.results import ResultRepository
from app.schemas.result import ResultDocument
from app.services.job_runner import JobContext
from app.services.ocr_setup import ocr_options
from app.services.profiling import Profile, profile_document, profile_table, profile_text


def build_profile(path: Path, extension: str, ocr: OcrOptions | None = None) -> Profile:
    """파일 형식에 맞게 열어 품질 진단·분석 결과를 계산한다. 시간이 걸리므로 스레드에서 실행한다.

    파라미터:
        path: 원본 파일 경로
        extension: 파일 확장자
        ocr: 스캔 PDF·이미지를 읽을 OCR 설정(없으면 OCR하지 않는다)
    """
    if extension in TABULAR_EXTENSIONS:
        with open_table(path, extension) as table:
            return profile_table(table)
    if extension in TEXT_EXTENSIONS:
        with open_text(path, extension) as text:
            return profile_text(text)
    if extension in DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS:
        with open_document(path, extension, ocr) as document:
            return profile_document(document)
    raise AppError(
        "ANALYSIS_NOT_SUPPORTED",
        f"{extension} 파일의 추출·진단은 아직 지원하지 않습니다.",
        422,
    )


async def analyze(ctx: JobContext) -> None:
    """원본 파일을 읽어 품질 진단·분석 결과를 새 버전의 analysis 결과로 저장한다.

    단계: validating(원본 확인) → parsing(읽기와 품질 진단·통계를 한 번에) → saving.
    실패하면 마지막 단계가 남는다.

    파라미터:
        ctx: 작업 실행 정보
    """
    await ctx.report("validating", 5)
    path = ctx.storage.path(ctx.file.storage.key)
    if not path.is_file():
        raise AppError("SOURCE_MISSING", "원본 파일이 저장소에 없습니다.", 422)

    await ctx.report("parsing", 10)
    profile = await asyncio.to_thread(
        build_profile, path, ctx.file.extension, ocr_options(ctx.settings)
    )

    await ctx.report("saving", 90)
    results = ResultRepository(ctx.db)
    version = await results.next_version(ctx.file.id, "analysis")
    await results.insert(
        ResultDocument(
            field=ctx.file.id,
            job_id=ctx.job.id,
            result_type="analysis",
            version=version,
            quality=profile.quality,
            analysis=profile.analysis,
        )
    )
