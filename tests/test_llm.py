import os
from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr

from app.core.config import Settings
from app.core.errors import AppError
from app.services.llm import (
    AzureChatBackend,
    ChatReply,
    LlmClient,
    ModelTier,
    create_llm_client,
)
from app.services.storage import LocalStorage

pytestmark = pytest.mark.anyio


class CategoryName(BaseModel):
    """테스트용 응답 모델(카테고리 이름 제안)."""

    name: str
    definition: str


class FakeBackend:
    """미리 정한 응답을 돌려주는 가짜 LLM. 받은 요청을 기록한다."""

    deployment = "fake-deployment"

    def __init__(self, replies: list[str | AppError]) -> None:
        """가짜 백엔드를 만든다.

        파라미터:
            replies: 차례로 돌려줄 응답(JSON 문자열) 또는 던질 오류
        """
        self._replies = list(replies)
        self.sent: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, response_model: type[BaseModel]) -> ChatReply:
        """기록하고 다음 응답을 돌려준다.

        파라미터:
            system: 시스템 지시문
            user: 사용자 메시지
            response_model: 응답 모델(사용하지 않음)
        """
        self.sent.append((system, user))
        reply = self._replies.pop(0)
        if isinstance(reply, AppError):
            raise reply
        return ChatReply(content=reply, prompt_tokens=100, completion_tokens=20)


GOOD = '{"name": "가로등 고장", "definition": "가로등이 꺼지거나 깜빡임"}'


async def generate(client: LlmClient, masked_input: str, version: str = "name@1") -> CategoryName:
    """테스트용 공통 호출.

    파라미터:
        client: LLM 호출기
        masked_input: 보낼 입력
        version: 프롬프트 버전
    """
    return await client.generate(
        prompt_version=version,
        system="민원 묶음의 이름을 지어라.",
        masked_input=masked_input,
        response_model=CategoryName,
    )


async def test_generate_validates_and_records_usage(tmp_path: Path) -> None:
    backend = FakeBackend([GOOD])
    client = LlmClient(backend, LocalStorage(tmp_path))

    result = await generate(client, "수청동 [상세주소] 가로등이 깜빡입니다")

    assert result == CategoryName(name="가로등 고장", definition="가로등이 꺼지거나 깜빡임")
    record = client.usage.to_record()
    assert record["deployment"] == "fake-deployment"
    assert (record["calls"], record["cacheHits"], record["failures"]) == (1, 0, 0)
    assert (record["promptTokens"], record["completionTokens"]) == (100, 20)
    assert record["promptVersions"] == ["name@1"]
    assert record["firstSentAt"] is not None


async def test_cache_prevents_second_call(tmp_path: Path) -> None:
    backend = FakeBackend([GOOD])
    first = LlmClient(backend, LocalStorage(tmp_path))
    await generate(first, "가로등 민원")

    # 새 호출기(다른 작업)라도 같은 요청이면 캐시를 쓴다
    second = LlmClient(backend, LocalStorage(tmp_path))
    result = await generate(second, "가로등 민원")

    assert result.name == "가로등 고장"
    assert len(backend.sent) == 1
    assert (second.usage.calls, second.usage.cache_hits) == (0, 1)


async def test_prompt_version_change_bypasses_cache(tmp_path: Path) -> None:
    backend = FakeBackend([GOOD, GOOD])
    client = LlmClient(backend, LocalStorage(tmp_path))

    await generate(client, "가로등 민원", version="name@1")
    await generate(client, "가로등 민원", version="name@2")

    assert len(backend.sent) == 2


async def test_refuses_to_send_unmasked_pii(tmp_path: Path) -> None:
    backend = FakeBackend([GOOD])
    client = LlmClient(backend, LocalStorage(tmp_path))

    with pytest.raises(AppError) as info:
        await generate(client, "홍길동 주무관 010-1234-5678로 연락")

    assert info.value.code == "PII_DETECTED"
    assert "010" not in info.value.message  # 오류 메시지에도 원문을 남기지 않는다
    assert backend.sent == []


