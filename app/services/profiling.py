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

# 부호·천 단위 쉼표(1,234)·소수(.5)를 허용한다. 쉼표는 세 자리 묶음일 때만 숫자로 본다
# ("1,2"처럼 목록일 수 있는 값을 숫자로 잘못 읽지 않게)
NUMBER_PATTERN = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$|^[+-]?\.\d+$")
# yes/no·Y/N·0/1은 다른 뜻(코드값·숫자)일 수 있어 불리언으로 보지 않는다
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
# 위 형식은 앞에서부터 차례로 시도한다. 한국 공공데이터에 흔한 -, /, . 구분자를 모두 받는다
# 열 대부분이 같은 타입인데 일부만 다르면 형식 불일치로 본다
# (100%를 요구하면 오타 하나로 숫자 열이 문자열이 되고, 불일치를 찾을 수 없다)
DOMINANT_TYPE_RATIO = 0.9
# 로그 수준은 심각한 것부터 둔다. WARNING은 WARN으로 합쳐 센다
LOG_KEYWORDS = ("FATAL", "ERROR", "WARN", "INFO", "DEBUG")
LOG_KEYWORD_PATTERN = re.compile(r"\b(FATAL|ERROR|WARN(?:ING)?|INFO|DEBUG)\b")
# 값 종류가 이 수 이하인 문자열·불리언 열을 범주형으로 보고 값별 빈도를 낸다
# (값 종류가 많은 열은 이름·주소 같은 자유 입력이라 빈도표가 의미 없고 원본 노출만 커진다)
CATEGORY_MAX_UNIQUE = 50
# 화면 차트에 한 번에 보여줄 만한 개수로 잡은 잠정값
TOP_VALUES = 10
HISTOGRAM_BINS = 10
# TXT 핵심어 빈도: 상위 개수와 형태소 분석할 최대 줄 수(큰 파일에서 시간이 너무 걸리지 않게)
TOP_KEYWORDS = 20
KEYWORD_MAX_LINES = 20_000
# 중복 줄은 글자 있는 줄 앞 20만 줄까지만 본다(줄마다 해시를 들고 있어 메모리가 줄 수만큼 는다)
DUPLICATE_MAX_LINES = 200_000
# 품질 점수 가중치(합 1). 잠정으로 네 지표를 같게 둔다
# (어느 지표를 더 무겁게 볼지는 아직 정하지 않았다. 정해지면 여기만 바꾼다)
QUALITY_WEIGHTS = {"completeness": 0.25, "uniqueness": 0.25, "validity": 0.25, "consistency": 0.25}
# 화면에 점수 산식을 그대로 보여 주려고 결과에 함께 저장하는 설명 문구
SCORE_BASIS = (
    "점수 = 완전성 25% + 유일성 25% + 유효성 25% + 일관성 25% (각 0~100, 소수점 버림, 잠정 기준)"
)


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
    # float()에 바로 넣지 않는다: "nan", "inf", "1e5" 같은 문자열도 숫자로 받아 버린다
    if not NUMBER_PATTERN.match(stripped):
        return None
    return float(stripped.replace(",", ""))


