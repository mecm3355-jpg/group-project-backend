"""정제 결과 다운로드 형식 변환. 산출물은 CSV로 저장되어 있고, json·xlsx는 요청 때 변환한다."""

import csv
import json
from pathlib import Path
from typing import Literal

import openpyxl

ExportFormat = Literal["csv", "json", "xlsx"]

MEDIA_TYPES: dict[ExportFormat, str] = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _read_csv(source: Path) -> tuple[list[str], list[list[str]]]:
    """산출물 CSV를 (열, 행)으로 읽는다.

    파라미터:
        source: 산출물 CSV 경로
    """
    with source.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        columns = next(reader, [])
        return columns, [row for row in reader]


def convert(source: Path, target: Path, fmt: Literal["json", "xlsx"]) -> None:
    """산출물 CSV를 json(객체 배열, 빈 칸은 null) 또는 xlsx로 바꿔 쓴다. 값은 문자열 그대로 둔다.

    파라미터:
        source: 산출물 CSV 경로
        target: 쓸 파일 경로
        fmt: json 또는 xlsx
    """
    columns, rows = _read_csv(source)
    if fmt == "json":
        records = [
            {c: (v if v != "" else None) for c, v in zip(columns, row, strict=False)}
            for row in rows
        ]
        target.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    workbook = openpyxl.Workbook(write_only=True)
    sheet = workbook.create_sheet("cleaned")
    sheet.append(columns)
    for row in rows:
        sheet.append([v if v != "" else None for v in row])
    workbook.save(target)
