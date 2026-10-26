from pathlib import Path

import pytest

from app.core.errors import AppError
from app.processors.document import DocLine
from app.processors.splitter import FormRule, load_split_rules, split_by_form

RULE = FormRule(
    start_label="번호",
    fields={"number": "번호", "title": "제목", "agency": "처리기관", "registered": "등록일"},
    content_field="title",
    content_type="title",
    continuation_fields=["title"],
    ignore_patterns=["://", r"\d+\s*/\s*\d+\s*$"],
)


def ocr(text: str, page: int = 1, confidence: float = 0.9) -> DocLine:
    """테스트용 OCR 줄.

    파라미터:
        text: 글자
        page: 페이지
        confidence: 신뢰도
    """
    return DocLine(text=text, page=page, confidence=confidence)


def test_splits_repeated_form_with_ocr_noise() -> None:
    lines = [
        ocr("26. 10. 24. 오후 10:00 국민신문고 | 민원"),  # 첫 표지 앞 머리말은 버린다
        ocr("번호 40513"),
        ocr("제목 도로가 비만오면 침수"),
        ocr("처리 기관 _ 경기도 성남시", confidence=0.5),
        ocr("등록일 : 2026-10-24"),
        ocr("번호 40512"),
        ocr("제목 정보공개 요청에 관한"),
        ocr("열람 허가 가능의 건"),  # 여러 줄 제목은 이어 붙인다
        ocr("처리기관 국토교통부"),
    ]

    first, second = split_by_form(lines, RULE)

    assert first.fields == {
        "number": "40513",
        "title": "도로가 비만오면 침수",
        "agency": "경기도 성남시",
        "registered": "2026-10-24",
    }
    assert (first.page, first.confidence, first.min_confidence) == (1, 0.8, 0.5)
    assert second.fields["title"] == "정보공개 요청에 관한 열람 허가 가능의 건"


def test_record_continues_across_pages_and_skips_footer() -> None:
    lines = [
        ocr("번호 1", page=1),
        ocr("제목 버스 미정차", page=1),
        ocr("https://www.epeople.go.kr/... 1/11", page=1),  # 바닥글은 제목에 붙지 않는다
        ocr("처리기관 대구광역시", page=2),
    ]

    [record] = split_by_form(lines, RULE)

    assert record.fields == {"number": "1", "title": "버스 미정차", "agency": "대구광역시"}
    assert record.page == 1


def test_records_without_content_are_dropped() -> None:
    lines = [ocr("번호 1"), ocr("처리기관 소방청"), ocr("번호 2"), ocr("제목 감지기 문의")]

    records = split_by_form(lines, RULE)

    assert [r.fields["number"] for r in records] == ["2"]


def test_text_lines_have_no_confidence() -> None:
    [record] = split_by_form([DocLine("번호 1"), DocLine("제목 가로등")], RULE)

    assert record.confidence is None


def test_repository_rules_include_sinmungo() -> None:
    rule = load_split_rules()["sinmungo"]

    assert rule.start_label == "번호"
    assert rule.content_field in rule.fields


def test_invalid_rules_file(tmp_path: Path) -> None:
    path = tmp_path / "rules.toml"
    path.write_text('[x]\nstart_label = "번호"\n', encoding="utf-8")

    with pytest.raises(AppError) as info:
        load_split_rules(path)
    assert info.value.code == "INVALID_SPLIT_RULES"
