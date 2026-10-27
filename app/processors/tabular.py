"""표 형태 파일(CSV·TSV·XLSX·XLS·JSON)을 같은 모양(머리글 + 행 반복자)으로 읽는다."""

import csv
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

import openpyxl
import xlrd

from app.core.errors import AppError
from app.processors.encoding import detect_encoding

# 모든 표 추출기가 돌려주는 칸 값 타입. 형식마다 다른 값을 이 범위로 맞춰 뒤 단계(품질 진단·
# 정제·통계)가 형식을 몰라도 되게 한다(Any 금지 규칙도 지킨다)
Cell = str | int | float | bool | datetime | date | time | None

# 민원 본문처럼 긴 칸을 읽을 수 있도록 csv 모듈의 칸 크기 제한을 늘린다
# (기본 128KB. 10MB는 한 칸이 비정상적으로 큰 파일만 걸러 낼 정도의 여유)
csv.field_size_limit(10 * 1024 * 1024)

TABULAR_EXTENSIONS = {"csv", "tsv", "xlsx", "xls", "json"}
# 구분자 후보. 쉼표가 기본이고, 세미콜론(유럽식 엑셀 저장)·탭·파이프도 본다
DELIMITER_CANDIDATES = (",", ";", "\t", "|")


@dataclass
class Table:
    """읽어 들인 표. rows는 한 번만 돌 수 있는 반복자이며 각 행은 columns와 같은 길이로 맞춘다."""

    format: str
    encoding: str | None
    sheet: str | None
    columns: list[str]
    rows: Iterator[list[Cell]]
    # 머리글 문제: 비어 있던 머리글 수, 중복된 머리글 이름
    empty_header_count: int = 0
    duplicate_headers: list[str] = field(default_factory=list)
    # 머리글보다 칸이 많거나 적었던 행 수(행 반복이 끝난 뒤 확정)
    ragged_row_count: int = 0


def normalize_headers(raw: list[Cell]) -> tuple[list[str], int, list[str]]:
    """머리글을 문자열로 바꾸고 빈 이름·중복 이름을 고친다.

    빈 머리글은 column_N, 중복은 이름_2·이름_3으로 바꾼다.
    (고친 머리글, 빈 머리글 수, 중복된 원래 이름 목록)을 돌려준다.

    파라미터:
        raw: 첫 행의 값들
    """
    columns: list[str] = []
    seen: set[str] = set()
    empty = 0
    duplicates: list[str] = []
    for index, value in enumerate(raw):
        # 엑셀 머리글은 숫자·날짜일 수 있어 문자열로 바꾸고, 앞뒤 공백은 같은 이름으로 본다
        name = "" if value is None else str(value).strip()
        # 빈 머리글은 열 번호(1부터)로 이름을 붙여, 정제 규칙에서 열을 지목할 수 있게 한다
        if not name:
            empty += 1
            name = f"column_{index + 1}"
        # 중복 이름은 dict 키로 쓰는 곳(행 → 객체, 열별 통계)에서 값이 덮이므로 꼬리표를 단다
        if name in seen:
            duplicates.append(name)
            suffix = 2
            # 원본에 이미 "이름_2"가 있을 수 있어 비어 있는 번호까지 올린다
            while f"{name}_{suffix}" in seen:
                suffix += 1
            name = f"{name}_{suffix}"
        seen.add(name)
        columns.append(name)
    return columns, empty, duplicates


def _fit_rows(table: Table, rows: Iterator[list[Cell]]) -> Iterator[list[Cell]]:
    """행 길이를 머리글 길이에 맞춘다. 모자라면 None으로 채우고 넘치면 자른다.

    길이가 달랐던 행 수는 table.ragged_row_count에 쌓는다. 완전히 빈 줄([])은 그대로 넘긴다.

    파라미터:
        table: 머리글이 정해진 표
        rows: 원본 행 반복자
    """
    width = len(table.columns)
    for row in rows:
        # 빈 줄은 길이 불일치로 세지 않고 그대로 넘겨, 품질 진단이 "빈 행"으로 따로 세게 한다
        if not row:
            yield []
            continue
        # 길이가 다른 행도 버리지 않고 맞춰서 넘긴다. 데이터를 잃지 않으면서 개수는 진단에 남긴다
        if len(row) != width:
            table.ragged_row_count += 1
            row = (row + [None] * width)[:width]
        yield row


def _pick_delimiter(first_line: str, extension: str) -> str:
    """구분자를 고른다. tsv는 탭, 그 밖에는 첫 줄에 가장 많이 나온 후보(없으면 쉼표).

    파라미터:
        first_line: 파일의 첫 줄
        extension: csv 또는 tsv
    """
    if extension == "tsv":
        return "\t"
    # 머리글 줄에서 후보별 개수만 비교한다. 머리글에는 따옴표 안 구분자가 드물어 이 정도로 충분하다
    counts = {d: first_line.count(d) for d in DELIMITER_CANDIDATES}
    # 개수가 같으면 후보 순서가 앞선 쪽(쉼표)이 이긴다
    best = max(counts, key=lambda d: counts[d])
    # 열이 하나뿐인 파일은 후보가 하나도 없으므로 표준 구분자인 쉼표로 둔다
    return best if counts[best] > 0 else ","


