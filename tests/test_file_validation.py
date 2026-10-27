import zipfile
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.services.file_validation import ZipLimits, decode_text_sample, detect_format

LIMITS = ZipLimits(max_uncompressed_bytes=10 * 1024 * 1024, max_entries=100)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"\x00" * 32


def write(tmp_path: Path, name: str, content: bytes) -> Path:
    """임시 폴더에 테스트 파일을 만든다.

    파라미터:
        tmp_path: pytest 임시 폴더
        name: 파일명
        content: 파일 내용
    """
    path = tmp_path / name
    path.write_bytes(content)
    return path


def make_ooxml(tmp_path: Path, name: str, parts: dict[str, bytes]) -> Path:
    """지정한 파트로 ZIP(OOXML 흉내) 파일을 만든다.

    파라미터:
        tmp_path: pytest 임시 폴더
        name: 파일명
        parts: ZIP 안 경로 → 내용
    """
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for part, content in parts.items():
            archive.writestr(part, content)
    return path


def error_code(path: Path, extension: str, limits: ZipLimits = LIMITS) -> str:
    """detect_format이 던진 AppError의 코드를 돌려준다.

    파라미터:
        path: 검사할 파일
        extension: 확장자
        limits: 압축 해제 제한
    """
    with pytest.raises(AppError) as info:
        detect_format(path, extension, limits)
    return info.value.code


@pytest.mark.parametrize(
    ("extension", "content", "mime", "category"),
    [
        ("csv", "이름,나이\n홍길동,30\n".encode(), "text/csv", "tabular"),
        ("csv", "이름,나이\n홍길동,30\n".encode("cp949"), "text/csv", "tabular"),
        ("csv", "﻿이름,나이\n".encode(), "text/csv", "tabular"),
        ("tsv", b"a\tb\n1\t2\n", "text/tab-separated-values", "tabular"),
        ("json", b'[{"a": 1}]', "application/json", "tabular"),
        ("txt", "민원 내용".encode("utf-16"), "text/plain", "text"),
        ("log", b"INFO start\nERROR fail\n", "text/plain", "text"),
        ("pdf", PDF, "application/pdf", "document"),
        ("png", PNG, "image/png", "image"),
        ("jpg", JPEG, "image/jpeg", "image"),
        ("jpeg", JPEG, "image/jpeg", "image"),
        (
            "xls",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32,
            "application/vnd.ms-excel",
            "tabular",
        ),
    ],
)
def test_accepts_valid_files(
    tmp_path: Path, extension: str, content: bytes, mime: str, category: str
) -> None:
    path = write(tmp_path, f"sample.{extension}", content)

    detected = detect_format(path, extension, LIMITS)

    assert (detected.extension, detected.mime_type, detected.category) == (
        extension,
        mime,
        category,
    )


