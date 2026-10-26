import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.core.errors import AppError
from app.schemas.complaint_analysis import ComplaintRecord
from app.services.attributes import extract_by_llm, extract_by_rules, extract_place
from app.services.llm import ChatReply, LlmClient
from app.services.storage import LocalStorage


def record(text: str, record_id: str = "R1") -> ComplaintRecord:
    """테스트용 민원.

    파라미터:
        text: 본문(마스킹본)
        record_id: 민원 ID
    """
    return ComplaintRecord(record_id=record_id, text=text)


def test_rule_extraction_finds_six_attributes() -> None:
    attrs = extract_by_rules(
        record(
            "수청동 놀이터 옆에서 보안등이 또 꺼져 아이들이 넘어질 위험이 있습니다. "
            "수리 부탁드립니다."
        )
    )

    assert attrs.facility == "가로등"  # 보안등 → 시설 사전 표준 이름
    assert attrs.place.dong == "수청동"
    assert attrs.place.landmark == "놀이터 옆"
    assert attrs.urgency == "긴급"
    assert attrs.repeat == "재신고"
    assert attrs.request_type == "수리·조치"
    assert attrs.affected == "어린이"
    assert attrs.analyzer == "rule-dict@1"


def test_rule_extraction_leaves_unknown_empty() -> None:
    attrs = extract_by_rules(record("확인 바랍니다"))

    assert attrs.facility is None
    assert attrs.place.dong is None
    assert attrs.repeat is None
    assert attrs.affected is None
    assert attrs.urgency == "낮음"


@pytest.mark.parametrize(
    ("text", "dong", "landmark"),
    [
        ("송악읍 아파트 정문 큰길 건너편에서 운전자 시야가", "송악읍", "아파트 정문 큰길 건너편"),
        ("위치는 합덕읍 학교 주변, 공영주차장 출입구입니다.", "합덕읍", "학교 주변"),
        ("차량이 이동하는 도로에서 진동이 심합니다", None, None),
    ],
)
def test_extract_place(text: str, dong: str | None, landmark: str | None) -> None:
    place = extract_place(text)

    assert place.dong == dong
    assert place.landmark == landmark


class BatchBackend:
    """입력의 민원 번호마다 같은 속성을 돌려주는 가짜 LLM. 첫 묶음은 실패시킬 수 있다."""

    deployment = "fake-fast"

    def __init__(self, fail_first: bool = False, drop: str | None = None) -> None:
        """가짜 백엔드를 만든다.

        파라미터:
            fail_first: 첫 호출에서 LLM_ERROR를 던질지
            drop: 응답에서 뺄 민원 번호
        """
        self.calls = 0
        self._fail_first = fail_first
        self._drop = drop

    async def complete(self, system: str, user: str, response_model: type[BaseModel]) -> ChatReply:
        """입력 줄의 [번호]를 읽어 응답을 만든다.

        파라미터:
            system: 시스템 지시문
            user: 사용자 메시지
            response_model: 응답 모델(사용하지 않음)
        """
        self.calls += 1
        if self._fail_first and self.calls == 1:
            raise AppError("LLM_ERROR", "실패", 502)
        ids = [line[1 : line.index("]")] for line in user.splitlines() if line.startswith("[")]
        items = [
            {
                "id": i,
                "facility": "가로등",
                "dong": "수청동",
                "landmark": None,
                "urgency": "높음",
                "repeat": None,
                "request_type": "수리·조치",
                "affected": None,
            }
            for i in ids
            if i != self._drop
        ]
        return ChatReply(json.dumps({"items": items}), 10, 5)


@pytest.mark.anyio
async def test_llm_extraction_batches_and_records_errors(tmp_path: Path) -> None:
    backend = BatchBackend(fail_first=True, drop="R4")
    client = LlmClient(backend, LocalStorage(tmp_path))
    records = [record("수청동 가로등이 꺼졌습니다", f"R{i}") for i in range(5)]

    result = await extract_by_llm(records, client, batch_size=2)

    assert backend.calls == 3
    assert [a.record_id for a in result.attributes] == ["R2", "R3"]
    assert result.attributes[0].urgency == "높음"
    assert result.attributes[0].analyzer == "llm:fake-fast:complaint-attributes@1"
    assert result.errors == ["R0: LLM_ERROR", "R1: LLM_ERROR", "R4: LLM 응답에서 빠짐"]


@pytest.mark.anyio
async def test_llm_extraction_refuses_unmasked_text(tmp_path: Path) -> None:
    backend = BatchBackend()
    client = LlmClient(backend, LocalStorage(tmp_path))

    result = await extract_by_llm([record("연락처 010-1234-5678")], client)

    assert backend.calls == 0
    assert result.errors == ["R1: PII_DETECTED"]
