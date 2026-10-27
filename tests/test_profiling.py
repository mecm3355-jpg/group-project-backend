from collections.abc import Iterator
from datetime import datetime

import pytest
from pydantic import JsonValue

from app.processors.document import DocumentText, ImageInfo
from app.processors.tabular import Cell, Table
from app.processors.text import TextLines
from app.services import profiling
from app.services.profiling import (
    classify,
    histogram,
    parse_number,
    profile_document,
    profile_table,
    profile_text,
)


def make_table(columns: list[str], rows: list[list[Cell]], ragged: int = 0) -> Table:
    """테스트용 Table을 만든다.

    파라미터:
        columns: 열 이름
        rows: 행 목록(이미 열 길이에 맞춘 것)
        ragged: 길이가 달랐던 행 수
    """
    table = Table("csv", "utf-8", None, columns, iter(rows))
    table.ragged_row_count = ragged
    return table


def column_info(analysis: dict[str, JsonValue]) -> dict[str, dict[str, JsonValue]]:
    """분석 결과의 열 정보를 열 이름 → 정보 dict로 꺼낸다.

    파라미터:
        analysis: profile.analysis
    """
    columns = analysis["columns"]
    assert isinstance(columns, list)
    result: dict[str, dict[str, JsonValue]] = {}
    for column in columns:
        assert isinstance(column, dict)
        result[str(column["name"])] = column
    return result


def lines(items: list[str]) -> Iterator[str]:
    """줄 목록을 반복자로 바꾼다.

    파라미터:
        items: 줄 목록
    """
    return iter(items)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1000", 1000.0), ("-1,234.5", -1234.5), (".5", 0.5), ("1,23", None), ("12a", None)],
)
def test_parse_number(text: str, expected: float | None) -> None:
    assert parse_number(text) == expected


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("42", "number"),
        (3.5, "number"),
        (True, "boolean"),
        ("FALSE", "boolean"),
        ("2026-07-14", "date"),
        ("2026.07.14 09:30", "date"),
        (datetime(2026, 7, 14), "date"),
        ("가로등", "string"),
    ],
)
def test_classify(value: Cell, kind: str) -> None:
    assert classify(value)[0] == kind


def test_profile_table_matches_schema_example_shape() -> None:
    rows: list[list[Cell]] = [
        ["a@x.com", "1000", "2026-01-01"],
        ["", "50000", "2026-03-01"],
        ["b@x.com", "12,000", "2026-02-01"],
        ["b@x.com", "12,000", "2026-02-01"],  # 중복 행
        [None, None, None],  # 빈 행
    ]

    profile = profile_table(make_table(["email", "price", "date"], rows))

    analysis = profile.analysis
    assert analysis["format"] == "csv"
    assert analysis["rowCount"] == 4
    assert analysis["columnCount"] == 3
    assert analysis["columns"] == [
        {"name": "email", "inferredType": "string", "missingCount": 1, "uniqueCount": 2},
        {"name": "price", "inferredType": "number", "missingCount": 0, "uniqueCount": 3},
        {"name": "date", "inferredType": "date", "missingCount": 0, "uniqueCount": 3},
    ]
    assert analysis["statistics"] == {
        "email": {
            "topValues": [{"value": "b@x.com", "count": 2}, {"value": "a@x.com", "count": 1}]
        },
        "price": {
            "min": 1000,
            "max": 50000,
            "mean": 18750,
            "histogram": [
                {"start": 1000, "end": 17333.3333, "count": 3},
                {"start": 17333.3333, "end": 33666.6667, "count": 0},
                {"start": 33666.6667, "end": 50000, "count": 1},
            ],
        },
        "date": {"min": "2026-01-01T00:00:00", "max": "2026-03-01T00:00:00"},
    }

    issues = {i.type: i for i in profile.quality.issues}
    assert set(issues) == {"missing_values", "duplicates", "empty_rows"}
    assert issues["missing_values"].location == "email"
    assert issues["missing_values"].count == 1
    assert issues["missing_values"].message == "email 필드에 결측치가 있습니다."
    assert issues["missing_values"].severity == "medium"  # 1/4 = 25%
    assert issues["duplicates"].count == 1
    assert issues["duplicates"].message == "중복 행 1개가 발견되었습니다."
    assert issues["empty_rows"].count == 1
    # 완전성 11/12, 유일성 3/4, 유효성·일관성 100의 평균(25%씩)
    assert profile.quality.score == 91  # floor(91.67)
    assert profile.quality.metrics == {
        "completeness": 91.7,  # 11/12
        "uniqueness": 75.0,  # 1 − 1/4
        "validity": 100.0,
        "consistency": 100.0,
    }
    assert profile.quality.score_basis


def test_profile_table_detects_type_mismatch() -> None:
    rows: list[list[Cell]] = [[str(i)] for i in range(19)]
    rows.append(["모름"])

    profile = profile_table(make_table(["age"], rows))

    issue = next(i for i in profile.quality.issues if i.type == "type_mismatch")
    assert (issue.location, issue.count) == ("age", 1)
    assert profile.quality.metrics is not None
    assert profile.quality.metrics["validity"] == 95.0  # 20칸 중 1칸이 숫자가 아님
    assert profile.analysis["columns"] == [
        {"name": "age", "inferredType": "number", "missingCount": 0, "uniqueCount": 20}
    ]


def test_profile_table_reports_header_and_row_length_problems() -> None:
    table = make_table(["a", "column_2"], [["1", "2"]], ragged=1)
    table.empty_header_count = 1
    table.duplicate_headers = ["a"]

    types = {i.type for i in profile_table(table).quality.issues}

    assert {"row_length_mismatch", "empty_header", "duplicate_header"} <= types