def parse_datetime(text: str) -> datetime | None:
    """문자열이 날짜(시각)면 값을, 아니면 None을 돌려준다.

    파라미터:
        text: 칸 문자열
    """
    stripped = text.strip()
    # 앞부분이 날짜 모양이 아니면 형식 10개를 다 시도하지 않고 바로 끝낸다
    if not DATE_PREFIX.match(stripped):
        return None
    # 맞는 형식을 처음 찾으면 그 값을 쓴다. 어느 것도 맞지 않으면(예: 2026-13-40) 날짜 아님
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
    # 엑셀은 칸 타입을 이미 알려 주므로 그대로 쓴다. bool은 int의 하위 타입이라 먼저 본다
    if isinstance(value, bool):
        return "boolean", None
    if isinstance(value, int | float):
        return "number", float(value)
    if isinstance(value, datetime):
        return "date", value
    # 날짜만 있는 값은 최솟값·최댓값을 datetime과 함께 비교하려고 자정으로 맞춘다
    if isinstance(value, date):
        return "date", datetime.combine(value, time())
    # 시각만 있는 값은 날짜 통계(최솟값·최댓값)에 섞을 수 없어 문자열로 둔다
    if isinstance(value, time):
        return "string", None
    # CSV처럼 모두 문자열인 값은 숫자 → 불리언 → 날짜 순으로 해석해 본다
    # (싼 검사부터 해서 대부분의 칸이 strptime까지 가지 않게)
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
    # 날짜는 str()이 환경에 따라 달라질 수 있어 ISO 형식으로 고정한다
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    # " 서울"과 "서울"을 같은 값으로 본다(공백 문제는 whitespace 진단이 따로 알린다)
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
        # 결측은 타입·고유값 집계에 넣지 않는다(빈칸이 string으로 세어져 타입 판정을 흐리지 않게)
        if is_missing(value):
            self.missing += 1
            return
        # 앞뒤 공백은 정제 규칙 trim_whitespace로 고칠 수 있는 문제라 따로 센다
        if isinstance(value, str) and value != value.strip():
            self.whitespace += 1
        self.value_counts[cell_key(value)] += 1
        kind, parsed = classify(value)
        self.type_counts[kind] = self.type_counts.get(kind, 0) + 1
        # 통계(최솟값·최댓값·평균·분포)용 값을 모은다. 열 타입은 끝까지 본 뒤에 정해진다
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
        # 가장 많은 타입이 압도적일 때만 그 타입으로 본다. 섞여 있으면 자유 입력 문자열 열이다
        kind, count = max(self.type_counts.items(), key=lambda item: item[1])
        return kind if count / total >= DOMINANT_TYPE_RATIO else "string"

    def mismatched(self) -> int:
        """정해진 타입과 다른 값의 수(형식 불일치). string 열은 0.

        파라미터: 없음
        """
        kind = self.inferred_type()
        # 문자열 열에는 어떤 값이든 들어갈 수 있어 "형식이 다른 값"이 없다
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
    # 소수 넷째 자리면 평균·구간 경계를 읽기에 충분하고 부동소수 꼬리(0.30000000000000004)를 없앤다
    rounded = round(value, 4)
    # 12500.0 대신 12500으로 내보낸다(설계 파일 예시의 statistics 형식)
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
    # 값 종류가 구간 수보다 적으면 그 수만큼만 나눈다(빈 구간이 줄줄이 생기지 않게)
    bins = 1 if low == high else min(HISTOGRAM_BINS, len(set(numbers)))
    width = (high - low) / bins
    counts = [0] * bins
    # 최댓값은 계산상 bins번째 구간으로 넘어가므로 마지막 구간에 넣는다
    for number in numbers:
        index = bins - 1 if width == 0 else min(int((number - low) / width), bins - 1)
        counts[index] += 1
    # 마지막 구간 끝은 low + width * bins 대신 high를 써서 부동소수 오차로 최댓값보다 작아지지 않게
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