async def test_invalid_response_is_failure_and_not_cached(tmp_path: Path) -> None:
    backend = FakeBackend(['{"name": "이름만"}', GOOD])
    client = LlmClient(backend, LocalStorage(tmp_path))

    with pytest.raises(AppError) as info:
        await generate(client, "가로등 민원")
    assert info.value.code == "LLM_INVALID_RESPONSE"
    assert (client.usage.calls, client.usage.failures) == (1, 1)

    # 잘못된 응답은 캐시하지 않으므로 다시 호출한다
    assert (await generate(client, "가로등 민원")).name == "가로등 고장"
    assert len(backend.sent) == 2


async def test_backend_error_counts_as_failure(tmp_path: Path) -> None:
    backend = FakeBackend([AppError("LLM_ERROR", "호출 실패", 502)])
    client = LlmClient(backend, None)

    with pytest.raises(AppError):
        await generate(client, "가로등 민원")

    assert (client.usage.calls, client.usage.failures) == (0, 1)


def llm_settings(tmp_path: Path, **values: str | None) -> Settings:
    """.env와 무관하게 Azure 설정을 직접 지정한 설정을 만든다.

    파라미터:
        tmp_path: 저장소 임시 폴더
        values: 바꿀 AZURE_OPENAI_* 값(필드 이름, 소문자)
    """
    base: dict[str, str | None] = {
        "azure_openai_endpoint": None,
        "azure_openai_api_key": None,
        "azure_openai_api_version": None,
        "azure_openai_deployment_fast": None,
        "azure_openai_deployment_deep": None,
    }
    base.update(values)
    settings = Settings(mongodb_uri="mongodb://localhost", storage_root=tmp_path)
    return settings.model_copy(
        update={
            **base,
            "azure_openai_api_key": SecretStr(base["azure_openai_api_key"])
            if base["azure_openai_api_key"]
            else None,
        }
    )


def test_missing_configuration(tmp_path: Path) -> None:
    with pytest.raises(AppError) as info:
        AzureChatBackend(llm_settings(tmp_path), "fast")
    assert info.value.code == "LLM_NOT_CONFIGURED"


def test_tier_selects_deployment_and_v1_needs_no_version(tmp_path: Path) -> None:
    settings = llm_settings(
        tmp_path,
        azure_openai_endpoint="https://example.openai.azure.com/openai/v1",
        azure_openai_api_key="test-key",
        azure_openai_deployment_fast="gpt-5.6-luna-1",
        azure_openai_deployment_deep="gpt-5.6-sol-1",
    )

    assert AzureChatBackend(settings, "fast").deployment == "gpt-5.6-luna-1"
    assert AzureChatBackend(settings, "deep").deployment == "gpt-5.6-sol-1"


def test_classic_endpoint_requires_api_version(tmp_path: Path) -> None:
    settings = llm_settings(
        tmp_path,
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_api_key="test-key",
        azure_openai_deployment_fast="gpt-5.6-luna-1",
    )

    with pytest.raises(AppError):
        AzureChatBackend(settings, "fast")


# ---- 실제 Azure OpenAI 연결 확인: RUN_LLM_TESTS=1 이고 .env에 AZURE_OPENAI_* 가 있을 때만 ----


@pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1",
    reason="실제 LLM 호출은 RUN_LLM_TESTS=1일 때만 실행(외부 전송·비용 발생)",
)
@pytest.mark.parametrize("tier", ["fast", "deep"])
async def test_live_azure_openai(tmp_path: Path, tier: ModelTier) -> None:
    settings = Settings(mongodb_uri="mongodb://localhost", storage_root=tmp_path)
    client = create_llm_client(settings, tier)

    # 가상 데이터만 보낸다
    result = await generate(
        client, "가로등이 밤마다 깜빡임 / 보안등이 꺼져 골목이 어두움 / 가로등 불이 안 들어옴"
    )

    assert result.name
    assert client.usage.calls == 1
    assert client.usage.prompt_tokens > 0