def test_accepts_xlsx_and_docx(tmp_path: Path) -> None:
    xlsx = make_ooxml(
        tmp_path, "a.xlsx", {"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"<x/>"}
    )
    docx = make_ooxml(
        tmp_path, "a.docx", {"[Content_Types].xml": b"<x/>", "word/document.xml": b"<x/>"}
    )

    assert detect_format(xlsx, "xlsx", LIMITS).category == "tabular"
    assert detect_format(docx, "docx", LIMITS).category == "document"


@pytest.mark.parametrize("extension", ["exe", "xlsm", "docm", "bat", "ps1", "js"])
def test_rejects_blocked_extension(tmp_path: Path, extension: str) -> None:
    path = write(tmp_path, "sample", b"data")

    assert error_code(path, extension) == "UNSUPPORTED_FILE_TYPE"


@pytest.mark.parametrize(
    ("extension", "content", "mime"),
    [
        ("hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", "application/x-ole-storage"),
        ("pptx", b"PK\x03\x04rest", "application/zip"),
        ("md", "# 제목\n본문".encode(), "text/plain"),
        ("", b"plain text", "text/plain"),
        ("bin", b"\x00\x01\x02\x03binary", "application/octet-stream"),
    ],
)
def test_registers_other_formats(tmp_path: Path, extension: str, content: bytes, mime: str) -> None:
    path = write(tmp_path, "sample", content)

    detected = detect_format(path, extension, LIMITS)

    assert (detected.extension, detected.mime_type, detected.category) == (
        extension,
        mime,
        "other",
    )


@pytest.mark.parametrize(
    ("extension", "content", "code"),
    [
        ("dat", b"MZ\x90\x00rest", "UNSUPPORTED_FILE_TYPE"),  # 이름만 바꾼 실행 파일
        ("dat", b"\x7fELFrest", "UNSUPPORTED_FILE_TYPE"),
        ("dat", PDF, "FILE_TYPE_MISMATCH"),  # 지원 형식인데 확장자만 다름
        ("gif", PNG, "FILE_TYPE_MISMATCH"),
    ],
)
def test_rejects_disguised_other_files(
    tmp_path: Path, extension: str, content: bytes, code: str
) -> None:
    path = write(tmp_path, "sample", content)

    assert error_code(path, extension) == code


@pytest.mark.parametrize(
    ("extension", "content"),
    [
        ("csv", PDF),  # PDF를 csv로 이름만 바꿈
        ("jpg", PNG),  # PNG를 jpg로 이름만 바꿈
        ("pdf", b"just text"),
        ("xlsx", b"just text"),
        ("png", JPEG),
    ],
)
def test_rejects_extension_mismatch(tmp_path: Path, extension: str, content: bytes) -> None:
    path = write(tmp_path, f"sample.{extension}", content)

    assert error_code(path, extension) == "FILE_TYPE_MISMATCH"


def test_rejects_binary_as_text(tmp_path: Path) -> None:
    path = write(tmp_path, "sample.txt", b"abc\x00\x01\x02def")

    assert error_code(path, "txt") == "INVALID_FILE"


def test_rejects_broken_json(tmp_path: Path) -> None:
    path = write(tmp_path, "sample.json", b'{"a": ')

    assert error_code(path, "json") == "INVALID_FILE"


def test_rejects_zip_that_is_not_xlsx(tmp_path: Path) -> None:
    path = make_ooxml(tmp_path, "a.xlsx", {"readme.txt": b"hello"})

    assert error_code(path, "xlsx") == "INVALID_FILE"


def test_rejects_docx_renamed_to_xlsx(tmp_path: Path) -> None:
    path = make_ooxml(
        tmp_path, "a.xlsx", {"[Content_Types].xml": b"<x/>", "word/document.xml": b"<x/>"}
    )

    assert error_code(path, "xlsx") == "INVALID_FILE"


def test_rejects_corrupted_zip(tmp_path: Path) -> None:
    path = write(tmp_path, "a.xlsx", b"PK\x03\x04" + b"\x00" * 40)

    assert error_code(path, "xlsx") == "INVALID_FILE"


def test_rejects_macro_document(tmp_path: Path) -> None:
    path = make_ooxml(
        tmp_path,
        "a.xlsx",
        {
            "[Content_Types].xml": b"<x/>",
            "xl/workbook.xml": b"<x/>",
            "xl/vbaProject.bin": b"macro",
        },
    )

    assert error_code(path, "xlsx") == "MACRO_NOT_ALLOWED"


def test_rejects_zip_bomb_by_size(tmp_path: Path) -> None:
    path = make_ooxml(
        tmp_path,
        "a.xlsx",
        {"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"0" * 2048},
    )

    limits = ZipLimits(max_uncompressed_bytes=1024, max_entries=100)
    assert error_code(path, "xlsx", limits) == "ARCHIVE_TOO_LARGE"


def test_rejects_zip_bomb_by_entries(tmp_path: Path) -> None:
    parts = {f"xl/sheet{i}.xml": b"<x/>" for i in range(10)}
    parts.update({"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"<x/>"})
    path = make_ooxml(tmp_path, "a.xlsx", parts)

    limits = ZipLimits(max_uncompressed_bytes=1024 * 1024, max_entries=5)
    assert error_code(path, "xlsx", limits) == "ARCHIVE_TOO_LARGE"


def test_decode_text_sample_allows_cut_multibyte_char() -> None:
    # 샘플 끝에서 한글 한 글자(3바이트)가 잘린 경우
    sample = "가나다".encode()[:-1]

    assert decode_text_sample(sample) == "가나"
