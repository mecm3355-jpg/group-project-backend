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
from app.schemas.result import ResultDocument

# 값 종류가 이 수 이하인 문자열 열만 최빈값 채우기를 추천한다(자유 서술 열 제외)
MODE_MAX_UNIQUE = 50


@dataclass(frozen=True)
class Suggestion:
    """추천 규칙과 추천 이유."""

    rule: CleanRule
    reason: str


def _column_info(result: ResultDocument) -> dict[str, tuple[str, int]]:
    """분석 결과의 열 정보를 열 이름 → (추정 타입, 고유값 수)로 꺼낸다.

    파라미터:
        result: 분석 결과 문서
    """
    info: dict[str, tuple[str, int]] = {}
    columns = result.analysis.get("columns")
    if not isinstance(columns, list):
        return info
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
    """품질 진단 결과의 문제마다 해결 규칙을 추천한다.

    - whitespace → trim_whitespace
    - duplicates → remove_duplicates(모든 열 기준)
    - missing_values → 숫자 열은 median, 값 종류가 적은 문자열·불리언 열은 mode로 fill_missing

    파라미터:
        result: 최신 분석(analysis) 결과 문서
    """
    columns = _column_info(result)
    suggestions: list[Suggestion] = []
    for issue in result.quality.issues:
        column = issue.location
        if issue.type == "whitespace" and column:
            suggestions.append(
                Suggestion(
                    TrimWhitespaceRule(type="trim_whitespace", column=column),
                    f"{column} 필드에 앞뒤 공백이 있는 값이 {issue.count}개 있습니다.",
                )
            )
        elif issue.type == "duplicates":
            suggestions.append(
                Suggestion(
                    RemoveDuplicatesRule(type="remove_duplicates"),
                    f"모든 열이 같은 중복 행이 {issue.count}개 있습니다.",
                )
            )
        elif issue.type == "missing_values" and column in columns:
            kind, unique = columns[column]
            if kind == "number":
                rule = FillMissingRule(type="fill_missing", column=column, strategy="median")
                reason = f"숫자 열 {column}의 결측치 {issue.count}개를 중앙값으로 채웁니다."
            elif kind in ("string", "boolean") and unique <= MODE_MAX_UNIQUE:
                rule = FillMissingRule(type="fill_missing", column=column, strategy="mode")
                reason = f"{column}의 결측치 {issue.count}개를 가장 많이 나온 값으로 채웁니다."
            else:
                continue
            suggestions.append(Suggestion(rule, reason))
    return suggestions
