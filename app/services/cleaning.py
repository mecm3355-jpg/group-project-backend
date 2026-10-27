"""정제 규칙을 표 데이터에 적용한다. 같은 원본·같은 규칙이면 항상 같은 결과가 나온다."""

import csv
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from app.core.errors import AppError
from app.processors.tabular import Cell
from app.schemas.cleaning import (
    CleanRule,
    FillMissingRule,
    RemoveDuplicatesRule,
    TrimWhitespaceRule,
)
from app.services.profiling import cell_key, is_missing, parse_number


@dataclass(frozen=True)
class RuleChange:
    """규칙 하나를 적용해 바뀐 행·칸 수."""

    rule: CleanRule
    affected: int


def _column_index(columns: list[str], name: str) -> int:
    """열 이름의 위치를 찾는다. 없으면 AppError(UNKNOWN_COLUMN).

    파라미터:
        columns: 표의 열 이름
        name: 찾을 열 이름
    """
    try:
        return columns.index(name)
    except ValueError as exc:
        raise AppError("UNKNOWN_COLUMN", f"'{name}' 열이 없습니다.", 422) from exc


def check_columns(columns: list[str], rules: list[CleanRule]) -> None:
    """규칙이 가리키는 열이 모두 표에 있는지 확인한다.

    파라미터:
        columns: 표의 열 이름
        rules: 확인할 규칙 목록
    """
    for rule in rules:
        # 중복 제거는 열을 비우면 "모든 열 기준"이라 확인할 열이 없다
        if isinstance(rule, RemoveDuplicatesRule):
            names = rule.columns or []
        else:
            names = [rule.column]
        for name in names:
            _column_index(columns, name)


def _as_number(value: Cell) -> float | None:
    """숫자로 볼 수 있는 값이면 실수로, 아니면 None.

    파라미터:
        value: 칸 값
    """
    # bool은 int의 하위 타입이라 먼저 걸러야 True가 1로 평균에 섞이지 않는다
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    # CSV는 모든 칸이 문자열이므로 품질 진단과 같은 숫자 판정(parse_number)을 써서 결과를 맞춘다
    if isinstance(value, str):
        return parse_number(value)
    return None


def _number_cell(value: float) -> Cell:
    """채울 숫자를 정수면 int로, 아니면 소수 4자리로 반올림한 float로 만든다.

    파라미터:
        value: 계산한 값
    """
    # 평균은 0.1+0.2 같은 부동소수 꼬리가 붙으므로 4자리에서 끊어 CSV에 깔끔하게 쓴다
    rounded = round(value, 4)
    # 정수 열에 "30.0"이 섞이면 다음 진단에서 형식이 흔들리므로 정수는 int로 둔다
    return int(rounded) if rounded.is_integer() else rounded


def _fill_value(rows: list[list[Cell]], index: int, rule: FillMissingRule) -> Cell:
    """fill_missing 규칙으로 채울 값을 계산한다.

    파라미터:
        rows: 표의 행
        index: 대상 열 위치
        rule: 결측 채우기 규칙
    """
    if rule.strategy == "constant":
        return rule.value
    # 기준 값은 결측이 아닌 칸에서만 계산한다(빈 칸이 0이나 ""로 섞이면 평균·최빈값이 틀어진다)
    present = [row[index] for row in rows if not is_missing(row[index])]
    if not present:
        raise AppError("NO_VALUES_TO_FILL", f"'{rule.column}' 열에 채울 기준 값이 없습니다.", 422)
    if rule.strategy == "mode":
        # cell_key로 세어 "1"과 1처럼 표현만 다른 값을 같은 값으로 본다(품질 진단과 같은 기준)
        counts = Counter(cell_key(v) for v in present)
        # 빈도가 같으면 Counter는 먼저 나온 값을 앞에 두므로 결과가 항상 같다(결정적)
        top = counts.most_common(1)[0][0]
        # 채우는 값은 키가 아닌 원래 값(타입 포함)을 쓴다
        return next(v for v in present if cell_key(v) == top)
    # 평균·중앙값은 숫자로 읽히는 칸만 쓴다. 숫자 아닌 칸이 섞여 있어도 나머지로 계산한다
    numbers = [n for n in (_as_number(v) for v in present) if n is not None]
    if not numbers:
        raise AppError(
            "NOT_NUMERIC_COLUMN",
            f"'{rule.column}' 열은 숫자가 아니어서 {rule.strategy}로 채울 수 없습니다.",
            422,
        )
    center = statistics.mean(numbers) if rule.strategy == "mean" else statistics.median(numbers)
    return _number_cell(center)


