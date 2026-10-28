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
    # 진단은 OCR이 없어도 끝나야 하므로 allow_missing_ocr로 연다. 이미지는 메타데이터만,
    # 스캔 PDF는 "OCR 필요" 문제로 결과에 남는다
    if extension in DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS:
        with open_document(path, extension, ocr, allow_missing_ocr=True) as document:
            return profile_document(document)
    # 업로드는 미지원 형식도 받아 두므로(category other) 여기서 처음 막힌다
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
    # 진행률은 단계 경계만 대략 찍는다. 읽기·진단이 대부분의 시간을 쓰므로 10→90 구간을 준다
    await ctx.report("validating", 5)
    # 원본이 지워졌거나 저장소가 바뀐 경우를 읽기 전에 분명한 오류 코드로 알린다
    path = ctx.storage.path(ctx.file.storage.key)
    if not path.is_file():
        raise AppError("SOURCE_MISSING", "원본 파일이 저장소에 없습니다.", 422)

    await ctx.report("parsing", 10)
    # 파일 읽기·OCR은 동기 코드라 이벤트 루프를 막지 않도록 스레드로 넘긴다
    # (막히면 워커의 종료 신호 처리와 DB 호출이 그동안 모두 멈춘다)
    profile = await asyncio.to_thread(
        build_profile, path, ctx.file.extension, ocr_options(ctx.settings)
    )

    await ctx.report("saving", 90)
    results = ResultRepository(ctx.db)
    # 재처리할 때마다 덮어쓰지 않고 새 버전으로 쌓는다(schema.md processing_results.version)
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
