"""AI 분석 리포트(POST /api/v1/files/{file_id}/report)의 LLM 응답 형식.

LLM 구조화 출력의 스키마로 그대로 쓰므로 모든 필드를 필수로 둔다(값이 없으면 빈 목록).
JSON 필드는 camelCase로 저장·응답한다.
"""

from typing import Literal

from app.schemas.common import ApiModel

# LLM이 정해진 값만 내도록 Literal로 제한한다(품질 진단 Severity와 같은 세 단계)
Level = Literal["high", "medium", "low"]


class ReportIssue(ApiModel):
    """품질 문제 하나에 대한 해석."""

    issue: str
    # LLM이 추정한 원인(확인된 사실이 아님)
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
    # 표본 한계·추측 여부 등 리포트를 해석할 때의 주의사항
    caveats: list[str]
