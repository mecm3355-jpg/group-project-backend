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
from app.services.profiling import without_raw_values
from app.services.rule_report import build_rule_report

# 프롬프트를 고치면 올린다. 결과(ai.promptVersion)에 남겨 어떤 프롬프트로 쓴 리포트인지 추적한다
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


def _summary_of(result: ResultDocument) -> dict[str, JsonValue]:
    """결과 문서에서 리포트 입력으로 쓸 부분(품질·분석)을 꺼낸다. 원본 값 항목은 뺀다.

    파라미터:
        result: 분석 또는 정제 결과 문서
    """
    # version은 리포트가 어느 결과를 근거로 썼는지 LLM이 구분하게 넣는다.
    # 품질 문제의 location은 열 이름이라 그대로 두고, 원본 값 빈도(topValues 등)만 뺀다
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
    # 파일 정보는 데이터 종류를 짐작할 만큼만(확장자·분류·크기) 보낸다
    payload: dict[str, JsonValue] = {
        "file": {
            "extension": file.extension,
            "category": file.category,
            "sizeBytes": file.size_bytes,
        },
        "latestAnalysis": _summary_of(analysis),
    }
    # 정제를 했으면 적용 규칙·변경 건수·정제 전후 요약을 함께 보내 "정제 후 남은 문제"를 쓰게 한다
    if cleaning is not None:
        payload["latestCleaning"] = {
            **_summary_of(cleaning),
            "rules": cleaning.cleaning.get("rules"),
            "changes": cleaning.cleaning.get("changes"),
            "before": cleaning.cleaning.get("before"),
            "after": cleaning.cleaning.get("after"),
        }
    # ensure_ascii=False: 한글을 \uXXXX로 바꾸지 않아야 마스킹 패턴이 맞고 토큰도 덜 든다.
    # default=str은 날짜·ObjectId처럼 JSON에 없는 타입을 문자열로 바꾼다
    text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    # 1차 마스킹: 열 이름·정제 규칙 값 등에 섞인 전화번호·이메일 등을 가린다
    # (LLM 어댑터가 전송 직전에 한 번 더 검사하는 것이 2차)
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
        # API가 작업을 만들 때 확인했지만, 그 사이 결과가 지워졌을 수 있어 워커에서 다시 본다.
        # 이미 받은 작업의 실행 실패라 404가 아닌 422로 작업 오류에 남긴다
        analysis = await results.get_latest(ctx.file.id, "analysis")
        if analysis is None:
            raise AppError(
                "RESULT_NOT_FOUND", "진단 결과가 없습니다. 먼저 추출·진단을 실행하세요.", 422
            )
        # 정제 결과는 없어도 된다(진단만 한 파일도 리포트를 만들 수 있다)
        cleaning = await results.get_latest(ctx.file.id, "cleaning")
        masked_input = build_report_input(ctx.file, analysis, cleaning)
        llm = llm_factory(ctx.settings)

        # LLM 호출이 가장 오래 걸리는 단계라 진행률 20~90 구간을 이 단계에 둔다
        await ctx.report("ai_summary", 20)
        # LLM 연결 정보가 없는 환경(로컬·테스트·키 미발급)에서도 리포트 기능이 동작하도록
        # 같은 AiReport 형식의 규칙 리포트로 대신한다
        if llm is None:
            report_body = build_rule_report(analysis, cleaning)
        else:
            report_body = await llm.generate(
                prompt_version=PROMPT_VERSION,
                system=SYSTEM_PROMPT,
                masked_input=masked_input,
                response_model=AiReport,
            )

        # 리포트는 덮어쓰지 않고 새 버전으로 쌓는다(processing_results는 버전별 보관)
        await ctx.report("saving", 90)
        await results.insert(
            ResultDocument(
                field=ctx.file.id,
                job_id=ctx.job.id,
                result_type="report",
                version=await results.next_version(ctx.file.id, "report"),
                ai={
                    "report": report_body.model_dump(mode="json", by_alias=True),
                    # 근거 버전을 남겨, 재진단·재정제 뒤 이 리포트가 낡았는지 알 수 있게 한다
                    "basedOn": {
                        "analysisVersion": analysis.version,
                        "cleaningVersion": cleaning.version if cleaning else None,
                    },
                    # llm: AI 리포트, rule: LLM 없이 규칙으로 쓴 리포트
                    "method": "rule" if llm is None else "llm",
                    "promptVersion": None if llm is None else PROMPT_VERSION,
                    # 토큰 사용량은 비용 추적용(규칙 리포트는 호출이 없어 None)
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
    # 리포트는 여러 진단 결과를 엮어 원인·영향을 추론해야 해서 빠른 배포 대신 deep 배포를 쓴다
    return create_llm_client(settings, "deep") if llm_configured(settings, "deep") else None


# 워커가 작업 종류 "report"에 연결하는 실제 처리기
report = make_report_handler(default_llm)
