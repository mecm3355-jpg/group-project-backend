"""추출한 데이터의 품질 진단과 분석 결과 계산.

결과는 processing_results의 quality·analysis 형식(설계 파일 예시)을 따른다.
품질 점수와 심각도 기준은 잠정값이다.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time

from pydantic import JsonValue

from app.processors.document import DocumentText
from app.processors.tabular import Cell, Table
from app.processors.text import TextLines
from app.schemas.dataset import SOURCE_COLUMNS
from app.schemas.result import QualityIssue, QualityResult, Severity
from app.services.keywords import noun_tokens

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
# 값 종류가 이 수 이하인 문자열·불리언 열을 범주형으로 보고 값별 빈도를 낸다
CATEGORY_MAX_UNIQUE = 50
TOP_VALUES = 10
HISTOGRAM_BINS = 10
# TXT 핵심어 빈도: 상위 개수와 형태소 분석할 최대 줄 수(큰 파일에서 시간이 너무 걸리지 않게)
TOP_KEYWORDS = 20
KEYWORD_MAX_LINES = 20_000
SCORE_BASIS = "점수 = 100 × 완전성 × 유일성 × 일관성 (소수점 버림, 잠정 기준)"


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
    # 고유값별 개수(cell_key 기준). 고유값 수와 범주형 빈도에 쓴다
    value_counts: Counter[str] = field(default_factory=Counter)
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
        self.value_counts[cell_key(value)] += 1
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


def _percent(part: float, whole: float) -> float:
    """비율을 0~100 백분율(소수 첫째 자리)로 바꾼다. 분모가 0이면 100.

    파라미터:
        part: 좋은 쪽의 수(예: 채워진 칸)
        whole: 전체 수
    """
    return round(100 * part / whole, 1) if whole else 100.0


def histogram(numbers: list[float]) -> list[JsonValue]:
    """숫자 목록을 같은 폭 구간(최대 10개)으로 나눠 구간별 개수를 센다.

    마지막 구간은 최댓값을 포함한다. 값이 모두 같으면 구간 하나다.

    파라미터:
        numbers: 숫자 값 목록(비어 있지 않음)
    """
    low, high = min(numbers), max(numbers)
    bins = 1 if low == high else min(HISTOGRAM_BINS, len(set(numbers)))
    width = (high - low) / bins
    counts = [0] * bins
    for number in numbers:
        index = bins - 1 if width == 0 else min(int((number - low) / width), bins - 1)
        counts[index] += 1
    return [
        {
            "start": _round(low + width * i),
            "end": _round(high if i == bins - 1 else low + width * (i + 1)),
            "count": count,
        }
        for i, count in enumerate(counts)
    ]


def top_values(counts: Counter[str]) -> list[JsonValue]:
    """많이 나온 값 상위 10개와 개수를 돌려준다.

    파라미터:
        counts: 값별 개수
    """
    return [{"value": value, "count": count} for value, count in counts.most_common(TOP_VALUES)]


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

    품질 지표(0~100, 잠정):
    - 완전성(completeness): 채워진 칸 비율
    - 유일성(uniqueness): 1 − 중복 행 비율
    - 유효성(validity): 1 − 열 타입과 다른 값 비율(채워진 칸 기준)
    - 일관성(consistency): 1 − 열 개수가 머리글과 다른 행 비율
    품질 점수 = 100 × 완전성 × 유일성 × 일관성의 버림(잠정, 가중치 미정이라 유효성은 아직 빼 둔다).
    버림이라 문제가 하나라도 있으면 100점이 되지 않는다.

    통계: 숫자 열은 최솟값·최댓값·평균·구간별 분포(histogram), 날짜 열은 최솟값·최댓값,
    값 종류가 50개 이하인 문자열·불리언 열은 값별 빈도 상위 10개(topValues).

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
    mismatched = sum(c.mismatched() for c in stats)
    ragged = min(table.ragged_row_count, total_rows)
    metrics = {
        "completeness": _percent(filled, cells),
        "uniqueness": _percent(row_count - duplicates, row_count),
        "validity": _percent(filled - mismatched, filled),
        "consistency": _percent(total_rows - ragged, total_rows),
    }
    score = 100.0
    if cells:
        score *= filled / cells
        score *= 1 - duplicates / row_count
        score *= 1 - ragged / total_rows

    columns: list[JsonValue] = []
    statistics: dict[str, JsonValue] = {}
    for column in stats:
        kind = column.inferred_type()
        info: dict[str, JsonValue] = {
            "name": column.name,
            "inferredType": kind,
            "missingCount": column.missing,
            "uniqueCount": len(column.value_counts),
        }
        if merged:
            info["notApplicableCount"] = column.not_applicable
        columns.append(info)
        if kind == "number" and column.numbers:
            statistics[column.name] = {
                "min": _round(min(column.numbers)),
                "max": _round(max(column.numbers)),
                "mean": _round(sum(column.numbers) / len(column.numbers)),
                "histogram": histogram(column.numbers),
            }
        elif kind == "date" and column.dates:
            statistics[column.name] = {
                "min": min(column.dates).isoformat(),
                "max": max(column.dates).isoformat(),
            }
        elif kind in ("string", "boolean") and len(column.value_counts) <= CATEGORY_MAX_UNIQUE:
            statistics[column.name] = {"topValues": top_values(column.value_counts)}

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
    quality = QualityResult(
        score=math.floor(score), issues=issues, metrics=metrics, score_basis=SCORE_BASIS
    )
    return Profile(quality, analysis)


def profile_text(text: TextLines) -> Profile:
    """텍스트 전체를 한 번 돌며 줄 수·빈 줄 수·중복 줄·끝 공백 줄을 센다.

    로그는 수준(ERROR 등)별 줄 수를, TXT는 명사 핵심어 빈도 상위 20개(topKeywords)를 센다.
    핵심어는 앞 20,000줄까지만 형태소 분석하며, 잘랐으면 keywordLineCount에 분석한 줄 수를 남긴다.

    파라미터:
        text: open_text로 연 텍스트(lines를 이 함수가 끝까지 읽는다)
    """
    line_count = empty = duplicate = trailing = keyword_lines = 0
    counts = dict.fromkeys(LOG_KEYWORDS, 0)
    keywords: Counter[str] = Counter()
    seen: set[int] = set()  # 줄 내용의 해시(메모리를 아끼려고 문자열 대신)
    is_log = text.format == "log"
    for line in text.lines:
        line_count += 1
        stripped = line.strip()
        if not stripped:
            empty += 1
            continue
        if line != line.rstrip():
            trailing += 1
        key = hash(stripped)
        if key in seen:
            duplicate += 1
        else:
            seen.add(key)
        if is_log:
            if match := LOG_KEYWORD_PATTERN.search(line):
                keyword = "WARN" if match.group(1).startswith("WARN") else match.group(1)
                counts[keyword] += 1
        elif keyword_lines < KEYWORD_MAX_LINES:
            keyword_lines += 1
            keywords.update(noun_tokens(stripped))

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
    if duplicate:
        issues.append(
            QualityIssue(
                type="duplicate_lines",
                severity="low",
                count=duplicate,
                location=None,
                message=f"앞에 나온 줄과 내용이 같은 줄 {duplicate}개가 발견되었습니다.",
            )
        )
    if trailing:
        issues.append(
            QualityIssue(
                type="trailing_whitespace",
                severity="low",
                count=trailing,
                location=None,
                message=f"끝에 공백이 있는 줄 {trailing}개가 발견되었습니다.",
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
    else:
        analysis["topKeywords"] = [
            {"keyword": word, "count": count} for word, count in keywords.most_common(TOP_KEYWORDS)
        ]
        if keyword_lines < line_count - empty:
            analysis["keywordLineCount"] = keyword_lines
    return Profile(QualityResult(issues=issues), analysis)


def profile_document(document: DocumentText) -> Profile:
    """문서 전체를 한 번 돌며 글자 줄 수·글자 수를 센다. PDF는 글자 없는 페이지를 찾는다.

    OCR하지 못한 글자 없는 페이지는 "OCR 필요"로, OCR 신뢰도가 낮은 줄은 "검수 필요"로 알린다.
    이미지는 형식·가로·세로·색상 모드(image)를 남기고, OCR이 없어 글자를 읽지 않았으면
    method를 none으로 두고 ocr_not_available(low)로 알린다.

    파라미터:
        document: open_document로 연 문서(lines를 이 함수가 끝까지 읽는다)
    """
    line_count = characters = 0
    for line in document.lines:
        line_count += 1
        characters += len(line.strip())

    unread_image = document.image is not None and document.ocr_engine is None
    issues: list[QualityIssue] = []
    if unread_image:
        issues.append(
            QualityIssue(
                type="ocr_not_available",
                severity="low",
                count=1,
                location=None,
                message="OCR이 설정되지 않아 이미지 속 글자는 읽지 않았습니다.",
            )
        )
    elif document.pages_without_text:
        all_pages = document.pages_without_text == document.page_count
        issues.append(
            QualityIssue(
                type="no_text_layer",
                severity="high" if all_pages else "medium",
                count=document.pages_without_text,
                location=None,
                message=(
                    f"글자가 없는 페이지 {document.pages_without_text}개가 발견되었습니다. "
                    "스캔본이면 OCR이 필요합니다."
                ),
            )
        )
    elif line_count == 0:
        issues.append(
            QualityIssue(
                type="no_text",
                severity="high",
                count=1,
                location=None,
                message="문서에서 추출된 글자가 없습니다.",
            )
        )

    if document.ocr_low_confidence_lines:
        pages = ", ".join(str(p) for p in document.ocr_low_confidence_pages[:10])
        issues.append(
            QualityIssue(
                type="low_ocr_confidence",
                severity="medium",
                count=document.ocr_low_confidence_lines,
                location=f"page {pages}",
                message=(
                    f"OCR 신뢰도가 {document.low_confidence} 미만인 줄 "
                    f"{document.ocr_low_confidence_lines}개가 있습니다. 사람이 검수해야 합니다."
                ),
            )
        )

    analysis: dict[str, JsonValue] = {"format": document.format}
    if document.ocr_pages:
        analysis["method"] = "ocr" if document.ocr_pages == document.page_count else "text+ocr"
        analysis["ocr"] = {
            "engine": document.ocr_engine,
            "pages": document.ocr_pages,
            "lineCount": document.ocr_line_count,
            "meanConfidence": round(document.ocr_confidence_total / document.ocr_line_count, 4)
            if document.ocr_line_count
            else None,
            "lowConfidenceLines": document.ocr_low_confidence_lines,
        }
    else:
        analysis["method"] = "none" if unread_image else "text"
    if document.image is not None:
        analysis["image"] = {
            "format": document.image.format,
            "width": document.image.width,
            "height": document.image.height,
            "mode": document.image.mode,
        }
    if document.page_count is not None:
        analysis["pageCount"] = document.page_count
        analysis["pagesWithoutText"] = document.pages_without_text
    else:
        analysis["tableCount"] = document.table_count
    analysis.update({"lineCount": line_count, "characterCount": characters})
    return Profile(QualityResult(issues=issues), analysis)
