import csv
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core.errors import AppError
from app.processors.tabular import Cell
from app.schemas.cleaning import CleanRequest, CleanRule
from app.services.cleaning import apply_rules, write_csv

RULE = TypeAdapter(CleanRule)
COLUMNS = ["id", "name", "age", "city"]


def rows() -> list[list[Cell]]:
    """테스트용 행(매번 새로 만든다 — apply_rules가 행을 바꿀 수 있다).

    파라미터: 없음
    """
    return [
        ["1", " 홍길동 ", "30", "포항"],
        ["2", "김철수", "", "포항"],
        ["3", "이영희", "40", None],
        ["1", "홍길동", "30", "포항"],
        [],
        ["4", "박민수", "35", "김천"],
    ]


def run(*rules: dict[str, object]) -> tuple[list[list[Cell]], list[int]]:
    """규칙(dict)을 적용하고 (결과 행, 규칙별 변경 수)를 돌려준다.

    파라미터:
        rules: 명세서 형식의 규칙들
    """
    parsed = [RULE.validate_python(r) for r in rules]
    result, changes = apply_rules(COLUMNS, rows(), parsed)
    return result, [c.affected for c in changes]


def test_trim_whitespace() -> None:
    result, affected = run({"type": "trim_whitespace", "column": "name"})

    assert affected == [1]
    assert result[0][1] == "홍길동"
    assert len(result) == 5  # 빈 줄은 빠진다


def test_remove_duplicates_by_columns_after_trim() -> None:
    # 공백을 지우기 전에도 앞뒤 공백은 무시하고 비교한다
    result, affected = run({"type": "remove_duplicates", "columns": ["id", "name"]})

    assert affected == [1]
    assert [r[0] for r in result] == ["1", "2", "3", "4"]


def test_remove_duplicates_all_columns() -> None:
    _, affected = run({"type": "remove_duplicates"})

    assert affected == [1]


@pytest.mark.parametrize(
    ("strategy", "expected"), [("mean", 33.75), ("median", 32.5), ("mode", "30")]
)
def test_fill_missing_strategies(strategy: str, expected: Cell) -> None:
    result, affected = run({"type": "fill_missing", "column": "age", "strategy": strategy})

    assert affected == [1]
    assert result[1][2] == expected


def test_fill_missing_constant_and_none_cell() -> None:
    result, affected = run(
        {"type": "fill_missing", "column": "city", "strategy": "constant", "value": "미상"}
    )

    assert affected == [1]
    assert result[2][3] == "미상"


def test_rules_apply_in_order() -> None:
    result, affected = run(
        {"type": "trim_whitespace", "column": "name"},
        {"type": "remove_duplicates", "columns": ["name", "age"]},
        {"type": "fill_missing", "column": "age", "strategy": "median"},
    )

    assert affected == [1, 1, 1]
    assert [r[1] for r in result] == ["홍길동", "김철수", "이영희", "박민수"]
    assert result[1][2] == 35  # 중복 제거 후 30, 40, 35의 중앙값


@pytest.mark.parametrize(
    ("rule", "code"),
    [
        ({"type": "trim_whitespace", "column": "없는열"}, "UNKNOWN_COLUMN"),
        ({"type": "remove_duplicates", "columns": ["id", "없는열"]}, "UNKNOWN_COLUMN"),
        ({"type": "fill_missing", "column": "city", "strategy": "median"}, "NOT_NUMERIC_COLUMN"),
    ],
)
def test_rule_errors(rule: dict[str, object], code: str) -> None:
    with pytest.raises(AppError) as info:
        run(rule)
    assert info.value.code == code


def test_fill_missing_without_any_value() -> None:
    parsed = [RULE.validate_python({"type": "fill_missing", "column": "a", "strategy": "mode"})]

    with pytest.raises(AppError) as info:
        apply_rules(["a"], [[None], [""]], parsed)
    assert info.value.code == "NO_VALUES_TO_FILL"


@pytest.mark.parametrize(
    "body",
    [
        {"rules": []},
        {"rules": [{"type": "drop_table"}]},
        {"rules": [{"type": "fill_missing", "column": "a", "strategy": "constant"}]},
        {"rules": [{"type": "fill_missing", "column": "a", "strategy": "max"}]},
        {"rules": [{"type": "remove_duplicates", "columns": []}]},
    ],
)
def test_clean_request_rejects_invalid_rules(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CleanRequest.model_validate(body)


def test_clean_request_accepts_spec_example() -> None:
    # 명세서 "처리 예시" 그대로
    body = {
        "rules": [
            {"type": "trim_whitespace", "column": "customer_name"},
            {"type": "remove_duplicates", "columns": ["customer_id"]},
            {"type": "fill_missing", "column": "age", "strategy": "median"},
        ]
    }

    assert len(CleanRequest.model_validate(body).rules) == 3


def test_write_csv_round_trip(tmp_path: Path) -> None:
    from datetime import datetime

    path = tmp_path / "out.csv"
    write_csv(path, ["a", "b"], [["가", None], [datetime(2026, 7, 14), 3.5]])

    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # 엑셀용 BOM
    with path.open(encoding="utf-8-sig", newline="") as f:
        assert list(csv.reader(f)) == [["a", "b"], ["가", ""], ["2026-07-14T00:00:00", "3.5"]]