def _apply(
    rule: CleanRule, columns: list[str], rows: list[list[Cell]]
) -> tuple[list[list[Cell]], int]:
    """규칙 하나를 적용한다. (새 행 목록, 바뀐 칸 또는 지운 행 수)를 돌려준다.

    파라미터:
        rule: 적용할 규칙
        columns: 표의 열 이름
        rows: 표의 행(각 행은 열 길이와 같다)
    """
    if isinstance(rule, TrimWhitespaceRule):
        index = _column_index(columns, rule.column)
        changed = 0
        # 행을 제자리에서 고친다(복사하지 않아 큰 표도 메모리를 두 배로 쓰지 않는다)
        for row in rows:
            value = row[index]
            # 실제로 바뀐 칸만 세어 변경 내역(affected)이 과장되지 않게 한다
            if isinstance(value, str) and value != value.strip():
                row[index] = value.strip()
                changed += 1
        return rows, changed

    if isinstance(rule, RemoveDuplicatesRule):
        # 기준 열을 지정하지 않으면 모든 열이 같은 행을 중복으로 본다
        indexes = [_column_index(columns, c) for c in rule.columns or columns]
        seen: set[tuple[str, ...]] = set()
        kept: list[list[Cell]] = []
        # 처음 나온 행을 남기고 뒤에 나온 같은 행을 지운다(원래 순서 유지)
        for row in rows:
            # 품질 진단의 중복 판정과 같은 키(cell_key)를 써서 진단 건수와 지운 건수를 맞춘다
            key = tuple(cell_key(row[i]) for i in indexes)
            if key not in seen:
                seen.add(key)
                kept.append(row)
        return kept, len(rows) - len(kept)

    # 남은 규칙은 fill_missing 하나다(CleanRule 합집합)
    index = _column_index(columns, rule.column)
    targets = [row for row in rows if is_missing(row[index])]
    # 결측이 없으면 기준 값을 계산하지 않는다. 숫자 아닌 열에 mean을 걸었어도 바꿀 칸이
    # 없으면 오류 없이 0건으로 끝난다
    if targets:
        value = _fill_value(rows, index, rule)
        for row in targets:
            row[index] = value
    return rows, len(targets)


def apply_rules(
    columns: list[str], rows: list[list[Cell]], rules: list[CleanRule]
) -> tuple[list[list[Cell]], list[RuleChange]]:
    """규칙을 순서대로 적용한다. 완전히 빈 줄([])은 먼저 뺀다. 입력 행 목록은 바뀔 수 있다.

    파라미터:
        columns: 표의 열 이름
        rows: 표의 행
        rules: 적용할 규칙(순서대로)
    """
    # 열 확인을 먼저 해, 앞 규칙을 적용한 뒤에 뒤 규칙에서 실패하는 일이 없게 한다
    check_columns(columns, rules)
    # 빈 줄은 칸이 없어 row[index]가 실패하므로 규칙 적용 전에 뺀다
    current = [row for row in rows if row]
    changes: list[RuleChange] = []
    # 순서가 결과를 바꾼다(예: 공백 제거 후 중복 제거해야 " a"와 "a"가 같은 행이 된다).
    # 그래서 요청에 적힌 순서를 그대로 따른다
    for rule in rules:
        current, affected = _apply(rule, columns, current)
        changes.append(RuleChange(rule, affected))
    return current, changes


def _csv_text(value: Cell) -> str:
    """CSV에 쓸 문자열로 바꾼다(None은 빈 칸, 날짜·시각은 ISO 형식).

    파라미터:
        value: 칸 값
    """
    if value is None:
        return ""
    # str(datetime)은 "2026-10-25 06:00:00"처럼 공백이 들어가므로 ISO 형식으로 통일한다
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return str(value)


def write_csv(path: Path, columns: list[str], rows: list[list[Cell]]) -> None:
    """정제 결과를 CSV(UTF-8 BOM, 엑셀에서 바로 열림)로 쓴다.

    파라미터:
        path: 쓸 파일 경로
        columns: 열 이름
        rows: 행 목록
    """
    # BOM이 없으면 한국어 엑셀이 CP949로 열어 한글이 깨진다. newline=""은 csv 모듈이
    # 줄바꿈을 직접 쓰게 해 Windows에서 빈 줄이 끼지 않게 한다
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows([_csv_text(v) for v in row] for row in rows)
