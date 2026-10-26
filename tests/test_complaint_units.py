import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from bson import ObjectId
from pydantic import BaseModel

from app.core.analysis_config import IssueConfig
from app.core.errors import AppError
from app.processors.splitter import FormRule
from app.schemas.complaint import IssueDocument
from app.schemas.complaint_api import ComplaintAnalysisRequest
from app.schemas.dataset import FieldMapping
from app.services.complaint_extraction import extract_complaints
from app.services.complaint_vectors import load_vectors, save_vectors
from app.services.embedding import normalize
from app.services.issue_grouping import IssueInput, distance_meters, group_issues
from app.services.llm import ChatReply, LlmClient
from app.services.naming import keyword_name, suggest_name
from app.services.storage import LocalStorage
from tests.document_files import make_docx

RULE = FormRule(
    start_label="번호",
    fields={"number": "번호", "title": "제목", "registered": "등록일"},
    content_field="title",
    content_type="title",
    received_field="registered",
)


def request(**mapping: str) -> ComplaintAnalysisRequest:
    """규칙 방식 분석 요청.

    파라미터:
        mapping: 공통 필드 → 열 이름
    """
    return ComplaintAnalysisRequest(
        mapping=FieldMapping.model_validate(mapping), attribute_method="rule"
    )


def test_table_rows_become_complaints_and_problems_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "data"
    path.write_text(
        "내용,접수일,위도,경도,분류\n"
        "가로등 고장,2026-07-14,36.04,129.37,136\n"
        ",2026-07-15,,,\n"  # 빈 본문
        "가로등 고장,2026-07-14,36.04,129.37,136\n"  # 완전 중복
        "쓰레기 방치,2026-07-16,129.37,36.04,144\n",  # 위도·경도가 뒤바뀐 좌표
        encoding="utf-8",
    )

    result = extract_complaints(
        path,
        "csv",
        request(
            content="내용", received_at="접수일", latitude="위도", longitude="경도",
            source_category="분류",
        ),
        None,
        {},
    )  # fmt: skip

    first, second = result.records
    assert (first.text, first.locator, first.source_category) == ("가로등 고장", {"row": 2}, "136")
    assert first.received_at == datetime(2026, 7, 14, tzinfo=UTC)
    assert (first.latitude, first.longitude) == (36.04, 129.37)
    assert (second.locator, second.latitude) == ({"row": 5}, None)
    assert {i.type: i.count for i in result.issues} == {
        "empty_content": 1,
        "duplicates": 1,
        "out_of_range": 1,
    }


def test_table_needs_valid_mapping(tmp_path: Path) -> None:
    path = tmp_path / "data"
    path.write_text("내용\n가로등\n", encoding="utf-8")

    with pytest.raises(AppError) as missing:
        extract_complaints(path, "csv", request(), None, {})
    with pytest.raises(AppError) as unknown:
        extract_complaints(path, "csv", request(content="본문"), None, {})

    assert (missing.value.code, unknown.value.code) == ("MAPPING_REQUIRED", "MAPPING_INVALID")


def test_document_is_split_by_rule_or_read_as_one(tmp_path: Path) -> None:
    path = tmp_path / "doc"
    path.write_bytes(
        make_docx(["번호 1", "제목 가로등 고장", "등록일 2026-10-24", "번호 2", "제목 불법 주차"])
    )

    split = extract_complaints(
        path, "docx", ComplaintAnalysisRequest(split_rule="list"), None, {"list": RULE}
    )
    whole = extract_complaints(path, "docx", ComplaintAnalysisRequest(), None, {})

    assert [(r.text, r.content_type) for r in split.records] == [
        ("가로등 고장", "title"),
        ("불법 주차", "title"),
    ]
    assert split.records[0].received_at == datetime(2026, 10, 24, tzinfo=UTC)
    [one] = whole.records
    assert one.text.startswith("번호 1\n제목 가로등 고장")


def test_text_file_is_split_by_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "memo"
    path.write_text(
        "가로등이 꺼졌습니다\n빨리 고쳐 주세요\n\n\n쓰레기가 많습니다\n", encoding="utf-8"
    )

    result = extract_complaints(path, "txt", ComplaintAnalysisRequest(), None, {})

    assert [(r.text, r.locator) for r in result.records] == [
        ("가로등이 꺼졌습니다 빨리 고쳐 주세요", {"line": 1}),
        ("쓰레기가 많습니다", {"line": 5}),
    ]


def item(
    category: str,
    axis: int,
    *,
    place: str | None = "수청동 놀이터 옆",
    point: tuple[float, float] | None = None,
    day: int = 1,
    urgency: str | None = "보통",
) -> IssueInput:
    """테스트용 이슈 묶기 입력.

    파라미터:
        category: 카테고리
        axis: 벡터 축(같으면 본문이 비슷한 것으로 본다)
        place: 장소 표현
        point: (위도, 경도)
        day: 접수일(2026-07-day)
        urgency: 긴급도
    """
    vector = normalize(np.eye(4)[axis : axis + 1] + 0.01)[0]
    return IssueInput(
        complaint_id=ObjectId(),
        category_id=category,
        vector=vector,
        text=f"민원 {axis}-{day}",
        place=place,
        latitude=point[0] if point else None,
        longitude=point[1] if point else None,
        received_at=datetime(2026, 7, day, tzinfo=UTC),
        urgency=urgency,
    )


