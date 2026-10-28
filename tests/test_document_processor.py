from pathlib import Path

import pytest

from app.core.errors import AppError
from app.processors.document import DocumentText, open_document
from app.services.profiling import profile_document
from tests.document_files import make_docx, make_pdf


def read_all(path: Path, extension: str) -> tuple[list[str], DocumentText]:
    """문서를 끝까지 읽어 (줄 목록, 문서 객체)를 돌려준다.

    파라미터:
        path: 파일 경로
        extension: docx 또는 pdf
    """
    with open_document(path, extension) as document:
        lines = list(document.lines)
    return lines, document


def test_docx_paragraphs_and_table_in_order(tmp_path: Path) -> None:
    # 저장소처럼 확장자 없이 저장한다
    path = tmp_path / "abc123"
    path.write_bytes(
        make_docx(
            ["가로등 고장 민원", "", "  수청동 일대  "], table=[["구분", "건수"], ["가로등", "3"]]
        )
    )

    lines, document = read_all(path, "docx")

    assert lines == ["가로등 고장 민원", "  수청동 일대", "구분\t건수", "가로등\t3"]
    assert (document.table_count, document.page_count) == (1, None)


def test_pdf_counts_pages_without_text(tmp_path: Path) -> None:
    path = tmp_path / "def456"
    path.write_bytes(make_pdf([["Street light broken", "Near city hall"], []]))

    lines, document = read_all(path, "pdf")

    assert lines == ["Street light broken", "Near city hall"]
    assert (document.page_count, document.pages_without_text) == (2, 1)


@pytest.mark.parametrize(
    ("extension", "content"),
    [("docx", b"PK\x03\x04broken"), ("pdf", b"%PDF-1.7\nbroken")],
)
def test_broken_document_is_invalid(tmp_path: Path, extension: str, content: bytes) -> None:
    path = tmp_path / "broken"
    path.write_bytes(content)

    with pytest.raises(AppError) as info:
        read_all(path, extension)
    assert info.value.code == "INVALID_FILE"


def test_profile_pdf_reports_missing_text_layer(tmp_path: Path) -> None:
    path = tmp_path / "scan"
    path.write_bytes(make_pdf([["Hello"], [], []]))

    with open_document(path, "pdf") as document:
        profile = profile_document(document)

    assert profile.analysis == {
        "format": "pdf",
        "method": "text",
        "pageCount": 3,
        "pagesWithoutText": 2,
        "lineCount": 1,
        "characterCount": 5,
    }
    [issue] = profile.quality.issues
    assert (issue.type, issue.severity, issue.count) == ("no_text_layer", "medium", 2)
    assert profile.quality.score is None


def test_profile_scanned_pdf_is_high_severity(tmp_path: Path) -> None:
    path = tmp_path / "scan"
    path.write_bytes(make_pdf([[]]))

    with open_document(path, "pdf") as document:
        profile = profile_document(document)

    [issue] = profile.quality.issues
    assert (issue.type, issue.severity) == ("no_text_layer", "high")


def test_profile_docx(tmp_path: Path) -> None:
    path = tmp_path / "doc"
    path.write_bytes(make_docx(["첫 줄", "둘째 줄"], table=[["a", "b"]]))

    with open_document(path, "docx") as document:
        profile = profile_document(document)

    assert profile.analysis == {
        "format": "docx",
        "method": "text",
        "tableCount": 1,
        "lineCount": 3,
        "characterCount": 10,
    }
    assert profile.quality.issues == []


def test_profile_empty_docx(tmp_path: Path) -> None:
    path = tmp_path / "doc"
    path.write_bytes(make_docx(["", "  "]))

    with open_document(path, "docx") as document:
        profile = profile_document(document)

    [issue] = profile.quality.issues
    assert (issue.type, issue.severity) == ("no_text", "high")
