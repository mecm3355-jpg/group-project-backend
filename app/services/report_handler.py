"""AI 분석 리포트 작업("report") 처리기.

원본 데이터는 보내지 않고, 최신 분석(품질 진단·통계)과 최신 정제 결과의 요약만
LLM(deep 배포)에 보낸다.
요약도 마스킹한 뒤 보내며, LLM 어댑터가 전송 직전에 개인정보를 한 번 더 검사한다.
LLM 연결 정보가 없으면 같은 형식의 규칙 리포트를 만든다(rule_report).
"""

import json
from collections.abc import Callable

from pydantic import JsonValue

from app.core.config import Settings
from app.core.errors import AppError
from app.repositories.results import ResultRepository
from app.schemas.file import FileDocument
from app.schemas.report import AiReport
from app.schemas.result import ResultDocument
from app.services.job_runner import JobContext, JobHandler
from app.services.llm import LlmClient, create_llm_client, llm_configured
from app.services.masking import mask_text
from app.services.rule_report import build_rule_report

PROMPT_VERSION = "file-report@2"

SYSTEM_PROMPT = """당신은 공공 데이터의 품질을 진단하는 데이터 분석가다.
입력은 한 파일의 품질 진단 결과 요약(JSON)이다. 원본 데이터는 포함되어 있지 않다.

다음 원칙으로 한국어 리포트를 작성하라.
- 입력에 있는 사실만 쓴다. 입력에 없는 값·원인을 단정하지 말고, 추정이면 "~로 보인다"처럼 쓴다.
- 열은 입력의 열 이름 그대로 부른다.
- summary: 이 데이터가 무엇으로 보이는지와 전반적인 품질을 2~4문장으로.
- keyFindings: 가장 중요한 사실 3~6개.
- qualityIssues: 입력의 품질 문제마다 원인 추정(likelyCause), 분석에 미치는 영향(impact), 심각도.
- cleaningRecommendations: 우선순위 순으로. ID·좌표·식별 번호처럼 평균·중앙값으로 채우면
  안 되는 열은 채우기 대신 원본 확인이나 제외를 권한다.
- analysisSuggestions: 이 데이터로 할 만한 분석과 그때의 주의점.
- caveats: 이 리포트의 한계(예: 통계 요약만 보고 판단함).

입력의 analysis.merged가 true이면 여러 기관 자료를 공통 열로 합친 통합 데이터셋이다.
- analysis.sources에 출처별 행 수, 매핑(공통 필드 → 원본 열), 고정값, 매핑 메모(notes)가 있다.
  매핑이 이상해 보여도 notes에 이유가 있으면 그 이유를 따른다(예: 값 범위로 좌표 열을 바꿔 매핑).
- columns의 notApplicableCount는 그 열을 제공하지 않는 출처의 빈칸 수다. 결측이 아니므로
  결측 문제로 다루지 말고, 출처 간 열 구성 차이로 설명한다. missingCount만 실제 결측이다.
- analysis.distributions의 출처·지역·기존분류·처리기관·월별 분포로 출처 간 비교를 하라.
"""

# 설정으로 LLM 호출기를 만든다. 연결 정보가 없으면 None(규칙 리포트)
LlmFactory = Callable[[Settings], LlmClient | None]


