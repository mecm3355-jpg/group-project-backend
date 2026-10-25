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
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return parse_number(value)
    return None


def _number_cell(value: float) -> Cell:
    """채울 숫자를 정수면 int로, 아니면 소수 4자리로 반올림한 float로 만든다.

    파라미터:
        value: 계산한 값
    """
    rounded = round(value, 4)
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
    present = [row[index] for row in rows if not is_missing(row[index])]
    if not present:
        raise AppError("NO_VALUES_TO_FILL", f"'{rule.column}' 열에 채울 기준 값이 없습니다.", 422)
    if rule.strategy == "mode":
        counts = Counter(cell_key(v) for v in present)
        top = counts.most_common(1)[0][0]
        return next(v for v in present if cell_key(v) == top)
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
        for row in rows:
            value = row[index]
            if isinstance(value, str) and value != value.strip():
                row[index] = value.strip()
                changed += 1
        return rows, changed

    if isinstance(rule, RemoveDuplicatesRule):
        indexes = [_column_index(columns, c) for c in rule.columns or columns]
        seen: set[tuple[str, ...]] = set()
        kept: list[list[Cell]] = []
        for row in rows:
            key = tuple(cell_key(row[i]) for i in indexes)
            if key not in seen:
                seen.add(key)
                kept.append(row)
        return kept, len(rows) - len(kept)

    index = _column_index(columns, rule.column)
    targets = [row for row in rows if is_missing(row[index])]
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
    check_columns(columns, rules)
    current = [row for row in rows if row]
    changes: list[RuleChange] = []
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
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows([_csv_text(v) for v in row] for row in rows)
