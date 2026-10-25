"""통합 데이터셋 작업("merge") 처리기.

여러 원본 파일을 확인된 매핑으로 공통 열에 맞춰 하나의 CSV로 합친다. 각 행에는 출처 파일 ID와
원본 행 번호를 남겨 원본으로 되돌아갈 수 있게 한다(PRD 3장). 합친 뒤에는 진단과 함께
출처·지역·기존분류·처리기관·월별 분포를 계산해 분석 결과로 저장한다.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from bson import ObjectId
from pydantic import JsonValue

from app.core.errors import AppError
from app.processors.tabular import Cell, Table, open_table
from app.repositories.files import FileRepository
from app.repositories.results import ResultRepository
from app.schemas.dataset import FIELD_LABELS, SOURCE_COLUMNS, CanonicalField, MergeRequest
from app.schemas.result import ResultDocument
from app.services.artifacts import job_dir_key, job_params_key
from app.services.cleaning import write_csv
from app.services.job_runner import JobContext
from app.services.profiling import is_missing, parse_datetime, profile_table
from app.services.storage import LocalStorage

MERGED_COLUMNS = [*SOURCE_COLUMNS, *FIELD_LABELS.values()]
# 분포를 계산할 통합 열과 보여줄 상위 개수
DISTRIBUTION_COLUMNS = ("출처파일ID", "지역", "기존분류", "처리기관")
TOP_N = 30


@dataclass(frozen=True)
class ResolvedSource:
    """통합에 넣을 원본 파일(경로와 매핑이 확정된 것)."""

    file_id: ObjectId
    name: str
    path: Path
    extension: str
    mapping: dict[CanonicalField, str]  # 공통 필드 → 원본 열
    constants: dict[str, str]  # 공통 필드 → 고정값


@dataclass
class MergeOutcome:
    """통합 결과: 열·행과 출처별 행 수."""

    columns: list[str]
    rows: list[list[Cell]]
    source_rows: dict[str, int] = field(default_factory=dict)


def _normalize_date(value: Cell) -> Cell:
    """접수일시를 ISO 형식으로 맞춘다. 날짜로 읽히지 않으면 원래 값을 둔다.

    파라미터:
        value: 원본 칸 값
    """
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and (parsed := parse_datetime(value)) is not None:
        return parsed.isoformat(timespec="seconds")
    return value


def merge_sources(sources: list[ResolvedSource]) -> MergeOutcome:
    """원본 파일들을 차례로 읽어 공통 열로 바꿔 합친다. 완전히 빈 행은 뺀다. 스레드에서 실행한다.

    파라미터:
        sources: 매핑이 확정된 원본 파일 목록
    """
    outcome = MergeOutcome(columns=list(MERGED_COLUMNS), rows=[])
    fields = list(FIELD_LABELS)
    for source in sources:
        with open_table(source.path, source.extension) as table:
            index = {name: i for i, name in enumerate(table.columns)}
            missing = [c for c in source.mapping.values() if c not in index]
            if missing:
                raise AppError(
                    "UNKNOWN_COLUMN",
                    f"{source.name}에 {', '.join(missing)} 열이 없습니다.",
                    422,
                )
            count = 0
            for row_number, row in enumerate(table.rows, start=2):  # 1행은 머리글
                if not row or all(is_missing(v) for v in row):
                    continue
                merged: list[Cell] = [str(source.file_id), row_number]
                for name in fields:
                    column = source.mapping.get(name)
                    value = row[index[column]] if column else None
                    if is_missing(value):
                        value = source.constants.get(name)
                    merged.append(_normalize_date(value) if name == "received_at" else value)
                outcome.rows.append(merged)
                count += 1
            outcome.source_rows[str(source.file_id)] = count
    return outcome


def distributions(outcome: MergeOutcome) -> dict[str, JsonValue]:
    """통합 데이터의 분포: 출처·지역·기존분류·처리기관별 건수(상위 30)와 월별 건수.

    파라미터:
        outcome: 통합 결과
    """
    position = {name: i for i, name in enumerate(outcome.columns)}
    result: dict[str, JsonValue] = {}
    for column in DISTRIBUTION_COLUMNS:
        counts = Counter(
            str(row[position[column]]).strip()
            for row in outcome.rows
            if not is_missing(row[position[column]])
        )
        result[column] = {
            "distinct": len(counts),
            "top": [{"value": v, "count": n} for v, n in counts.most_common(TOP_N)],
        }
    months = Counter(
        str(value)[:7]
        for row in outcome.rows
        if isinstance(value := row[position["접수일시"]], str)
        and len(value) >= 7
        and value[4] == "-"
    )
    result["월별"] = dict(sorted(months.items()))
    return result


def build_merged(
    sources: list[ResolvedSource], storage: LocalStorage, key: str
) -> tuple[MergeOutcome, int]:
    """통합 CSV를 만들어 저장소 key 위치에 둔다. (통합 결과, 파일 크기)를 돌려준다.

    파라미터:
        sources: 매핑이 확정된 원본 파일 목록
        storage: 저장소
        key: 통합 파일을 둘 위치(통합 파일의 files.storage.key)
    """
    outcome = merge_sources(sources)
    if not outcome.rows:
        raise AppError("EMPTY_MERGE", "합칠 행이 없습니다.", 422)
    temp = storage.new_temp_path()
    try:
        write_csv(temp, outcome.columns, outcome.rows)
        size = temp.stat().st_size
        storage.put(temp, key)
    finally:
        temp.unlink(missing_ok=True)
    return outcome, size


async def merge(ctx: JobContext) -> None:
    """통합 작업을 실행한다. 단계: validating → merging → profiling → saving.

    ctx.file은 새로 만들 통합 파일이다. 원본 목록과 매핑은 작업 파라미터 파일에서 읽는다.

    파라미터:
        ctx: 작업 실행 정보
    """
    try:
        await ctx.report("validating", 5)
        raw = await asyncio.to_thread(ctx.storage.read_text, job_params_key(ctx.job.id))
        request = MergeRequest.model_validate_json(raw)
        files = FileRepository(ctx.db)
        sources: list[ResolvedSource] = []
        for item in request.sources:
            doc = await files.get(ObjectId(item.file_id))
            if doc is None:
                raise AppError("FILE_NOT_FOUND", f"원본 파일({item.file_id})이 없습니다.", 422)
            path = ctx.storage.path(doc.storage.key)
            if not path.is_file():
                raise AppError("SOURCE_MISSING", f"{doc.original_name} 원본이 없습니다.", 422)
            constants = {
                k: v for k, v in item.constants.model_dump().items() if isinstance(v, str) and v
            }
            sources.append(
                ResolvedSource(
                    doc.id, doc.original_name, path, doc.extension, item.mapping.mapped(), constants
                )
            )

        await ctx.report("merging", 15)
        outcome, size = await asyncio.to_thread(
            build_merged, sources, ctx.storage, ctx.file.storage.key
        )
        await files.collection.update_one({"_id": ctx.file.id}, {"$set": {"sizeBytes": size}})

        await ctx.report("profiling", 70)
        profile = await asyncio.to_thread(
            profile_table, Table("csv", "utf-8-sig", None, outcome.columns, iter(outcome.rows))
        )
        names = {str(s.file_id): s.name for s in sources}
        lineage: list[JsonValue] = [
            {
                "fileId": str(s.file_id),
                "name": s.name,
                "rows": outcome.source_rows.get(str(s.file_id), 0),
                "mapping": {str(k): v for k, v in s.mapping.items()},
                "constants": dict(s.constants),
            }
            for s in sources
        ]
        dist = distributions(outcome)
        source_dist = dist["출처파일ID"]
        top = source_dist.get("top") if isinstance(source_dist, dict) else None
        if isinstance(top, list):
            for entry in top:
                if isinstance(entry, dict):
                    entry["name"] = names.get(str(entry.get("value")), "")

        await ctx.report("saving", 90)
        results = ResultRepository(ctx.db)
        await results.insert(
            ResultDocument(
                field=ctx.file.id,
                job_id=ctx.job.id,
                result_type="analysis",
                version=await results.next_version(ctx.file.id, "analysis"),
                quality=profile.quality,
                analysis={
                    **profile.analysis,
                    "merged": True,
                    "sources": lineage,
                    "distributions": dist,
                },
            )
        )
    finally:
        await asyncio.to_thread(ctx.storage.delete_prefix, job_dir_key(ctx.job.id))
