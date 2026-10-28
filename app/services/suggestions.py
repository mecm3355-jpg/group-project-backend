"""정제 규칙 추천(POST /files/{file_id}/cleaning-suggestions).

명세서의 기능명은 "AI 정제 규칙 추천"이지만 LLM 연동 전까지는 최신 품질 진단 결과로 규칙을 추천한다.
추천은 제안일 뿐이며, 사용자가 고른 규칙만 /clean으로 적용한다.
"""

from dataclasses import dataclass

from app.schemas.cleaning import (
    CleanRule,
    FillMissingRule,
    RemoveDuplicatesRule,
    TrimWhitespaceRule,
)
from app.schemas.dataset import SOURCE_COLUMNS
from app.schemas.result import ResultDocument
from app.services.profiling import is_merged

# 값 종류가 이 수 이하인 문자열 열만 최빈값 채우기를 추천한다(자유 서술 열 제외)
MODE_MAX_UNIQUE = 50


@dataclass(frozen=True)
class Suggestion:
    """추천 규칙, 추천 이유, 예상 변경 건수(바뀌는 칸 수 또는 지워지는 행 수)."""

    rule: CleanRule
    reason: str
    expected_changes: int


def _column_info(result: ResultDocument) -> dict[str, tuple[str, int]]:
    """분석 결과의 열 정보를 열 이름 → (추정 타입, 고유값 수)로 꺼낸다.

    파라미터:
        result: 분석 결과 문서
    """
    info: dict[str, tuple[str, int]] = {}
    # analysis는 형식마다 모양이 다른 자유 객체라, 표 결과가 아니면(columns 없음) 빈 정보로 둔다
    columns = result.analysis.get("columns")
    if not isinstance(columns, list):
        return info
    # 예전 버전 결과처럼 필드가 빠진 항목은 추천 근거로 못 쓰므로 조용히 건너뛴다
    for column in columns:
        if not isinstance(column, dict):
            continue
        name, kind, unique = (
            column.get("name"),
            column.get("inferredType"),
            column.get("uniqueCount"),
        )
        if isinstance(name, str) and isinstance(kind, str) and isinstance(unique, int):
            info[name] = (kind, unique)
    return info


def suggest_rules(result: ResultDocument) -> list[Suggestion]:
    """품질 진단 결과의 문제마다 해결 규칙을 추천한다. 예상 변경 건수는 그 문제의 count다.

    - whitespace → trim_whitespace
    - duplicates → remove_duplicates(모든 열 기준)
    - missing_values → 숫자 열은 median, 값 종류가 적은 문자열·불리언 열은 mode로 fill_missing

    파라미터:
        result: 최신 분석(analysis) 결과 문서
    """
    columns = _column_info(result)
    suggestions: list[Suggestion] = []
    # 문제 하나에 규칙 하나를 짝지어, 사용자가 어떤 문제를 고치는 규칙인지 바로 알게 한다
    for issue in result.quality.issues:
        # location은 문제가 난 열 이름(행 전체 문제면 None, schema.md quality.issues 예시)
        column = issue.location
        if issue.type == "whitespace" and column:
            suggestions.append(
                Suggestion(
                    TrimWhitespaceRule(type="trim_whitespace", column=column),
                    f"{column} 필드에 앞뒤 공백이 있는 값이 {issue.count}개 있습니다.",
                    issue.count,
                )
            )
        elif issue.type == "duplicates":
            names = list(columns)
            if is_merged(names):
                # 통합 파일은 출처파일ID·원본행이 행마다 달라 그 둘을 빼고 비교한다
                suggestions.append(
                    Suggestion(
                        RemoveDuplicatesRule(
                            type="remove_duplicates", columns=names[len(SOURCE_COLUMNS) :]
                        ),
                        f"출처 정보를 뺀 모든 열이 같은 중복 행이 {issue.count}개 있습니다.",
                        issue.count,
                    )
                )
            else:
                suggestions.append(
                    Suggestion(
                        RemoveDuplicatesRule(type="remove_duplicates"),
                        f"모든 열이 같은 중복 행이 {issue.count}개 있습니다.",
                        issue.count,
                    )
                )
        elif issue.type == "missing_values" and column in columns:
            kind, unique = columns[column]
            # 숫자는 평균 대신 중앙값: 이상치 몇 개에 채울 값이 끌려가지 않는다
            if kind == "number":
                rule = FillMissingRule(type="fill_missing", column=column, strategy="median")
                reason = f"숫자 열 {column}의 결측치 {issue.count}개를 중앙값으로 채웁니다."
            elif kind in ("string", "boolean") and unique <= MODE_MAX_UNIQUE:
                rule = FillMissingRule(type="fill_missing", column=column, strategy="mode")
                reason = f"{column}의 결측치 {issue.count}개를 가장 많이 나온 값으로 채웁니다."
            else:
                # 날짜 열·자유 서술 열은 그럴듯한 대표값이 없어 추천하지 않는다(사용자가 직접 판단)
                continue
            suggestions.append(Suggestion(rule, reason, issue.count))
    return suggestions
