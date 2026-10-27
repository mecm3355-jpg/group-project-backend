"""LLM 없이 만드는 분석 리포트(POST /files/{file_id}/report의 규칙 방식).

LLM(deep 배포) 연결 정보가 없을 때 쓴다. AI 리포트와 같은 형식(AiReport)으로, 품질 진단·통계·
정제 결과에 있는 사실만 정해진 문장으로 옮긴다. 원인·영향은 문제 종류별 일반 설명이다.
"""

from pydantic import JsonValue

from app.schemas.report import AiReport, Level, ReportIssue, ReportRecommendation
from app.schemas.result import ResultDocument
from app.services.suggestions import suggest_rules

# 문제 종류별 (흔한 원인, 분석에 미치는 영향). 없는 종류는 기본 설명을 쓴다
ISSUE_NOTES: dict[str, tuple[str, str]] = {
    "missing_values": (
        "입력 누락이나 선택 항목으로 보인다.",
        "집계·평균에서 빠지거나 왜곡될 수 있다.",
    ),
    "duplicates": ("같은 자료가 두 번 등록된 것으로 보인다.", "건수가 부풀려진다."),
    "whitespace": ("입력·변환 과정에서 공백이 섞인 것으로 보인다.", "같은 값이 다르게 묶인다."),
    "type_mismatch": (
        "한 열에 다른 형식의 값이 섞인 것으로 보인다.",
        "숫자·날짜 계산이 틀어질 수 있다.",
    ),
    "outliers": ("입력 오류이거나 실제 극단값일 수 있다.", "평균·합계가 크게 흔들린다."),
    "empty_lines": ("편집 과정에서 빈 줄이 남은 것으로 보인다.", "줄 수 집계에만 영향이 있다."),
    "duplicate_lines": (
        "같은 내용이 반복 기록되었거나 복사된 것으로 보인다.",
        "빈도 집계가 부풀려질 수 있다.",
    ),
    "trailing_whitespace": (
        "편집·변환 과정에서 줄 끝에 공백이 남은 것으로 보인다.",
        "같은 줄이 다르게 비교될 수 있다.",
    ),
    "ocr_not_available": (
        "서버에 OCR이 설정되지 않았다.",
        "이미지 속 글자는 분석에서 빠진다.",
    ),
    "no_text_layer": (
        "스캔본이라 글자 층이 없는 것으로 보인다.",
        "OCR 없이는 내용을 분석할 수 없다.",
    ),
    "low_ocr_confidence": (
        "인쇄 품질이나 글꼴 탓에 OCR이 흔들린 것으로 보인다.",
        "오인식이 섞인다.",
    ),
    "out_of_range": ("열 이름이 바뀌었거나 입력 오류로 보인다.", "지도·거리 분석이 틀어진다."),
}
# 새 문제 종류가 추가돼도 리포트가 깨지지 않도록 쓰는 중립적인 설명
DEFAULT_NOTE = ("원인은 원본을 확인해야 알 수 있다.", "해당 항목을 쓰는 분석에 영향이 있다.")
# 진단 심각도(Severity)를 리포트 수준(Level)으로 옮기는 표. 지금은 이름이 같지만 타입이 달라
# 한쪽 값이 바뀌어도 여기서 맞출 수 있게 따로 둔다
PRIORITY: dict[str, Level] = {"high": "high", "medium": "medium", "low": "low"}


def _number(analysis: dict[str, JsonValue], key: str) -> int | None:
    """분석 결과의 정수 값(없거나 숫자가 아니면 None).

    파라미터:
        analysis: processing_results.analysis
        key: 키 이름
    """
    value = analysis.get(key)
    # bool은 int의 하위 타입이라 True가 1행으로 읽히지 않게 따로 뺀다
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _overview(analysis: dict[str, JsonValue], score: int | None) -> str:
    """데이터 규모와 품질 점수를 한두 문장으로.

    파라미터:
        analysis: processing_results.analysis
        score: 품질 점수(없으면 None)
    """
    parts: list[str] = []
    rows, columns = _number(analysis, "rowCount"), _number(analysis, "columnCount")
    lines, pages = _number(analysis, "lineCount"), _number(analysis, "pageCount")
    data_format = analysis.get("format")
    # 파일 유형마다 analysis 키가 다르므로(설계 파일 예시: 표는 rowCount, 텍스트는 lineCount)
    # 있는 키로 유형을 판단한다. PDF도 lineCount가 있어 pageCount를 lineCount보다 먼저 본다
    if rows is not None:
        parts.append(f"{data_format} 형식 표 데이터로 {rows}행 {columns or 0}열이다.")
    elif pages is not None:
        parts.append(f"{data_format} 문서로 {pages}쪽, 글자 줄 {lines or 0}개다.")
    elif lines is not None:
        parts.append(f"{data_format} 텍스트로 {lines}줄이다.")
    if score is not None:
        parts.append(f"품질 점수는 {score}점이다.")
    return " ".join(parts) or "진단 결과의 요약 정보가 적다."


