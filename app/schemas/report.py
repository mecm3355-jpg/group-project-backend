"""AI 분석 리포트(POST /api/v1/files/{file_id}/report)의 LLM 응답 형식.

LLM 구조화 출력의 스키마로 그대로 쓰므로 모든 필드를 필수로 둔다(값이 없으면 빈 목록).
JSON 필드는 camelCase로 저장·응답한다.
"""

from typing import Literal

from app.schemas.common import ApiModel

Level = Literal["high", "medium", "low"]


class ReportIssue(ApiModel):
    """품질 문제 하나에 대한 해석."""

    issue: str
    likely_cause: str
    impact: str
    severity: Level


class ReportRecommendation(ApiModel):
    """정제·처리 권고 하나."""

    action: str
    reason: str
    priority: Level


class AiReport(ApiModel):
    """AI 분석 리포트 본문."""

    summary: str
    key_findings: list[str]
    quality_issues: list[ReportIssue]
    cleaning_recommendations: list[ReportRecommendation]
    analysis_suggestions: list[str]
    caveats: list[str]
