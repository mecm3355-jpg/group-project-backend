"""LLM 어댑터(PRD 6.1). LLM 호출은 이 모듈 하나로만 한다.

- 연결 정보는 .env에서만 읽는다(AZURE_OPENAI_*).
- 마스킹본만 보낸다. 전송 직전에 개인정보가 남아 있는지 다시 검사하고, 남아 있으면 보내지 않는다.
- 응답은 JSON 스키마(구조화 출력)로 받아 app/schemas 모델로 검증한다.
- GPT-5 계열은 temperature를 고정할 수 없을 수 있어, 재현성은 프롬프트 버전 + 입력 해시 기준
  응답 캐시로 확보한다.
- 호출 수·토큰·실패·전송 시각을 모아 processing_results.ai에 기록할 수 있게 한다.
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, TypeVar

import openai
from openai import AsyncAzureOpenAI, AsyncOpenAI
from openai.types.chat import (
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)
from pydantic import BaseModel, JsonValue, ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.schemas.mongo import utc_now
from app.services.masking import find_pii
from app.services.storage import LocalStorage

M = TypeVar("M", bound=BaseModel)

# fast: 대량·정형 작업(민원 속성 추출, 애매 배정, 정제 추천 검토)
# deep: 깊은 추론(AI 분석 리포트, 카테고리 이름·정의)
ModelTier = Literal["fast", "deep"]


@dataclass
class ChatReply:
    """LLM 응답 원문(JSON 문자열)과 토큰 사용량."""

    content: str
    prompt_tokens: int
    completion_tokens: int


class ChatBackend(Protocol):
    """LLM 호출 방법. 실제 구현은 AzureChatBackend, 테스트에서는 가짜 구현을 쓴다."""

    deployment: str

    async def complete(self, system: str, user: str, response_model: type[BaseModel]) -> ChatReply:
        """시스템·사용자 메시지를 보내고 response_model 스키마에 맞는 JSON 응답을 받는다.

        파라미터:
            system: 시스템 지시문
            user: 사용자 메시지(마스킹본)
            response_model: 응답 JSON 스키마가 될 모델
        """
        ...


def llm_configured(settings: Settings, tier: ModelTier) -> bool:
    """tier 배포로 호출할 연결 정보가 모두 있는지 본다(요청 단계에서 빨리 거부할 때 쓴다).

    파라미터:
        settings: 앱 설정
        tier: fast 또는 deep
    """
    try:
        AzureChatBackend(settings, tier)
    except AppError:
        return False
    return True


class AzureChatBackend:
    """Azure OpenAI(Azure AI Foundry) 배포를 호출한다."""

    def __init__(self, settings: Settings, tier: ModelTier) -> None:
        """tier에 맞는 배포로 클라이언트를 만든다. 연결 정보가 없으면 AppError(LLM_NOT_CONFIGURED).

        엔드포인트가 .../openai/v1 이면 Azure v1 API(API 버전 불필요)로,
        아니면 API 버전을 붙인 AzureOpenAI 클라이언트로 호출한다.

        파라미터:
            settings: AZURE_OPENAI_* 설정이 담긴 앱 설정
            tier: fast(대량·정형 작업) 또는 deep(깊은 추론)
        """
        endpoint = (settings.azure_openai_endpoint or "").strip()
        api_key = settings.azure_openai_api_key
        deployment = (
            settings.azure_openai_deployment_fast
            if tier == "fast"
            else settings.azure_openai_deployment_deep
        )
        is_v1 = endpoint.rstrip("/").endswith("/openai/v1")
        api_version = settings.azure_openai_api_version
        if not (endpoint and api_key and deployment and (is_v1 or api_version)):
            raise AppError(
                "LLM_NOT_CONFIGURED",
                f"LLM 연결 정보가 없습니다({tier}). .env의 AZURE_OPENAI_* 값을 확인하세요.",
                503,
            )
        self.deployment = deployment
        self._client: AsyncOpenAI
        if is_v1:
            self._client = AsyncOpenAI(
                base_url=endpoint.rstrip("/") + "/",
                api_key=api_key.get_secret_value(),
                timeout=settings.llm_timeout_seconds,
                max_retries=2,
            )
        else:
            self._client = AsyncAzureOpenAI(
                azure_endpoint=endpoint,
                api_key=api_key.get_secret_value(),
                api_version=api_version,
                timeout=settings.llm_timeout_seconds,
                max_retries=2,
            )

    async def complete(self, system: str, user: str, response_model: type[BaseModel]) -> ChatReply:
        """구조화 출력으로 호출한다. API 오류·거절·잘린 응답은 AppError로 바꾼다.

        파라미터:
            system: 시스템 지시문
            user: 사용자 메시지(마스킹본)
            response_model: 응답 JSON 스키마가 될 모델
        """
        try:
            completion = await self._client.chat.completions.parse(
                model=self.deployment,
                messages=[
                    ChatCompletionSystemMessageParam(role="system", content=system),
                    ChatCompletionUserMessageParam(role="user", content=user),
                ],
                response_format=response_model,
            )
        except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as exc:
            raise AppError(
                "LLM_INVALID_RESPONSE", f"LLM 응답이 완성되지 않았습니다: {type(exc).__name__}", 502
            ) from exc
        except openai.APIStatusError as exc:
            raise AppError(
                "LLM_ERROR", f"LLM 호출에 실패했습니다(HTTP {exc.status_code}).", 502
            ) from exc
        except openai.APIError as exc:
            raise AppError(
                "LLM_ERROR", f"LLM 호출에 실패했습니다: {type(exc).__name__}", 502
            ) from exc

        message = completion.choices[0].message
        if message.refusal:
            raise AppError("LLM_REFUSED", "LLM이 응답을 거절했습니다.", 502)
        usage = completion.usage
        return ChatReply(
            content=message.content or "",
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )

    async def close(self) -> None:
        """HTTP 연결을 닫는다.

        파라미터: 없음
        """
        await self._client.close()


@dataclass
class LlmUsage:
    """한 작업 동안의 LLM 사용 내역. to_record()를 processing_results.ai에 저장한다."""

    deployment: str
    calls: int = 0
    cache_hits: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_versions: set[str] = field(default_factory=set)
    first_sent_at: datetime | None = None
    last_sent_at: datetime | None = None

    def record_sent(self, reply: ChatReply) -> None:
        """실제 전송 한 건을 기록한다.

        파라미터:
            reply: 받은 응답(토큰 수)
        """
        now = utc_now()
        self.calls += 1
        self.prompt_tokens += reply.prompt_tokens
        self.completion_tokens += reply.completion_tokens
        self.first_sent_at = self.first_sent_at or now
        self.last_sent_at = now

    def to_record(self) -> dict[str, JsonValue]:
        """processing_results.ai에 넣을 사용 내역(PRD 6.1·10장: 호출 건수·토큰·실패·전송 시각).

        파라미터: 없음
        """
        return {
            "deployment": self.deployment,
            "calls": self.calls,
            "cacheHits": self.cache_hits,
            "failures": self.failures,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "promptVersions": list[JsonValue](sorted(self.prompt_versions)),
            "firstSentAt": self.first_sent_at.isoformat() if self.first_sent_at else None,
            "lastSentAt": self.last_sent_at.isoformat() if self.last_sent_at else None,
        }


class LlmClient:
    """개인정보 이중 검사·응답 캐시·스키마 검증·사용 내역 기록을 붙인 LLM 호출기."""

    def __init__(self, backend: ChatBackend, cache: LocalStorage | None) -> None:
        """호출기를 만든다.

        파라미터:
            backend: 실제 호출 방법
            cache: 응답 캐시를 둘 저장소. None이면 캐시하지 않는다
        """
        self._backend = backend
        self._cache = cache
        self.usage = LlmUsage(deployment=backend.deployment)

    def _cache_key(
        self, prompt_version: str, system: str, masked_input: str, model: type[BaseModel]
    ) -> str:
        """캐시 위치. 배포·프롬프트 버전·지시문·입력·응답 스키마가 같으면 같은 위치가 된다.

        파라미터:
            prompt_version: 프롬프트 버전(예: category-name@1)
            system: 시스템 지시문
            masked_input: 사용자 메시지(마스킹본)
            model: 응답 모델
        """
        payload = json.dumps(
            [
                self._backend.deployment,
                prompt_version,
                system,
                masked_input,
                model.model_json_schema(),
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        safe_version = "".join(c if c.isalnum() or c in "-_.@" else "_" for c in prompt_version)
        return f"llm-cache/{safe_version}/{digest}.json"

    async def generate(
        self, *, prompt_version: str, system: str, masked_input: str, response_model: type[M]
    ) -> M:
        """마스킹된 입력을 보내 response_model 형식의 결과를 받는다.

        개인정보가 남아 있으면 보내지 않고 AppError(PII_DETECTED).
        캐시에 같은 요청의 응답이 있으면 전송하지 않고 그 응답을 쓴다.
        응답이 스키마에 맞지 않으면 AppError(LLM_INVALID_RESPONSE).
        호출한 쪽에서 행 단위 오류로 기록한다.

        파라미터:
            prompt_version: 프롬프트 버전. 프롬프트를 고치면 반드시 올린다
            system: 시스템 지시문(개인정보 없음)
            masked_input: 사용자 메시지. mask_text를 거친 마스킹본이어야 한다
            response_model: 응답을 검증할 app/schemas 모델
        """
        remaining = find_pii(masked_input)
        if remaining:
            raise AppError(
                "PII_DETECTED",
                f"마스킹되지 않은 개인정보({', '.join(remaining)})가 있어 LLM에 보내지 않았습니다.",
                422,
            )
        self.usage.prompt_versions.add(prompt_version)
        key = self._cache_key(prompt_version, system, masked_input, response_model)

        if self._cache is not None:
            try:
                cached = await asyncio.to_thread(self._cache.read_text, key)
            except AppError:
                cached = None
            if cached is not None:
                try:
                    result = response_model.model_validate_json(cached)
                except ValidationError:
                    pass  # 스키마가 바뀐 옛 캐시는 무시하고 다시 호출한다
                else:
                    self.usage.cache_hits += 1
                    return result

        try:
            reply = await self._backend.complete(system, masked_input, response_model)
        except AppError:
            self.usage.failures += 1
            raise
        self.usage.record_sent(reply)
        try:
            result = response_model.model_validate_json(reply.content)
        except ValidationError as exc:
            self.usage.failures += 1
            raise AppError(
                "LLM_INVALID_RESPONSE", "LLM 응답이 요구한 형식과 다릅니다.", 502
            ) from exc

        if self._cache is not None:
            await asyncio.to_thread(self._cache.write_text, key, reply.content)
        return result


def create_llm_client(settings: Settings, tier: ModelTier) -> LlmClient:
    """설정으로 Azure OpenAI 호출기를 만든다. 캐시는 원본 저장소의 llm-cache 폴더에 둔다.

    파라미터:
        settings: 앱 설정(AZURE_OPENAI_*, storage_root)
        tier: fast(대량·정형 작업, 예: luna) 또는 deep(깊은 추론, 예: sol)
    """
    return LlmClient(AzureChatBackend(settings, tier), LocalStorage(settings.storage_root))