def _make_table(
    fmt: str,
    encoding: str | None,
    sheet: str | None,
    header: list[Cell],
    rows: Iterator[list[Cell]],
) -> Table:
    """머리글을 정리하고 행 길이를 맞춘 Table을 만든다.

    파라미터:
        fmt: 파일 형식(확장자)
        encoding: 텍스트 인코딩(엑셀·JSON은 None)
        sheet: 읽은 시트 이름(엑셀만)
        header: 첫 행 값
        rows: 나머지 행 반복자
    """
    columns, empty, duplicates = normalize_headers(header)
    # 행 반복자는 table을 참조해 ragged_row_count를 쌓으므로, table을 먼저 만들고 끼운다
    table = Table(fmt, encoding, sheet, columns, iter(()), empty, duplicates)
    table.rows = _fit_rows(table, rows)
    return table


@contextmanager
def _open_csv(path: Path, extension: str) -> Iterator[Table]:
    """CSV·TSV를 연다. 인코딩과 구분자는 자동으로 정한다.

    파라미터:
        path: 파일 경로
        extension: csv 또는 tsv
    """
    # 인코딩을 먼저 정해야 구분자 판단에 쓸 첫 줄을 제대로 읽을 수 있다
    encoding = detect_encoding(path)
    # newline=""은 csv 모듈 권장값. 따옴표 안 줄바꿈을 칸 내용으로 보존한다
    with path.open(encoding=encoding, newline="") as f:
        delimiter = _pick_delimiter(f.readline(), extension)
        # 첫 줄은 구분자 판단에만 썼으므로 처음으로 되돌려 머리글로 다시 읽는다
        f.seek(0)
        reader = csv.reader(f, delimiter=delimiter)
        header = next(reader, None)
        if header is None:
            raise AppError("EMPTY_TABLE", "표에 머리글 행이 없습니다.", 422)
        # 행은 지연 반복자로 넘겨 큰 CSV도 한 번에 메모리에 올리지 않는다
        rows: Iterator[list[Cell]] = (list(r) for r in reader)
        yield _make_table(extension, encoding, None, list(header), rows)


def _excel_cell(value: object) -> Cell:
    """엑셀 칸 값을 Cell 타입으로 맞춘다. 지원하지 않는 값은 문자열로 바꾼다.

    파라미터:
        value: openpyxl이 돌려준 칸 값
    """
    if value is None or isinstance(value, str | int | float | bool | datetime | date | time):
        return value
    # timedelta·Decimal·오류값 등은 버리지 않고 보이는 글자로 남긴다
    return str(value)


@contextmanager
def _open_xlsx(path: Path) -> Iterator[Table]:
    """XLSX의 첫 번째 시트를 연다(값만, 수식 결과 기준).

    원본은 저장소에 확장자 없이 저장되므로, 파일 이름의 확장자를 검사하는 openpyxl에
    경로 대신 열린 파일 객체를 넘긴다.

    파라미터:
        path: 파일 경로
    """
    with path.open("rb") as stream:
        # read_only: 행을 흘려 읽어 큰 시트도 메모리에 다 올리지 않는다
        # data_only: 수식 대신 마지막으로 저장된 계산 결과를 읽는다(분석 대상은 값이다)
        try:
            workbook = openpyxl.load_workbook(stream, read_only=True, data_only=True)
        except Exception as exc:  # openpyxl은 손상 유형마다 다른 예외를 던진다
            raise AppError("INVALID_FILE", "엑셀 파일을 읽을 수 없습니다.", 422) from exc
        # read_only 통합 문서는 파일을 붙잡고 있으므로 반드시 닫는다
        try:
            # 여러 시트 중 첫 시트만 분석한다(시트 선택은 기준 문서에 없음)
            sheet = workbook.worksheets[0]
            values = sheet.iter_rows(values_only=True)
            header = next(values, None)
            if header is None:
                raise AppError("EMPTY_TABLE", "표에 머리글 행이 없습니다.", 422)
            rows = ([_excel_cell(v) for v in row] for row in values)
            yield _make_table("xlsx", None, sheet.title, [_excel_cell(v) for v in header], rows)
        finally:
            workbook.close()