def test_profile_empty_table() -> None:
    profile = profile_table(make_table(["a"], []))

    assert profile.analysis["rowCount"] == 0
    assert profile.quality.score == 100
    assert profile.quality.issues == []


def test_profile_log_matches_schema_example_shape() -> None:
    text = TextLines(
        "log",
        "utf-8",
        lines(["2026 INFO start", "", "ERROR fail", "WARNING disk", "  ", "WARN cpu", "plain"]),
    )

    profile = profile_text(text)

    assert profile.analysis == {
        "format": "log",
        "encoding": "utf-8",
        "lineCount": 7,
        "emptyLineCount": 2,
        "keywordCounts": {"ERROR": 1, "WARN": 2, "INFO": 1},
    }
    assert profile.quality.score is None
    [issue] = profile.quality.issues
    assert (issue.type, issue.count, issue.message) == (
        "empty_lines",
        2,
        "빈 줄 2개가 발견되었습니다.",
    )


def test_profile_txt_has_no_keyword_counts() -> None:
    profile = profile_text(TextLines("txt", "utf-8", lines(["ERROR 라는 단어", "내용"])))

    assert "keywordCounts" not in profile.analysis
    assert profile.quality.issues == []


def test_profile_txt_counts_duplicates_whitespace_and_keywords() -> None:
    text = TextLines(
        "txt",
        "utf-8",
        lines(
            ["가로등이 고장났습니다", "도로 파손 신고 ", "가로등이 고장났습니다", "", "가로등 점검"]
        ),
    )

    profile = profile_text(text)

    issues = {i.type: i.count for i in profile.quality.issues}
    assert issues == {"empty_lines": 1, "duplicate_lines": 1, "trailing_whitespace": 1}
    keywords = profile.analysis["topKeywords"]
    assert isinstance(keywords, list)
    assert keywords[0] == {"keyword": "가로등", "count": 3}
    assert "keywordLineCount" not in profile.analysis


def test_profile_log_has_no_top_keywords() -> None:
    profile = profile_text(TextLines("log", "utf-8", lines(["INFO a", "INFO a"])))

    assert "topKeywords" not in profile.analysis
    assert [i.type for i in profile.quality.issues] == ["duplicate_lines"]


def test_free_text_column_has_no_top_values() -> None:
    rows: list[list[Cell]] = [[f"서술 {i}"] for i in range(51)]

    profile = profile_table(make_table(["memo"], rows))

    assert profile.analysis["statistics"] == {}


def test_histogram_single_value_and_last_bin_includes_max() -> None:
    assert histogram([5.0, 5.0]) == [{"start": 5, "end": 5, "count": 2}]
    bins = histogram([0.0, 1.0, 2.0, 10.0])
    assert sum(b["count"] for b in bins if isinstance(b, dict) and isinstance(b["count"], int)) == 4
    assert bins[-1] == {"start": 7.5, "end": 10, "count": 1}


def test_image_without_ocr_profile_has_metadata() -> None:
    document = DocumentText(
        "image", iter(()), page_count=1, image=ImageInfo("JPEG", 640, 480, "RGB")
    )

    profile = profile_document(document)

    assert [i.type for i in profile.quality.issues] == ["ocr_not_available"]
    assert profile.analysis["method"] == "none"
    assert profile.analysis["image"] == {
        "format": "JPEG",
        "width": 640,
        "height": 480,
        "mode": "RGB",
    }


def test_merged_table_excludes_structural_missing_and_source_columns() -> None:
    columns = ["출처파일ID", "원본행", "본문", "지역"]
    rows: list[list[Cell]] = [
        # 출처 A는 본문·지역 제공, 지역 하나는 진짜 결측
        ["A", 2, "가로등 고장", "양덕동"],
        ["A", 3, "도로 파손", ""],
        # 출처 B는 본문 열이 없음(전부 빈칸) → 결측이 아니라 해당 없음
        ["B", 2, "", "아산시"],
        ["B", 3, "", "아산시"],
        ["B", 4, "", "아산시"],  # 출처 정보만 다른 중복 행
    ]

    profile = profile_table(make_table(columns, rows))

    info = column_info(profile.analysis)
    assert (info["본문"]["missingCount"], info["본문"]["notApplicableCount"]) == (0, 3)
    assert (info["지역"]["missingCount"], info["지역"]["notApplicableCount"]) == (1, 0)
    missing = [i for i in profile.quality.issues if i.type == "missing_values"]
    assert [(i.location, i.count) for i in missing] == [("지역", 1)]
    duplicates = next(i for i in profile.quality.issues if i.type == "duplicates")
    assert duplicates.count == 2
    # 점수: 완전성(해당 칸 17개 중 16개)·유일성(1 − 중복 2/5)·유효성·일관성(100)의 평균
    assert profile.quality.score == int((100 * 16 / 17 + 100 * 3 / 5 + 100 + 100) / 4)


def test_plain_table_has_no_not_applicable_count() -> None:
    profile = profile_table(make_table(["a"], [["1"]]))

    assert "notApplicableCount" not in column_info(profile.analysis)["a"]


def test_duplicate_check_stops_at_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profiling, "DUPLICATE_MAX_LINES", 2)

    profile = profile_text(TextLines("log", "utf-8", lines(["a", "b", "a", "a"])))

    assert profile.quality.issues == []  # 앞 2줄(a, b)만 비교한다
    assert profile.analysis["duplicateCheckLineCount"] == 2
