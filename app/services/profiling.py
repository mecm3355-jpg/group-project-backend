"""추출한 데이터의 품질 진단과 분석 결과 계산.

결과는 processing_results의 quality·analysis 형식(설계 파일 예시)을 따른다.
품질 점수와 심각도 기준은 잠정값이다.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time

from pydantic import JsonValue

from app.processors.tabular import Cell, Table
from app.processors.text import TextLines
from app.schemas.dataset import SOURCE_COLUMNS
from app.schemas.result import QualityIssue, QualityResult, Severity

NUMBER_PATTERN = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$|^[+-]?\.\d+$")
BOOLEAN_VALUES = {"true", "false"}
# 날짜처럼 생긴 값만 strptime으로 확인한다(모든 문자열에 여러 형식을 시도하면 느리다)
DATE_PREFIX = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")
DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y.%m.%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)
# 열 대부분이 같은 타입인데 일부만 다르면 형식 불일치로 본다
DOMINANT_TYPE_RATIO = 0.9
LOG_KEYWORDS = ("FATAL", "ERROR", "WARN", "INFO", "DEBUG")
LOG_KEYWORD_PATTERN = re.compile(r"\b(FATAL|ERROR|WARN(?:ING)?|INFO|DEBUG)\b")


def is_missing(value: Cell) -> bool:
    """결측값인지 본다. None과 공백뿐인 문자열을 결측으로 본다.

    파라미터:
        value: 칸 값
    """
    return value is None or (isinstance(value, str) and not value.strip())


def parse_number(text: str) -> float | None:
    """문자열이 숫자면 값을, 아니면 None을 돌려준다. 천 단위 쉼표를 허용한다.

    파라미터:
        text: 칸 문자열
    """
    stripped = text.strip()
    if not NUMBER_PATTERN.match(stripped):
        return None
    return float(stripped.replace(",", ""))


def parse_datetime(text: str) -> datetime | None:
    """문자열이 날짜(시각)면 값을, 아니면 None을 돌려준다.

    파라미터:
        text: 칸 문자열
    """
    stripped = text.strip()
    if not DATE_PREFIX.match(stripped):
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(stripped, fmt)
        except ValueError:
            continue
    return None


def classify(value: Cell) -> tuple[str, float | datetime | None]:
    """값의 타입(number, boolean, date, string)과 통계용 값(숫자·날짜)을 돌려준다.

    파라미터:
        value: 결측이 아닌 칸 값
    """
    if isinstance(value, bool):
        return "boolean", None
    if isinstance(value, int | float):
        return "number", float(value)
    if isinstance(value, datetime):
        return "date", value
    if isinstance(value, date):
        return "date", datetime.combine(value, time())
    if isinstance(value, time):
        return "string", None
    text = str(value)
    number = parse_number(text)
    if number is not None:
        return "number", number
    if text.strip().lower() in BOOLEAN_VALUES:
        return "boolean", None
    moment = parse_datetime(text)
    if moment is not None:
        return "date", moment
    return "string", None


def cell_key(value: Cell) -> str:
    """고유값·중복 행 비교에 쓰는 문자열. 앞뒤 공백은 무시한다.

    파라미터:
        value: 칸 값
    """
    if value is None:
        return ""
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return str(value).strip()


@dataclass
class ColumnStats:
    """열 하나의 집계 중간값."""

    name: str
    missing: int = 0
    # 통합 파일에서 그 열을 제공하지 않는 출처의 빈칸(결측이 아니라 해당 없음)
    not_applicable: int = 0
    whitespace: int = 0
    uniques: set[str] = field(default_factory=set)
    type_counts: dict[str, int] = field(default_factory=dict)
    numbers: list[float] = field(default_factory=list)
    dates: list[datetime] = field(default_factory=list)

    def add(self, value: Cell) -> None:
        """칸 값 하나를 집계에 더한다.

        파라미터:
            value: 칸 값
        """
        if is_missing(value):
            self.missing += 1
            return
        if isinstance(value, str) and value != value.strip():
            self.whitespace += 1
        self.uniques.add(cell_key(value))
        kind, parsed = classify(value)
        self.type_counts[kind] = self.type_counts.get(kind, 0) + 1
        if isinstance(parsed, datetime):
            self.dates.append(parsed)
        elif parsed is not None:
            self.numbers.append(parsed)

    def inferred_type(self) -> str:
        """열 타입을 정한다. 90% 이상이 같은 타입이면 그 타입, 아니면 string, 값이 없으면 empty.

        파라미터: 없음
        """
        total = sum(self.type_counts.values())
        if total == 0:
            return "empty"
        kind, count = max(self.type_counts.items(), key=lambda item: item[1])
        return kind if count / total >= DOMINANT_TYPE_RATIO else "string"

    def mismatched(self) -> int:
        """정해진 타입과 다른 값의 수(형식 불일치). string 열은 0.

        파라미터: 없음
        """
        kind = self.inferred_type()
        if kind in ("string", "empty"):
            return 0
        return sum(self.type_counts.values()) - self.type_counts.get(kind, 0)


def _ratio_severity(ratio: float, high: float, medium: float) -> Severity:
    """비율로 심각도를 정한다.

    파라미터:
        ratio: 문제 비율(0~1)
        high: 이 이상이면 high
        medium: 이 이상이면 medium, 아니면 low
    """
    if ratio >= high:
        return "high"
    if ratio >= medium:
        return "medium"
    return "low"


def _round(value: float) -> float | int:
    """통계 값을 보기 좋게 반올림한다(정수면 int).

    파라미터:
        value: 실수 값
    """
    rounded = round(value, 4)
    return int(rounded) if rounded.is_integer() else rounded


@dataclass
class Profile:
    """프로파일 결과. quality·analysis는 processing_results에 그대로 저장한다."""

    quality: QualityResult
    analysis: dict[str, JsonValue]


def is_merged(columns: list[str]) -> bool:
    """통합 데이터셋 파일(첫 열이 출처파일ID·원본행)인지 본다.

    파라미터:
        columns: 표의 열 이름
    """
    return tuple(columns[: len(SOURCE_COLUMNS)]) == SOURCE_COLUMNS


def profile_table(table: Table) -> Profile:
    """표 전체를 한 번 돌며 품질 문제와 열별 분석·통계를 계산한다.

    품질 점수 = 100 × 채워진 칸 비율 × (1 − 중복 행 비율) × (1 − 길이 다른 행 비율)의 버림 (잠정).
    버림이라 문제가 하나라도 있으면 100점이 되지 않는다.

    통합 데이터셋 파일이면:
    - 어떤 출처에서 값이 하나도 없는 열은 그 출처에는 "해당 없음"으로 보고 결측·점수에서 뺀다
      (예: 본문 열이 없는 아산 자료의 본문 칸). 열 정보에 notApplicableCount로 남긴다.
    - 중복 행은 출처파일ID·원본행을 빼고 비교한다.

    파라미터:
        table: open_table로 연 표(rows를 이 함수가 끝까지 읽는다)
    """
    stats = [ColumnStats(name) for name in table.columns]
    seen_rows: set[tuple[str, ...]] = set()
    row_count = duplicates = empty_rows = 0
    merged = is_merged(table.columns)
    compare_from = len(SOURCE_COLUMNS) if merged else 0
    # 통합 파일: 출처별·열별 (결측 수, 값 있는 수)
    group_missing: dict[str, list[int]] = {}
    group_filled: dict[str, list[int]] = {}

    for row in table.rows:
        if not row or all(is_missing(v) for v in row):
            empty_rows += 1
            continue
        row_count += 1
        key = tuple(cell_key(v) for v in row[compare_from:])
        if key in seen_rows:
            duplicates += 1
        else:
            seen_rows.add(key)
        if merged:
            group = cell_key(row[0])
            missing_counts = group_missing.setdefault(group, [0] * len(stats))
            filled_counts = group_filled.setdefault(group, [0] * len(stats))
            for index, value in enumerate(row):
                if is_missing(value):
                    missing_counts[index] += 1
                else:
                    filled_counts[index] += 1
        for column, value in zip(stats, row, strict=True):
            column.add(value)

    for group, missing_counts in group_missing.items():
        for index, column in enumerate(stats):
            if group_filled[group][index] == 0:
                column.not_applicable += missing_counts[index]
                column.missing -= missing_counts[index]

    issues: list[QualityIssue] = []
    for column in stats:
        applicable = row_count - column.not_applicable
        if column.missing and applicable:
            issues.append(
                QualityIssue(
                    type="missing_values",
                    severity=_ratio_severity(column.missing / applicable, 0.3, 0.05),
                    count=column.missing,
                    location=column.name,
                    message=f"{column.name} 필드에 결측치가 있습니다.",
                )
            )
        if column.whitespace:
            issues.append(
                QualityIssue(
                    type="whitespace",
                    severity="low",
                    count=column.whitespace,
                    location=column.name,
                    message=(
                        f"{column.name} 필드에 앞뒤 공백이 있는 값 "
                        f"{column.whitespace}개가 있습니다."
                    ),
                )
            )
        if mismatched := column.mismatched():
            issues.append(
                QualityIssue(
                    type="type_mismatch",
                    severity="medium",
                    count=mismatched,
                    location=column.name,
                    message=(
                        f"{column.name} 필드에 {column.inferred_type()} 형식이 아닌 값 "
                        f"{mismatched}개가 있습니다."
                    ),
                )
            )
    if duplicates:
        issues.append(
            QualityIssue(
                type="duplicates",
                severity=_ratio_severity(duplicates / row_count, 0.2, 0.05),
                count=duplicates,
                location=None,
                message=f"중복 행 {duplicates}개가 발견되었습니다.",
            )
        )
    if table.ragged_row_count:
        issues.append(
            QualityIssue(
                type="row_length_mismatch",
                severity="medium",
                count=table.ragged_row_count,
                location=None,
                message=f"열 개수가 머리글과 다른 행 {table.ragged_row_count}개가 발견되었습니다.",
            )
        )
    if empty_rows:
        issues.append(
            QualityIssue(
                type="empty_rows",
                severity="low",
                count=empty_rows,
                location=None,
                message=f"빈 행 {empty_rows}개가 발견되었습니다.",
            )
        )
    if table.empty_header_count:
        issues.append(
            QualityIssue(
                type="empty_header",
                severity="low",
                count=table.empty_header_count,
                location=None,
                message=f"이름이 없는 열 {table.empty_header_count}개가 발견되었습니다.",
            )
        )
    if table.duplicate_headers:
        names = ", ".join(sorted(set(table.duplicate_headers)))
        issues.append(
            QualityIssue(
                type="duplicate_header",
                severity="medium",
                count=len(table.duplicate_headers),
                location=names,
                message=f"이름이 같은 열이 있습니다: {names}",
            )
        )

    cells = sum(row_count - c.not_applicable for c in stats)
    filled = cells - sum(c.missing for c in stats)
    total_rows = row_count + empty_rows
    score = 100.0
    if cells:
        score *= filled / cells
        score *= 1 - duplicates / row_count
        score *= 1 - min(table.ragged_row_count, total_rows) / total_rows

    columns: list[JsonValue] = []
    statistics: dict[str, JsonValue] = {}
    for column in stats:
        kind = column.inferred_type()
        info: dict[str, JsonValue] = {
            "name": column.name,
            "inferredType": kind,
            "missingCount": column.missing,
            "uniqueCount": len(column.uniques),
        }
        if merged:
            info["notApplicableCount"] = column.not_applicable
        columns.append(info)
        if kind == "number" and column.numbers:
            statistics[column.name] = {
                "min": _round(min(column.numbers)),
                "max": _round(max(column.numbers)),
                "mean": _round(sum(column.numbers) / len(column.numbers)),
            }
        elif kind == "date" and column.dates:
            statistics[column.name] = {
                "min": min(column.dates).isoformat(),
                "max": max(column.dates).isoformat(),
            }

    analysis: dict[str, JsonValue] = {"format": table.format}
    if table.encoding is not None:
        analysis["encoding"] = table.encoding
    if table.sheet is not None:
        analysis["sheet"] = table.sheet
    analysis.update(
        {
            "rowCount": row_count,
            "columnCount": len(stats),
            "columns": columns,
            "statistics": statistics,
        }
    )
    return Profile(QualityResult(score=math.floor(score), issues=issues), analysis)


def profile_text(text: TextLines) -> Profile:
    """텍스트 전체를 한 번 돌며 줄 수·빈 줄 수를 센다. 로그는 수준(ERROR 등)별 줄 수도 센다.

    파라미터:
        text: open_text로 연 텍스트(lines를 이 함수가 끝까지 읽는다)
    """
    line_count = empty = 0
    counts = dict.fromkeys(LOG_KEYWORDS, 0)
    is_log = text.format == "log"
    for line in text.lines:
        line_count += 1
        if not line.strip():
            empty += 1
        elif is_log and (match := LOG_KEYWORD_PATTERN.search(line)):
            keyword = "WARN" if match.group(1).startswith("WARN") else match.group(1)
            counts[keyword] += 1

    issues: list[QualityIssue] = []
    if empty:
        issues.append(
            QualityIssue(
                type="empty_lines",
                severity="low",
                count=empty,
                location=None,
                message=f"빈 줄 {empty}개가 발견되었습니다.",
            )
        )
    analysis: dict[str, JsonValue] = {
        "format": text.format,
        "encoding": text.encoding,
        "lineCount": line_count,
        "emptyLineCount": empty,
    }
    if is_log:
        analysis["keywordCounts"] = {k: v for k, v in counts.items() if v}
    return Profile(QualityResult(issues=issues), analysis)
