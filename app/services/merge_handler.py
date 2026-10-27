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

# 통합 CSV의 열: 출처 열(출처파일ID·원본행)을 맨 앞에 두고 공통 필드를 한글 이름으로 잇는다.
# 맨 앞 두 열은 profiling.is_merged가 통합 파일을 알아보는 표시이기도 하다
MERGED_COLUMNS = [*SOURCE_COLUMNS, *FIELD_LABELS.values()]
# 분포를 계산할 통합 열과 보여줄 상위 개수
DISTRIBUTION_COLUMNS = ("출처파일ID", "지역", "기존분류", "처리기관")
# 행정동·처리부서는 종류가 수십 개라 10개로는 모자라고, 전부 넣으면 결과 문서가 커진다(잠정값)
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
    notes: list[str]  # 매핑 메모


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
    # 기관마다 날짜 표기가 달라(2026.07.14, 2026/07/14 13:00, 엑셀 날짜) 합친 뒤 정렬·월별 집계가
    # 되도록 하나로 맞춘다. 엑셀 칸은 이미 datetime·date로 온다
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and (parsed := parse_datetime(value)) is not None:
        return parsed.isoformat(timespec="seconds")
    # 읽지 못한 값은 버리지 않고 남긴다. 진단에서 형식 불일치로 드러나 담당자가 볼 수 있다
    return value


