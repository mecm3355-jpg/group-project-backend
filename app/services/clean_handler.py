"""정제 작업("clean") 처리기. 원본에 규칙을 적용해 산출물 CSV를 만들고 정제 전후 요약을 저장한다."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from app.core.errors import AppError
from app.processors.tabular import Table, open_table
from app.repositories.results import ResultRepository
from app.schemas.cleaning import CleanRequest, CleanRule
from app.schemas.result import ResultDocument
from app.services.analyze_handler import build_profile
from app.services.artifacts import cleaned_data_key, job_dir_key, job_params_key
from app.services.cleaning import RuleChange, apply_rules, write_csv
from app.services.job_runner import JobContext
from app.services.profiling import Profile, profile_table
from app.services.storage import LocalStorage


@dataclass(frozen=True)
class CleaningOutcome:
    """정제 결과: 정제 전후 진단, 규칙별 변경 수, 산출물 크기."""

    before: Profile
    after: Profile
    changes: list[RuleChange]
    artifact_bytes: int


def run_cleaning(
    source: Path,
    extension: str,
    rules: list[CleanRule],
    storage: LocalStorage,
    artifact_key: str,
) -> CleaningOutcome:
    """원본을 읽어 규칙을 적용하고 산출물 CSV를 저장한 뒤 정제 전후를 진단한다. 스레드에서 실행한다.

    파라미터:
        source: 원본 파일 경로
        extension: 원본 확장자
        rules: 적용할 규칙(순서대로)
        storage: 산출물을 저장할 저장소
        artifact_key: 산출물 위치
    """
    # 정제 전 진단은 원본 기준으로 다시 계산한다. 직전 analysis 결과가 없거나 오래됐을 수 있다
    before = build_profile(source, extension)
    # 중복 제거·결측 채우기(중앙값 등)는 전체 행을 봐야 하므로 행을 메모리에 모은다
    with open_table(source, extension) as table:
        columns = table.columns
        rows = list(table.rows)
    cleaned, changes = apply_rules(columns, rows, rules)

    # 임시 파일에 다 쓴 뒤 저장소로 옮긴다. 쓰다 실패해도 산출물 위치에 반쪽 파일이 남지 않는다
    temp = storage.new_temp_path()
    try:
        write_csv(temp, columns, cleaned)
        # 옮기기 전에 크기를 잰다(put 뒤에는 임시 파일이 없을 수 있다)
        size = temp.stat().st_size
        storage.put(temp, artifact_key)
    finally:
        # put이 성공하면 이미 옮겨져 없고, 실패하면 여기서 지운다(missing_ok)
        temp.unlink(missing_ok=True)

    # 정제 후 진단은 메모리의 결과로 바로 계산한다. 산출물 CSV를 다시 읽지 않아도 되고,
    # 형식·인코딩은 write_csv가 쓴 값(csv, utf-8-sig)으로 표시한다
    after = profile_table(Table("csv", "utf-8-sig", None, columns, iter(cleaned)))
    return CleaningOutcome(before, after, changes, size)


def _summary(profile: Profile) -> dict[str, JsonValue]:
    """정제 전후 비교용 요약(행 수, 점수, 문제 수).

    파라미터:
        profile: 진단 결과
    """
    return {
        "rowCount": profile.analysis.get("rowCount"),
        "score": profile.quality.score,
        "issueCount": len(profile.quality.issues),
    }


def cleaning_record(
    rules: list[CleanRule], outcome: CleaningOutcome, artifact_key: str
) -> dict[str, JsonValue]:
    """processing_results.cleaning에 저장할 내용(적용 규칙, 변경 내역, 정제 전후 요약, 산출물 위치).

    파라미터:
        rules: 적용한 규칙
        outcome: 정제 결과
        artifact_key: 산출물 위치
    """
    # 산출물 본문은 넣지 않고 위치·크기만 남긴다(CLAUDE.md DB 설계 주의사항)
    return {
        # exclude_none: 규칙마다 쓰지 않는 선택 필드(column·columns·strategy)를 빼 기록을 짧게 한다
        "rules": [rule.model_dump(exclude_none=True) for rule in rules],
        "changes": [
            {**change.rule.model_dump(exclude_none=True), "affected": change.affected}
            for change in outcome.changes
        ],
        "before": _summary(outcome.before),
        "after": _summary(outcome.after),
        "artifacts": [
            {
                "kind": "cleaned_data",
                "format": "csv",
                "storageKey": artifact_key,
                "sizeBytes": outcome.artifact_bytes,
            }
        ],
    }


async def clean(ctx: JobContext) -> None:
    """정제 작업을 실행한다. 단계: validating → cleaning → saving.

    결과는 새 버전의 cleaning 결과로 저장하고(quality·analysis는 정제 후 기준),
    끝나면 작업 파라미터 파일을 지운다.

    파라미터:
        ctx: 작업 실행 정보
    """
    # 성공·실패와 상관없이 작업 임시 폴더(파라미터 파일)를 지우도록 전체를 try/finally로 감싼다
    try:
        await ctx.report("validating", 5)
        # 규칙은 API가 작업을 만들 때 저장소에 써 둔 파일에서 읽는다(processing_jobs에 파라미터
        # 필드가 없음, PRD 7.7). 워커가 다시 검증해 파일이 손상돼도 잘못된 규칙이 돌지 않게 한다
        raw = await asyncio.to_thread(ctx.storage.read_text, job_params_key(ctx.job.id))
        request = CleanRequest.model_validate_json(raw)
        # API에서 이미 막지만, 작업이 큐에 있는 동안 파일 상태가 바뀔 수 있어 다시 확인한다
        if not ctx.file.processing_capabilities.cleaning:
            raise AppError(
                "CLEANING_NOT_SUPPORTED", "이 파일 형식은 정제를 지원하지 않습니다.", 422
            )
        source = ctx.storage.path(ctx.file.storage.key)
        if not source.is_file():
            raise AppError("SOURCE_MISSING", "원본 파일이 저장소에 없습니다.", 422)

        await ctx.report("cleaning", 20)
        results = ResultRepository(ctx.db)
        # 버전을 먼저 정해야 산출물 파일 이름(v{version}.csv)을 정할 수 있다
        version = await results.next_version(ctx.file.id, "cleaning")
        artifact_key = cleaned_data_key(ctx.file.id, version)
        # 읽기·규칙 적용·CSV 쓰기는 모두 동기 작업이라 스레드에서 돌린다
        outcome = await asyncio.to_thread(
            run_cleaning, source, ctx.file.extension, request.rules, ctx.storage, artifact_key
        )

        await ctx.report("saving", 90)
        await results.insert(
            ResultDocument(
                field=ctx.file.id,
                job_id=ctx.job.id,
                result_type="cleaning",
                version=version,
                quality=outcome.after.quality,
                analysis=outcome.after.analysis,
                cleaning=cleaning_record(request.rules, outcome, artifact_key),
            )
        )
    finally:
        # 실패한 작업도 파라미터를 지운다. 재처리는 새 작업(새 파라미터 파일)으로 하기 때문이다
        await asyncio.to_thread(ctx.storage.delete_prefix, job_dir_key(ctx.job.id))