def without_raw_values(analysis: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """분석 결과에서 원본 값이 그대로 든 항목(열별 topValues, 텍스트 topKeywords)을 뺀다.

    이름·이메일 같은 칸 값이 LLM으로 나가지 않게 한다. 개수·숫자 통계는 남긴다.

    파라미터:
        analysis: processing_results.analysis
    """
    result = {k: v for k, v in analysis.items() if k != "topKeywords"}
    statistics = analysis.get("statistics")
    if isinstance(statistics, dict):
        trimmed: dict[str, JsonValue] = {}
        for name, stats in statistics.items():
            if isinstance(stats, dict):
                stats = {k: v for k, v in stats.items() if k != "topValues"}
                if not stats:
                    continue
            trimmed[name] = stats
        result["statistics"] = trimmed
    return result


def _summary_of(result: ResultDocument) -> dict[str, JsonValue]:
    """결과 문서에서 리포트 입력으로 쓸 부분(품질·분석)을 꺼낸다. 원본 값 항목은 뺀다.

    파라미터:
        result: 분석 또는 정제 결과 문서
    """
    return {
        "version": result.version,
        "quality": result.quality.model_dump(mode="json", by_alias=True),
        "analysis": without_raw_values(result.analysis),
    }


def build_report_input(
    file: FileDocument, analysis: ResultDocument, cleaning: ResultDocument | None
) -> str:
    """LLM에 보낼 입력(JSON 문자열)을 만들고 마스킹한다.

    파일명은 개인정보가 있을 수 있어 넣지 않는다.

    파라미터:
        file: 대상 파일
        analysis: 최신 분석 결과
        cleaning: 최신 정제 결과(없으면 None)
    """
    payload: dict[str, JsonValue] = {
        "file": {
            "extension": file.extension,
            "category": file.category,
            "sizeBytes": file.size_bytes,
        },
        "latestAnalysis": _summary_of(analysis),
    }
    if cleaning is not None:
        payload["latestCleaning"] = {
            **_summary_of(cleaning),
            "rules": cleaning.cleaning.get("rules"),
            "changes": cleaning.cleaning.get("changes"),
            "before": cleaning.cleaning.get("before"),
            "after": cleaning.cleaning.get("after"),
        }
    text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    return mask_text(text).text


def make_report_handler(llm_factory: LlmFactory) -> JobHandler:
    """LLM 호출기 만드는 방법을 받아 리포트 작업 처리기를 만든다(테스트에서 가짜 LLM을 넣는다).

    파라미터:
        llm_factory: 설정으로 LLM 호출기를 만드는 함수
    """

    async def report(ctx: JobContext) -> None:
        """리포트 작업을 실행한다. 단계: validating → ai_summary → saving.

        파라미터:
            ctx: 작업 실행 정보
        """
        await ctx.report("validating", 5)
        results = ResultRepository(ctx.db)
        analysis = await results.get_latest(ctx.file.id, "analysis")
        if analysis is None:
            raise AppError(
                "RESULT_NOT_FOUND", "진단 결과가 없습니다. 먼저 추출·진단을 실행하세요.", 422
            )
        cleaning = await results.get_latest(ctx.file.id, "cleaning")
        masked_input = build_report_input(ctx.file, analysis, cleaning)
        llm = llm_factory(ctx.settings)

        await ctx.report("ai_summary", 20)
        if llm is None:
            report_body = build_rule_report(analysis, cleaning)
        else:
            report_body = await llm.generate(
                prompt_version=PROMPT_VERSION,
                system=SYSTEM_PROMPT,
                masked_input=masked_input,
                response_model=AiReport,
            )

        await ctx.report("saving", 90)
        await results.insert(
            ResultDocument(
                field=ctx.file.id,
                job_id=ctx.job.id,
                result_type="report",
                version=await results.next_version(ctx.file.id, "report"),
                ai={
                    "report": report_body.model_dump(mode="json", by_alias=True),
                    "basedOn": {
                        "analysisVersion": analysis.version,
                        "cleaningVersion": cleaning.version if cleaning else None,
                    },
                    # llm: AI 리포트, rule: LLM 없이 규칙으로 쓴 리포트
                    "method": "rule" if llm is None else "llm",
                    "promptVersion": None if llm is None else PROMPT_VERSION,
                    "usage": None if llm is None else llm.usage.to_record(),
                },
            )
        )

    return report


def default_llm(settings: Settings) -> LlmClient | None:
    """리포트에 쓰는 기본 LLM: 깊은 추론용 deep 배포(예: gpt-5.6-sol-1). 연결 정보가 없으면 None.

    파라미터:
        settings: 앱 설정
    """
    return create_llm_client(settings, "deep") if llm_configured(settings, "deep") else None


report = make_report_handler(default_llm)