def merge_sources(sources: list[ResolvedSource]) -> MergeOutcome:
    """원본 파일들을 차례로 읽어 공통 열로 바꿔 합친다. 완전히 빈 행은 뺀다. 스레드에서 실행한다.

    파라미터:
        sources: 매핑이 확정된 원본 파일 목록
    """
    outcome = MergeOutcome(columns=list(MERGED_COLUMNS), rows=[])
    fields = list(FIELD_LABELS)
    for source in sources:
        # 원본을 하나씩 열어 바로 닫는다(여러 파일을 동시에 열어 두지 않게)
        with open_table(source.path, source.extension) as table:
            index = {name: i for i, name in enumerate(table.columns)}
            # 요청 시점에 확인했지만 그 뒤 원본이 바뀌었을 수 있어 한 번 더 본다
            missing = [c for c in source.mapping.values() if c not in index]
            if missing:
                raise AppError(
                    "UNKNOWN_COLUMN",
                    f"{source.name}에 {', '.join(missing)} 열이 없습니다.",
                    422,
                )
            count = 0
            for row_number, row in enumerate(table.rows, start=2):  # 1행은 머리글
                # 빈 행은 내용이 없어 통합에 넣지 않는다. 행 번호는 빈 행도 세므로
                # 원본행 값은 원본 파일에서 보이는 줄 번호와 맞는다
                if not row or all(is_missing(v) for v in row):
                    continue
                # 출처 열: 문제 행을 원본 파일·줄로 되짚어 갈 수 있게 한다(PRD 3장)
                merged: list[Cell] = [str(source.file_id), row_number]
                for name in fields:
                    column = source.mapping.get(name)
                    # 매핑하지 않은 필드는 빈칸. 이 출처에서 열 전체가 비면
                    # 진단이 "해당 없음"으로 본다(profile_table)
                    value = row[index[column]] if column else None
                    # 칸이 비었을 때만 고정값(예: 파일명의 시군구)으로 채운다. 원본 값이 우선이다
                    if is_missing(value):
                        value = source.constants.get(name)
                    merged.append(_normalize_date(value) if name == "received_at" else value)
                outcome.rows.append(merged)
                count += 1
            # 출처별 행 수는 리포트·화면의 출처 비교와 계보(sources) 기록에 쓴다
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
        # 앞뒤 공백만 다른 값은 같은 값으로 센다. 빈칸은 분포에서 뺀다(결측은 진단이 따로 센다)
        counts = Counter(
            str(row[position[column]]).strip()
            for row in outcome.rows
            if not is_missing(row[position[column]])
        )
        # 상위 N개만 보여 주되 전체 종류 수(distinct)를 함께 남겨 잘렸는지 알 수 있게 한다
        result[column] = {
            "distinct": len(counts),
            "top": [{"value": v, "count": n} for v, n in counts.most_common(TOP_N)],
        }
    # 월별: _normalize_date로 ISO("2026-07-...")가 된 값만 앞 7자(YYYY-MM)로 센다.
    # 날짜로 읽지 못해 원래 값이 남은 칸은 4번째 글자가 "-"가 아니라 빠진다
    months = Counter(
        str(value)[:7]
        for row in outcome.rows
        if isinstance(value := row[position["접수일시"]], str)
        and len(value) >= 7
        and value[4] == "-"
    )
    # 시간순으로 정렬해 화면이 그대로 추이 차트를 그리게 한다
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
    # 빈 통합 파일은 진단할 것이 없으므로 저장하지 않고 작업을 실패시킨다
    if not outcome.rows:
        raise AppError("EMPTY_MERGE", "합칠 행이 없습니다.", 422)
    # 임시 파일에 다 쓴 뒤 옮긴다. 쓰다가 실패해도 key 위치에 반쪽짜리 파일이 남지 않는다
    temp = storage.new_temp_path()
    try:
        write_csv(temp, outcome.columns, outcome.rows)
        # 크기는 옮기기 전에 잰다(files.sizeBytes에 넣는다)
        size = temp.stat().st_size
        storage.put(temp, key)
    finally:
        # put이 옮겼으면 이미 없고, 실패했으면 여기서 지운다
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
        # 원본 목록·매핑은 processing_jobs에 둘 필드가 없어 API가 작업 파라미터 파일로 남겼다
        raw = await asyncio.to_thread(ctx.storage.read_text, job_params_key(ctx.job.id))
        request = MergeRequest.model_validate_json(raw)
        files = FileRepository(ctx.db)
        sources: list[ResolvedSource] = []
        # 큐에서 기다리는 동안 원본이 삭제됐을 수 있어 다시 확인한다.
        # 이미 받은 작업의 실행 실패라 404·409 대신 422로 작업 오류에 남긴다
        for item in request.sources:
            doc = await files.get(ObjectId(item.file_id))
            if doc is None:
                raise AppError("FILE_NOT_FOUND", f"원본 파일({item.file_id})이 없습니다.", 422)
            path = ctx.storage.path(doc.storage.key)
            if not path.is_file():
                raise AppError("SOURCE_MISSING", f"{doc.original_name} 원본이 없습니다.", 422)
            # 빈 문자열·None 고정값은 버린다("값 없음"을 고정값으로 채우지 않게)
            constants = {
                k: v for k, v in item.constants.model_dump().items() if isinstance(v, str) and v
            }
            sources.append(
                ResolvedSource(
                    doc.id,
                    doc.original_name,
                    path,
                    doc.extension,
                    item.mapping.mapped(),
                    constants,
                    list(item.notes),
                )
            )

        # 파일 읽기·쓰기와 집계는 CPU·디스크 작업이라 스레드에서 돌려 워커 루프를 막지 않는다
        await ctx.report("merging", 15)
        outcome, size = await asyncio.to_thread(
            build_merged, sources, ctx.storage, ctx.file.storage.key
        )
        # API가 통합 파일 문서를 만들 때는 크기를 몰라 0으로 두었다
        await files.collection.update_one({"_id": ctx.file.id}, {"$set": {"sizeBytes": size}})

        # 저장한 CSV를 다시 읽지 않고 메모리의 행으로 바로 진단한다(같은 내용, 읽기 한 번 절약).
        # 인코딩은 write_csv가 쓴 utf-8-sig(엑셀에서 한글이 깨지지 않게 BOM을 붙인 형식)로 둔다
        await ctx.report("profiling", 70)
        profile = await asyncio.to_thread(
            profile_table, Table("csv", "utf-8-sig", None, outcome.columns, iter(outcome.rows))
        )
        # 계보(sources): 출처마다 행 수·매핑·고정값·메모를 남겨 통합 결과를 설명할 수 있게 한다.
        # AI 리포트 지시문도 이 값을 읽는다(report_handler SYSTEM_PROMPT)
        names = {str(s.file_id): s.name for s in sources}
        lineage: list[JsonValue] = [
            {
                "fileId": str(s.file_id),
                "name": s.name,
                "rows": outcome.source_rows.get(str(s.file_id), 0),
                "mapping": {str(k): v for k, v in s.mapping.items()},
                "constants": dict(s.constants),
                "notes": list[JsonValue](s.notes),
            }
            for s in sources
        ]
        dist = distributions(outcome)
        # 출처 분포는 ID만으로 읽기 어려워 파일명을 붙인다(파일명은 LLM에 보낼 때 마스킹된다)
        source_dist = dist["출처파일ID"]
        top = source_dist.get("top") if isinstance(source_dist, dict) else None
        if isinstance(top, list):
            for entry in top:
                if isinstance(entry, dict):
                    entry["name"] = names.get(str(entry.get("value")), "")

        # 통합 결과는 진단 결과(analysis)로 저장해 일반 파일과 같은 화면·API로 조회하게 한다.
        # merged·sources·distributions는 통합 파일에만 붙는 analysis 항목이다
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
        # 성공·실패와 관계없이 작업 파라미터 파일을 지운다. 매핑 정보는 결과의 sources에 남는다
        await asyncio.to_thread(ctx.storage.delete_prefix, job_dir_key(ctx.job.id))
