"""신규 카테고리 후보의 이름·정의·상위 그룹 초안(PRD 5.1 명명, 6장 B, 6.1: deep 배포).

LLM에는 후보의 핵심어와 대표 민원 마스킹본만 보낸다. LLM을 쓸 수 없거나 실패하면 핵심어로
이름을 만든다(담당자가 승인 때 고친다).
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import AppError
from app.services.llm import LlmClient

# 프롬프트(SYSTEM)를 고치면 올린다. LLM 응답 캐시가 이 버전 + 입력 해시로 묶이므로(PRD 6.1),
# 버전을 그대로 두면 바뀐 프롬프트에도 이전 응답이 재사용된다
PROMPT_VERSION = "category-name@1"
# PRD 5.4 초기 상위 그룹(참고용). 어느 것에도 맞지 않으면 새 그룹을 제안한다
GROUPS = (
    "교통·주정차",
    "대중교통",
    "도로·보행",
    "안전·조명",
    "환경·청소",
    "물·하천",
    "공원·공공시설",
    "소음·건축·주거",
    "행정·법령 질의",
)
SYSTEM = f"""너는 지방자치단체 민원 분류 체계를 만드는 도우미다.
비슷한 민원을 묶은 군집 하나의 핵심어와 대표 민원을 보고 카테고리 초안을 만든다.
- name: 짧은 명사구(예: 가로등 고장, 불법 현수막·광고물). 15자 이내
- definition: 한 줄 정의와 경계 메모(예: 가로등·보안등이 꺼지거나 깜빡임. 설치 요청은 제외)
- group: 상위 그룹. 다음 중 하나를 고르고, 맞는 것이 없으면 새 그룹을 짓는다:
  {", ".join(GROUPS)}
[전화], [인명] 같은 표시는 개인정보를 가린 것이다."""


class CategoryDraft(BaseModel):
    """LLM 응답: 카테고리 이름·정의·상위 그룹 초안."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="카테고리 이름(15자 이내)")
    definition: str = Field(description="한 줄 정의와 경계 메모")
    group: str = Field(description="상위 그룹")


@dataclass(frozen=True)
class NameSuggestion:
    """후보 하나의 이름 제안(LLM이 없으면 정의·그룹은 None)."""

    name: str
    definition: str | None
    group: str | None


def keyword_name(keywords: list[str]) -> str:
    """핵심어 앞 3개로 만든 임시 이름.

    파라미터:
        keywords: 후보 핵심어
    """
    # 핵심어는 c-TF-IDF 점수 순이라 앞쪽일수록 그 군집에만 두드러진 낱말이다
    return "·".join(keywords[:3]) if keywords else "이름 없음"


async def suggest_name(
    keywords: list[str], exemplars: list[str], client: LlmClient | None
) -> NameSuggestion:
    """후보 하나의 이름 초안을 만든다. 실패하면 핵심어 이름으로 대신한다.

    파라미터:
        keywords: 핵심어
        exemplars: 대표 민원 본문(마스킹본)
        client: deep 배포 LLM 호출기. None이면 핵심어 이름만 만든다
    """
    # LLM이 꺼져 있거나 실패해도 후보 목록은 나와야 하므로 핵심어 이름을 먼저 만들어 둔다
    fallback = NameSuggestion(keyword_name(keywords), None, None)
    if client is None:
        return fallback
    # 대표 민원은 200자까지만 보낸다. 주제를 알기엔 충분하고, 외부 전송량·토큰 비용을 줄인다
    lines = "\n".join(f"- {text[:200]}" for text in exemplars)
    try:
        draft = await client.generate(
            prompt_version=PROMPT_VERSION,
            system=SYSTEM,
            masked_input=f"핵심어: {', '.join(keywords)}\n대표 민원:\n{lines}",
            response_model=CategoryDraft,
        )
    except AppError:
        # 호출·형식 오류는 분석 전체를 멈출 일이 아니다. 담당자가 승인 때 이름을 고친다
        return fallback
    # 이름은 15자 이내로 요청했지만 지키지 않을 수 있어 저장 전에 100자로 자른다
    return NameSuggestion(draft.name.strip()[:100], draft.definition.strip(), draft.group.strip())