def build_rule_report(analysis: ResultDocument, cleaning: ResultDocument | None) -> AiReport:
    """최신 분석·정제 결과로 규칙 리포트를 만든다.

    파라미터:
        analysis: 최신 분석 결과
        cleaning: 최신 정제 결과(없으면 None)
    """
    quality = analysis.quality
    # 심각한 문제부터, 같은 심각도에서는 건수가 많은 것부터 보여 준다
    issues = sorted(
        quality.issues, key=lambda i: ({"high": 0, "medium": 1, "low": 2}[i.severity], -i.count)
    )
    # 핵심 사실: 개요 + 상위 문제 5개. AI 리포트 지시문의 keyFindings 3~6개와 개수를 맞춘다
    findings = [_overview(analysis.analysis, quality.score)]
    findings += [i.message for i in issues[:5]]
    if not issues:
        findings.append("진단에서 발견된 품질 문제가 없다.")
    # 민원 분석을 거친 파일이면 배정·검토·후보 현황도 핵심 사실에 넣는다
    complaint = analysis.analysis.get("complaint")
    if isinstance(complaint, dict):
        findings.append(
            f"민원 {complaint.get('total')}건 중 자동 배정 비율 {complaint.get('assignedRate')}, "
            f"검토 필요 {complaint.get('reviewCount')}건, "
            f"신규 후보 {complaint.get('candidateCount')}개다."
        )
    if cleaning is not None:
        # cleaning은 자유 형식 dict라 rules가 목록이 아닐 수도 있어 확인 후 센다
        rules = cleaning.cleaning.get("rules")
        count = len(rules) if isinstance(rules, list) else 0
        findings.append(f"정제 v{cleaning.version}에서 규칙 {count}개를 적용했다.")

    # 문제마다 진단 메시지에 위치(열 이름)를 붙이고, 원인·영향은 종류별 일반 설명을 쓴다
    report_issues = [
        ReportIssue(
            issue=f"{i.message}" + (f" (위치: {i.location})" if i.location else ""),
            likely_cause=ISSUE_NOTES.get(i.type, DEFAULT_NOTE)[0],
            impact=ISSUE_NOTES.get(i.type, DEFAULT_NOTE)[1],
            severity=PRIORITY[i.severity],
        )
        for i in issues
    ]
    # 정제 추천은 POST cleaning-suggestions와 같은 규칙 추천(suggest_rules)을 재사용해
    # 리포트와 추천 화면이 서로 다른 말을 하지 않게 한다.
    # 우선순위는 그 열의 품질 문제 심각도를 따른다. issues가 심각도순이라 dict에는 열마다
    # 마지막(가장 가벼운) 문제가 남는다. 열과 연결되지 않는 규칙은 medium
    severity_of = {i.location: i.severity for i in issues}
    recommendations = [
        ReportRecommendation(
            action=f"{s.rule.type}"
            + (f" ({getattr(s.rule, 'column', None)})" if getattr(s.rule, "column", None) else ""),
            reason=s.reason,
            priority=PRIORITY[severity_of.get(getattr(s.rule, "column", None), "medium")],
        )
        for s in suggest_rules(analysis)
    ]
    # 분석 제안은 결과에 있는 정보(통계·민원 분석)에 따라 고정 문장을 고른다
    suggestions = ["열별 결측·고유값 수를 보고 분석에 쓸 열을 고른다."]
    if isinstance(analysis.analysis.get("statistics"), dict) and analysis.analysis["statistics"]:
        suggestions.append("숫자 열의 최솟값·최댓값·평균으로 분포와 이상값을 확인한다.")
    if isinstance(complaint, dict):
        suggestions.append("신규 카테고리 후보를 검토·승인한 뒤 월별·카테고리별 추이를 본다.")
    # 요약은 개요 + 가장 심각한 문제 하나(지시문의 summary 2~4문장에 맞춘 분량)
    return AiReport(
        summary=" ".join(findings[:2]),
        key_findings=findings,
        quality_issues=report_issues,
        cleaning_recommendations=recommendations,
        analysis_suggestions=suggestions,
        # 읽는 사람이 AI 리포트로 오해하지 않도록 한계를 분명히 적는다
        caveats=[
            "LLM 없이 규칙으로 작성한 리포트다. 진단 결과에 있는 사실만 옮겼고, "
            "원인·영향은 문제 종류별 일반 설명이다.",
            "원본 데이터는 보지 않고 통계 요약만으로 작성했다.",
        ],
    )
