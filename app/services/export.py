"""정제 결과 다운로드 형식 변환. 산출물은 CSV로 저장되어 있고, json·xlsx는 요청 때 변환한다."""

import csv
import json
from pathlib import Path
from typing import Literal

import openpyxl

# GET /export?format=확장자(api-spec.md)로 받을 수 있는 형식. 정제가 표에만 되므로 표 형식만 둔다
ExportFormat = Literal["csv", "json", "xlsx"]

# 응답 Content-Type. CSV에 charset을 밝혀 브라우저가 한글을 다른 인코딩으로 추측하지 않게 한다
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
    # 산출물은 write_csv가 UTF-8 BOM으로 쓴 것이므로 인코딩을 판별하지 않고 그대로 읽는다
    with source.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        # 빈 파일이어도 오류 없이 빈 표로 변환되게 기본값 []을 준다
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
        # CSV는 결측을 빈 칸으로밖에 못 나타내므로, JSON에서는 null로 되돌려 결측임을 드러낸다.
        # strict=False: 길이가 다른 행이 있어도 변환을 멈추지 않는다
        records = [
            {c: (v if v != "" else None) for c, v in zip(columns, row, strict=False)}
            for row in rows
        ]
        target.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    # write_only: 행을 흘려 써 큰 결과도 메모리에 시트 전체를 만들지 않는다
    workbook = openpyxl.Workbook(write_only=True)
    sheet = workbook.create_sheet("cleaned")
    sheet.append(columns)
    for row in rows:
        # 빈 문자열 대신 None을 넣어야 엑셀에서 진짜 빈 칸이 된다
        sheet.append([v if v != "" else None for v in row])
    workbook.save(target)
