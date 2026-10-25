from collections.abc import Iterator
from datetime import datetime

import pytest

from app.processors.tabular import Cell, Table
from app.processors.text import TextLines
from app.services.profiling import classify, parse_number, profile_table, profile_text


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
        "price": {"min": 1000, "max": 50000, "mean": 18750},
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
    # 100 × (11/12 채움) × (1 − 1/4 중복)
    assert profile.quality.score == 68  # floor(68.75)


def test_profile_table_detects_type_mismatch() -> None:
    rows: list[list[Cell]] = [[str(i)] for i in range(19)]
    rows.append(["모름"])

    profile = profile_table(make_table(["age"], rows))

    issue = next(i for i in profile.quality.issues if i.type == "type_mismatch")
    assert (issue.location, issue.count) == ("age", 1)
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