def without_raw_values(analysis: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """분석 결과에서 원본 값이 그대로 든 항목(열별 topValues, 텍스트 topKeywords)을 뺀다.

    AI 리포트·열 매핑 추천처럼 외부 LLM에 보내는 곳에서 쓴다. 개수·숫자 통계는 남긴다.

    파라미터:
        analysis: processing_results.analysis
    """
    # 핵심어는 민원 본문에서 뽑은 단어라 사람 이름·주소가 섞일 수 있다
    result = {k: v for k, v in analysis.items() if k != "topKeywords"}
    statistics = analysis.get("statistics")
    if isinstance(statistics, dict):
        trimmed: dict[str, JsonValue] = {}
        for name, stats in statistics.items():
            if isinstance(stats, dict):
                # topValues는 칸 값 그대로다(예: 민원인 이름·전화번호 열의 빈도)
                stats = {k: v for k, v in stats.items() if k != "topValues"}
                # 빈도만 있던 범주형 열은 빈 객체로 남기지 않고 아예 뺀다(토큰 절약)
                if not stats:
                    continue
            trimmed[name] = stats
        result["statistics"] = trimmed
    return result


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
    # 통합 파일은 merge_handler가 이 두 열을 맨 앞에 붙여 만든다. 열 이름으로 판별하면
    # 별도 표시 필드 없이(설계 파일에 필드 추가 금지) 알아볼 수 있다
    return tuple(columns[: len(SOURCE_COLUMNS)]) == SOURCE_COLUMNS


def profile_table(table: Table) -> Profile:
    """표 전체를 한 번 돌며 품질 문제와 열별 분석·통계를 계산한다.

    품질 지표(0~100, 잠정):
    - 완전성(completeness): 채워진 칸 비율
    - 유일성(uniqueness): 1 − 중복 행 비율
    - 유효성(validity): 1 − 열 타입과 다른 값 비율(채워진 칸 기준)
    - 일관성(consistency): 1 − 열 개수가 머리글과 다른 행 비율
    품질 점수 = 네 지표의 가중 평균(QUALITY_WEIGHTS, 잠정 25%씩)의 버림.
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
    # 큰 파일도 한 번만 읽도록 열별 집계를 미리 만들어 두고 행을 흘려보내며 센다
    stats = [ColumnStats(name) for name in table.columns]
    seen_rows: set[tuple[str, ...]] = set()
    row_count = duplicates = empty_rows = 0
    merged = is_merged(table.columns)
    # 출처파일ID·원본행은 행마다 달라 그대로 비교하면 중복이 절대 나오지 않는다
    compare_from = len(SOURCE_COLUMNS) if merged else 0
    # 통합 파일: 출처별·열별 (결측 수, 값 있는 수)
    group_missing: dict[str, list[int]] = {}
    group_filled: dict[str, list[int]] = {}

    for row in table.rows:
        # 빈 행은 따로 세고 결측·중복 계산에서 뺀다(빈 행끼리 중복으로 잡히지 않게)
        if not row or all(is_missing(v) for v in row):
            empty_rows += 1
            continue
        row_count += 1
        # 중복 행: 앞에 나온 행과 칸 값(공백 무시)이 모두 같으면 센다. 첫 행은 원본으로 둔다
        key = tuple(cell_key(v) for v in row[compare_from:])
        if key in seen_rows:
            duplicates += 1
        else:
            seen_rows.add(key)
        # 통합 파일은 출처(첫 열 = 출처파일ID)별로 열마다 값이 있는지 따로 센다.
        # 끝까지 봐야 "이 출처는 이 열을 아예 안 준다"를 알 수 있어 일단 모아 둔다
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

    # 어떤 출처에서 한 칸도 채워지지 않은 열은 그 출처 원본에 없던 열이다.
    # 이를 결측으로 치면 데이터를 합치기만 해도 완전성이 떨어지므로 "해당 없음"으로 옮긴다
    for group, missing_counts in group_missing.items():
        for index, column in enumerate(stats):
            if group_filled[group][index] == 0:
                column.not_applicable += missing_counts[index]
                column.missing -= missing_counts[index]

    # 열별 문제: 결측·앞뒤 공백·형식 불일치. type 이름은 설계 파일 예시(missing_values 등)를 따른다
    issues: list[QualityIssue] = []
    for column in stats:
        # 결측 비율의 분모는 해당 없음을 뺀 행 수(값이 있어야 하는 칸만)
        applicable = row_count - column.not_applicable
        if column.missing and applicable:
            issues.append(
                QualityIssue(
                    type="missing_values",
                    # 30% 이상 비면 열을 분석에 쓰기 어렵고, 5% 미만은 흔한 누락 수준(잠정 기준)
                    severity=_ratio_severity(column.missing / applicable, 0.3, 0.05),
                    count=column.missing,
                    location=column.name,
                    message=f"{column.name} 필드에 결측치가 있습니다.",
                )
            )
        # 공백은 값의 뜻을 바꾸지 않고 자동 정제로 쉽게 고쳐지므로 항상 low
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
        # 숫자 열의 "없음" 같은 값은 통계를 틀리게 하므로 사람이 봐야 해서 medium
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
    # 표 전체 문제: 중복 행·열 개수 불일치·빈 행·머리글 문제
    if duplicates:
        issues.append(
            QualityIssue(
                type="duplicates",
                # 중복은 결측보다 집계를 직접 부풀리므로 high 기준을 20%로 더 낮게 잡는다(잠정)
                severity=_ratio_severity(duplicates / row_count, 0.2, 0.05),
                count=duplicates,
                location=None,
                message=f"중복 행 {duplicates}개가 발견되었습니다.",
            )
        )
    # 열 개수가 어긋난 행은 칸이 밀려 다른 열 값이 섞였을 수 있다
    # (표 파서가 None 채우기·자르기로 길이를 맞추며 센 수)
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
    # 같은 이름 열은 정제 규칙의 column 지정이 모호해지므로 medium
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

    # 지표 계산용 분모·분자. 해당 없음 칸은 처음부터 셀 수에서 빼 완전성에 영향을 주지 않는다
    cells = sum(row_count - c.not_applicable for c in stats)
    filled = cells - sum(c.missing for c in stats)
    # 일관성 분모는 빈 행도 포함한다(ragged_row_count가 빈 칸만 있는 행까지 포함해 센 수라서)
    total_rows = row_count + empty_rows
    # 유효성은 채워진 칸 중 형식이 맞는 비율. 결측은 완전성에서 이미 깎였으니 두 번 깎지 않는다
    mismatched = sum(c.mismatched() for c in stats)
    # 안전장치: 어긋난 행 수가 전체 행 수를 넘어 지표가 음수가 되는 일을 막는다
    ragged = min(table.ragged_row_count, total_rows)
    # 지표별 (좋은 쪽 수, 전체 수). 전체가 0이면 그 지표는 만점
    parts = {
        "completeness": (filled, cells),
        "uniqueness": (row_count - duplicates, row_count),
        "validity": (filled - mismatched, filled),
        "consistency": (total_rows - ragged, total_rows),
    }
    metrics = {name: _percent(good, whole) for name, (good, whole) in parts.items()}
    # 점수는 반올림 전 비율로 계산한다(반올림한 지표를 더하면 99.95가 100이 될 수 있다)
    score = sum(
        QUALITY_WEIGHTS[name] * (100 * good / whole if whole else 100.0)
        for name, (good, whole) in parts.items()
    )

    # 열 정보(analysis.columns)는 설계 파일 예시의 name·inferredType·missingCount·uniqueCount 형식
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
        # 통계는 열 타입에 맞는 것만 낸다. 형식이 다른 값(숫자 열의 글자)은 numbers에 없다
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

    # encoding은 CSV, sheet는 엑셀에만 있으므로 있을 때만 넣는다
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
    # 버림: 99.9점을 100점으로 올려 "문제 없음"처럼 보이지 않게 한다
    quality = QualityResult(
        score=math.floor(score), issues=issues, metrics=metrics, score_basis=SCORE_BASIS
    )
    return Profile(quality, analysis)


def profile_text(text: TextLines) -> Profile:
    """텍스트 전체를 한 번 돌며 줄 수·빈 줄 수·중복 줄·끝 공백 줄을 센다.

    로그는 수준(ERROR 등)별 줄 수를, TXT는 명사 핵심어 빈도 상위 20개(topKeywords)를 센다.
    핵심어는 글자 있는 줄 앞 20,000줄, 중복 줄은 앞 200,000줄까지만 보며, 잘랐으면
    keywordLineCount·duplicateCheckLineCount에 본 줄 수를 남긴다.

    파라미터:
        text: open_text로 연 텍스트(lines를 이 함수가 끝까지 읽는다)
    """
    line_count = empty = duplicate = trailing = keyword_lines = checked_lines = 0
    counts = dict.fromkeys(LOG_KEYWORDS, 0)
    keywords: Counter[str] = Counter()
    seen: set[int] = set()  # 줄 내용의 해시(메모리를 아끼려고 문자열 대신)
    is_log = text.format == "log"
    for line in text.lines:
        line_count += 1
        stripped = line.strip()
        # 빈 줄은 중복·핵심어 대상에서 뺀다(빈 줄끼리 중복으로 세지 않게)
        if not stripped:
            empty += 1
            continue
        # 앞 공백은 들여쓰기일 수 있어 끝 공백만 문제로 본다
        if line != line.rstrip():
            trailing += 1
        if checked_lines < DUPLICATE_MAX_LINES:
            checked_lines += 1
            key = hash(stripped)
            if key in seen:
                duplicate += 1
            else:
                seen.add(key)
        # 로그는 줄마다 처음 나온 수준 하나만 센다(메시지 속 "ERROR" 단어를 두 번 세지 않게)
        if is_log:
            if match := LOG_KEYWORD_PATTERN.search(line):
                keyword = "WARN" if match.group(1).startswith("WARN") else match.group(1)
                counts[keyword] += 1
        # 형태소 분석은 줄당 비용이 커서 앞부분만 본다. 핵심어 경향을 보기엔 충분하다
        elif keyword_lines < KEYWORD_MAX_LINES:
            keyword_lines += 1
            keywords.update(noun_tokens(stripped))

    # 텍스트 문제는 모두 내용 손실이 없는 서식 문제라 low. 텍스트에는 점수를 매기지 않는다
    # (설계 파일 TXT·LOG 예시에 score가 없다)
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
    # 0건인 수준은 빼서 설계 파일 예시처럼 나온 수준만 보여 준다
    if is_log:
        analysis["keywordCounts"] = {k: v for k, v in counts.items() if v}
    else:
        analysis["topKeywords"] = [
            {"keyword": word, "count": count} for word, count in keywords.most_common(TOP_KEYWORDS)
        ]
        # 잘랐을 때만 남겨 화면이 "일부만 본 결과"임을 알릴 수 있게 한다
        if keyword_lines < line_count - empty:
            analysis["keywordLineCount"] = keyword_lines
    if checked_lines < line_count - empty:
        analysis["duplicateCheckLineCount"] = checked_lines
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
    # lines를 끝까지 읽어야 PDF 페이지별 글자 유무·OCR 집계가 document에 채워진다
    for line in document.lines:
        line_count += 1
        characters += len(line.strip())

    # 이미지인데 OCR 엔진이 없으면 글자를 읽지 못한 것이지 글자가 없는 것이 아니다
    unread_image = document.image is not None and document.ocr_engine is None
    issues: list[QualityIssue] = []
    # 아래 세 경우는 원인이 겹치므로 가장 구체적인 하나만 알린다
    # 이미지 OCR 미설정은 설치하면 해결되는 환경 문제라 low
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
    # 일부 페이지만 비면 표지·도면일 수 있어 medium, 전부 비면 스캔본이라 분석이 불가해 high
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

    # OCR 오인식은 내용을 바꿀 수 있어 사람이 확인하도록 알린다. 위치는 앞 10페이지만 적어
    # 메시지가 너무 길어지지 않게 한다
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
    # method: 글자를 어떻게 얻었는지. 글자 층이 있는 페이지와 OCR한 페이지가 섞이면 text+ocr
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
    # 페이지 수가 있으면 PDF, 없으면 DOCX 같은 흐름형 문서라 표 개수를 남긴다
    if document.page_count is not None:
        analysis["pageCount"] = document.page_count
        analysis["pagesWithoutText"] = document.pages_without_text
    else:
        analysis["tableCount"] = document.table_count
    analysis.update({"lineCount": line_count, "characterCount": characters})
    return Profile(QualityResult(issues=issues), analysis)