def test_issues_group_same_place_and_problem() -> None:
    a, b = item("CAT-1", 0, day=1), item("CAT-1", 0, day=3, urgency="긴급")
    other_place = item("CAT-1", 0, place="중앙동 시장 입구")
    other_text = item("CAT-1", 1)
    no_place = item("CAT-1", 0, place=None)

    result = group_issues(
        [a, b, other_place, other_text, no_place],
        [],
        np.zeros((0, 4), dtype=np.float32),
        IssueConfig(),
    )

    [issue] = result.created
    assert (issue.complaint_count, issue.place, issue.max_urgency) == (
        2,
        "수청동 놀이터 옆",
        "긴급",
    )
    assert issue.first_received_at.day == 1 and issue.last_received_at.day == 3
    assert result.assignments == {a.complaint_id: issue.id, b.complaint_id: issue.id}


def test_issues_use_distance_when_points_exist() -> None:
    near = [
        item("CAT-1", 0, place="수청동 A", point=(36.0400, 129.3700)),
        item("CAT-1", 0, place="수청동 B", point=(36.0405, 129.3703)),  # 약 60m
    ]
    far = item("CAT-1", 0, place="수청동 A", point=(36.0500, 129.3700))  # 약 1.1km

    result = group_issues([*near, far], [], np.zeros((0, 4), dtype=np.float32), IssueConfig())

    assert [i.complaint_count for i in result.created] == [2]
    assert distance_meters(36.04, 129.37, 36.0405, 129.3703) < 100


def test_new_complaints_join_open_issue() -> None:
    issue = IssueDocument(
        category_id="CAT-1",
        place="수청동 놀이터 옆",
        complaint_count=2,
        first_received_at=datetime(2026, 6, 1, tzinfo=UTC),
        last_received_at=datetime(2026, 6, 5, tzinfo=UTC),
        summary="놀이터 옆 가로등",
        max_urgency="보통",
    )
    joined = item("CAT-1", 0, day=9, urgency="높음")

    result = group_issues([joined], [issue], normalize(np.eye(4)[0:1] + 0.01), IssueConfig())

    assert result.created == []
    [updated] = result.updated
    assert (updated.complaint_count, updated.max_urgency) == (3, "높음")
    assert updated.last_received_at.day == 9
    assert result.assignments == {joined.complaint_id: issue.id}


class NameBackend:
    """정해 둔 이름 초안을 돌려주는 가짜 LLM."""

    deployment = "fake-deep"

    async def complete(self, system: str, user: str, response_model: type[BaseModel]) -> ChatReply:
        """이름 초안 JSON을 돌려준다.

        파라미터:
            system: 시스템 지시문
            user: 사용자 메시지
            response_model: 응답 모델(사용하지 않음)
        """
        assert "가로등" in user
        reply = {"name": "가로등 고장", "definition": "가로등이 꺼짐", "group": "안전·조명"}
        return ChatReply(json.dumps(reply, ensure_ascii=False), 10, 5)


@pytest.mark.anyio
async def test_suggest_name_uses_llm_or_keywords(tmp_path: Path) -> None:
    client = LlmClient(NameBackend(), LocalStorage(tmp_path))

    drafted = await suggest_name(["가로등", "꺼짐"], ["골목 가로등이 꺼졌습니다"], client)
    fallback = await suggest_name(["가로등", "꺼짐", "어두움", "골목"], [], None)

    assert (drafted.name, drafted.group) == ("가로등 고장", "안전·조명")
    assert (fallback.name, fallback.definition) == ("가로등·꺼짐·어두움", None)
    assert keyword_name([]) == "이름 없음"


def test_vectors_round_trip(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    file_id, job_id = ObjectId(), ObjectId()
    ids = [ObjectId(), ObjectId()]
    vectors = normalize(np.asarray([[1.0, 0.0], [0.0, 1.0]]))

    save_vectors(storage, file_id, job_id, ids, vectors)
    loaded = load_vectors(storage, [(ids[1], file_id, job_id), (ObjectId(), file_id, ObjectId())])

    assert list(loaded) == [ids[1]]
    assert np.allclose(loaded[ids[1]], [0.0, 1.0])


def test_overlapping_candidate_reuses_previous_name() -> None:
    from app.schemas.complaint import CandidateDocument
    from app.services.complaint_handler import _reuse_suggestion

    old = CandidateDocument(
        job_id=ObjectId(),
        period="2026-10",
        size=4,
        keywords=["가로등"],
        exemplar_ids=[],
        suggested_name="가로등 고장",
        suggested_group="안전·조명",
    )
    previous_members = {old.id: {"a", "b", "c", "d"}}

    same = _reuse_suggestion({"a", "b", "c", "e"}, {old.id: old}, previous_members)  # 3/5
    different = _reuse_suggestion({"a", "x", "y", "z"}, {old.id: old}, previous_members)  # 1/7

    assert same is not None and (same.name, same.group) == ("가로등 고장", "안전·조명")
    assert different is None


def test_auto_method_uses_llm_only_when_configured(tmp_path: Path) -> None:
    from pydantic import SecretStr

    from app.core.config import Settings
    from app.services.complaint_handler import resolve_method

    plain = Settings(_env_file=None, mongodb_uri="mongodb://unused")
    with_llm = plain.model_copy(
        update={
            "azure_openai_endpoint": "https://example.openai.azure.com/openai/v1",
            "azure_openai_api_key": SecretStr("test-key"),
            "azure_openai_deployment_fast": "fast-deployment",
        }
    )

    assert resolve_method(ComplaintAnalysisRequest(), plain) == "rule"
    assert resolve_method(ComplaintAnalysisRequest(), with_llm) == "llm"
    assert resolve_method(ComplaintAnalysisRequest(attribute_method="rule"), with_llm) == "rule"