@contextmanager
def _open_xls(path: Path) -> Iterator[Table]:
    """XLS(구형 엑셀)의 첫 번째 시트를 연다. 날짜 칸은 datetime, 정수 값은 int로 바꾼다.

    파라미터:
        path: 파일 경로
    """
    # on_demand: 필요한 시트만 불러와 쓰지 않는 시트의 메모리를 아낀다
    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as exc:  # xlrd도 손상 유형마다 다른 예외를 던진다
        raise AppError("INVALID_FILE", "엑셀 파일을 읽을 수 없습니다.", 422) from exc
    try:
        sheet = book.sheet_by_index(0)

        def cell(c: xlrd.sheet.Cell) -> Cell:
            """xlrd 칸 하나를 Cell 타입으로 바꾼다.

            파라미터:
                c: xlrd 칸
            """
            # 빈 칸과 서식만 있는 칸은 모두 결측으로 본다(openpyxl의 None과 같게)
            if c.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                return None
            # XLS는 날짜를 일련번호(float)로 저장한다. 1900/1904 기준(datemode)에 맞춰 바꾼다
            if c.ctype == xlrd.XL_CELL_DATE:
                converted: datetime = xlrd.xldate_as_datetime(c.value, book.datemode)
                return converted
            if c.ctype == xlrd.XL_CELL_BOOLEAN:
                return bool(c.value)
            # XLS 숫자는 모두 float라 3이 3.0으로 나온다. XLSX와 결과를 맞추려고 정수는 int로
            if c.ctype == xlrd.XL_CELL_NUMBER:
                number = float(c.value)
                return int(number) if number.is_integer() else number
            return str(c.value)

        if sheet.nrows == 0:
            raise AppError("EMPTY_TABLE", "표에 머리글 행이 없습니다.", 422)
        header = [cell(c) for c in sheet.row(0)]
        rows = ([cell(c) for c in sheet.row(i)] for i in range(1, sheet.nrows))
        yield _make_table("xls", None, str(sheet.name), header, rows)
    finally:
        # on_demand로 연 통합 문서는 직접 풀어야 파일 핸들·메모리가 반환된다
        book.release_resources()


def _json_cell(value: object) -> Cell:
    """JSON 값을 Cell 타입으로 맞춘다. 객체·배열은 JSON 문자열로 바꾼다.

    파라미터:
        value: json.loads가 돌려준 값
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    # 중첩 구조는 펼치지 않고 글자로 둔다. ensure_ascii=False로 한글이 \uXXXX로 바뀌지 않게 한다
    return json.dumps(value, ensure_ascii=False)


@contextmanager
def _open_json(path: Path) -> Iterator[Table]:
    """JSON을 표로 연다. 객체 배열은 키를 열로, 객체 하나는 한 행으로, 값 배열은 value 열로 본다.

    파라미터:
        path: 파일 경로
    """
    # 바이트로 넘기면 json이 UTF-8/16/32를 스스로 판별한다(JSON 표준 인코딩은 UTF 계열뿐).
    # JSON은 구조상 흘려 읽을 수 없어 전체를 한 번에 읽는다(업로드 크기 제한이 상한)
    try:
        data: object = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AppError("INVALID_FILE", "JSON 형식이 아닙니다.", 422) from exc

    # 최상위가 객체 하나면 한 행짜리 표로 본다
    items: list[object] = data if isinstance(data, list) else [data]
    # 모든 항목이 객체일 때만 키를 열로 쓴다. 섞여 있으면 열을 정할 수 없어 value 한 열로 둔다
    if items and all(isinstance(item, dict) for item in items):
        # isinstance로 다시 거르는 것은 mypy가 dict 타입으로 좁히게 하려는 것
        records = [item for item in items if isinstance(item, dict)]
        # 순서를 지키는 집합으로 dict를 쓴다. 객체마다 키가 달라도 처음 나온 순서대로 열을 만든다
        keys: dict[str, None] = {}
        for record in records:
            keys.update(dict.fromkeys(str(k) for k in record))
        header: list[Cell] = list(keys)
        rows = ([_json_cell(r.get(k)) for k in keys] for r in records)
    else:
        header = ["value"]
        rows = ([_json_cell(item)] for item in items)
    yield _make_table("json", None, None, header, rows)


@contextmanager
def open_table(path: Path, extension: str) -> Iterator[Table]:
    """확장자에 맞는 추출기로 표를 연다. with 블록 안에서 table.rows를 끝까지 읽는다.

    파라미터:
        path: 파일 경로
        extension: csv, tsv, xlsx, xls, json 중 하나
    """
    if extension in ("csv", "tsv"):
        opener = _open_csv(path, extension)
    elif extension == "xlsx":
        opener = _open_xlsx(path)
    elif extension == "xls":
        opener = _open_xls(path)
    elif extension == "json":
        opener = _open_json(path)
    else:
        raise AppError("NOT_TABULAR", f"{extension} 파일은 표로 읽을 수 없습니다.", 422)
    # 행은 with 블록 안에서 지연으로 읽히므로, 인코딩·CSV 오류는 호출자가 행을 도는 중에 난다.
    # 그 예외가 이 yield 지점으로 되돌아오므로 여기서 한꺼번에 AppError로 바꾼다
    try:
        with opener as table:
            yield table
    except UnicodeDecodeError as exc:
        raise AppError("ENCODING_ERROR", "파일 인코딩을 읽는 중 오류가 발생했습니다.", 422) from exc
    except csv.Error as exc:
        raise AppError("INVALID_FILE", f"CSV를 읽을 수 없습니다: {exc}", 422) from exc
