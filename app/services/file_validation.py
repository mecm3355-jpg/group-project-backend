"""업로드 파일의 실제 형식 검증. 확장자만 믿지 않고 파일 서명(매직 바이트)과 내부 구조를 본다.

지원 형식은 확장자와 실제 내용이 맞아야 받는다. 그 밖의 형식은 기본 정보만 등록하고
내용 분석은 하지 않는다(category other, 프론트 PRD P0). 다만 실행 파일·스크립트와
매크로 문서, 지원 형식인 척하는 파일(확장자는 다르지만 내용이 PDF·이미지 등)은 거부한다.
"""

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import AppError
from app.schemas.file import FileCategory, ProcessingCapabilities

PDF_MAGIC = b"%PDF-"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# 실행 파일 서명: Windows PE(MZ), Linux ELF, macOS Mach-O
EXECUTABLE_MAGICS = (
    b"MZ",
    b"\x7fELF",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
)

TEXT_SAMPLE_BYTES = 64 * 1024


@dataclass(frozen=True)
class FormatSpec:
    """지원 확장자 하나의 판별 기준."""

    signature: str  # pdf, png, jpeg, zip, ole, text 중 하나
    mime_type: str
    category: FileCategory


# HWP·HWPX는 내용 분석 지원 여부 미결정(PRD 12장)이라 other로만 등록된다.
SUPPORTED: dict[str, FormatSpec] = {
    "csv": FormatSpec("text", "text/csv", "tabular"),
    "tsv": FormatSpec("text", "text/tab-separated-values", "tabular"),
    "json": FormatSpec("text", "application/json", "tabular"),
    "txt": FormatSpec("text", "text/plain", "text"),
    "log": FormatSpec("text", "text/plain", "text"),
    "xlsx": FormatSpec(
        "zip", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "tabular"
    ),
    "xls": FormatSpec("ole", "application/vnd.ms-excel", "tabular"),
    "docx": FormatSpec(
        "zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "document",
    ),
    "pdf": FormatSpec("pdf", "application/pdf", "document"),
    "png": FormatSpec("png", "image/png", "image"),
    "jpg": FormatSpec("jpeg", "image/jpeg", "image"),
    "jpeg": FormatSpec("jpeg", "image/jpeg", "image"),
}

# 매크로가 들 수 있는 형식(PRD 10장)과 실행 파일·스크립트는 other로도 받지 않는다
BLOCKED_EXTENSIONS = {
    "xlsm", "xltm", "xlam", "docm", "dotm", "pptm", "potm", "ppsm", "ppam",
    "exe", "dll", "com", "scr", "msi", "msp", "bat", "cmd", "ps1", "psm1", "vbs", "vbe",
    "js", "jse", "wsf", "wsh", "hta", "jar", "sh", "app", "apk", "lnk", "reg", "cpl",
}  # fmt: skip
# other 파일의 mimeType: 서명으로 알아낸 형식별 일반 미디어 타입
OTHER_MIME_TYPES = {
    "zip": "application/zip",
    "ole": "application/x-ole-storage",
    "text": "text/plain",
    "binary": "application/octet-stream",
}

# 형식 분류별 지원 기능. tabular·image는 설계 파일 예시 그대로, text·document는 잠정값
CAPABILITIES: dict[FileCategory, ProcessingCapabilities] = {
    "tabular": ProcessingCapabilities(
        preview=True, quality_check=True, cleaning=True, statistics=True, ai_summary=True
    ),
    "text": ProcessingCapabilities(
        preview=True, quality_check=True, cleaning=False, statistics=True, ai_summary=True
    ),
    "document": ProcessingCapabilities(
        preview=True, quality_check=True, cleaning=False, statistics=False, ai_summary=True
    ),
    "image": ProcessingCapabilities(
        preview=True, quality_check=True, cleaning=False, statistics=False, ai_summary=True
    ),
    "other": ProcessingCapabilities(
        preview=False, quality_check=False, cleaning=False, statistics=False, ai_summary=False
    ),
}

# OOXML 안에서 꼭 있어야 하는 파트와 매크로 파트
OOXML_PARTS = {"xlsx": "xl/workbook.xml", "docx": "word/document.xml"}
OOXML_MACRO_PARTS = {"xl/vbaProject.bin", "word/vbaProject.bin"}


@dataclass(frozen=True)
class ZipLimits:
    """압축 폭탄을 막기 위한 압축 해제 제한."""

    max_uncompressed_bytes: int
    max_entries: int


@dataclass(frozen=True)
class DetectedFormat:
    """검증을 통과한 파일의 형식 정보. files 문서의 extension·mimeType·category가 된다."""

    extension: str
    mime_type: str
    category: FileCategory


def _invalid(extension: str, reason: str) -> AppError:
    """확장자는 지원하지만 내용이 형식에 맞지 않을 때의 오류를 만든다.

    파라미터:
        extension: 업로드한 파일의 확장자
        reason: 사용자에게 보여줄 이유
    """
    return AppError("INVALID_FILE", f"{extension} 파일이 올바르지 않습니다: {reason}", 415)


def sniff_signature(header: bytes) -> str | None:
    """파일 앞부분으로 바이너리 형식을 알아낸다. 알려진 서명이 없으면 None(텍스트 후보).

    파라미터:
        header: 파일의 처음 몇 바이트(8바이트 이상)
    """
    for name, magic in (
        ("pdf", PDF_MAGIC),
        ("png", PNG_MAGIC),
        ("jpeg", JPEG_MAGIC),
        ("zip", ZIP_MAGIC),
        ("ole", OLE_MAGIC),
    ):
        if header.startswith(magic):
            return name
    return None


def decode_text_sample(sample: bytes) -> str | None:
    """텍스트 파일 앞부분을 UTF-8(BOM 포함)·UTF-16(BOM)·CP949 순으로 풀어 본다.

    NUL 바이트가 있거나(바이너리) 어느 인코딩으로도 풀리지 않으면 None.
    샘플 끝에서 잘린 멀티바이트 문자는 무시한다.

    파라미터:
        sample: 파일의 처음 일부
    """
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return sample[: len(sample) // 2 * 2].decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in sample:
        return None
    for encoding in ("utf-8-sig", "cp949"):
        for trim in range(4):  # 끝에서 잘린 문자(최대 3바이트) 허용
            try:
                return sample[: len(sample) - trim].decode(encoding)
            except UnicodeDecodeError:
                continue
    return None


def _check_text(path: Path, extension: str) -> None:
    """텍스트 형식(csv·tsv·json·txt·log) 내용을 확인한다. json은 전체를 파싱해 본다.

    파라미터:
        path: 검사할 파일 경로
        extension: 업로드한 파일의 확장자
    """
    with path.open("rb") as f:
        sample = f.read(TEXT_SAMPLE_BYTES)
    if decode_text_sample(sample) is None:
        raise _invalid(extension, "텍스트로 읽을 수 없습니다.")
    if extension == "json":
        raw = path.read_bytes()
        try:
            json.loads(raw)  # bytes는 UTF-8/16/32를 자동 판별
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _invalid(extension, "JSON 형식이 아닙니다.") from exc


def _check_ooxml(path: Path, extension: str, limits: ZipLimits) -> None:
    """XLSX·DOCX의 ZIP 내부 구조, 매크로 포함 여부, 압축 해제 크기를 확인한다.

    파라미터:
        path: 검사할 파일 경로
        extension: xlsx 또는 docx
        limits: 압축 해제 제한
    """
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise _invalid(extension, "압축 구조가 손상되었습니다.") from exc

    if len(infos) > limits.max_entries:
        raise AppError("ARCHIVE_TOO_LARGE", "압축 안의 파일 수가 너무 많습니다.", 413)
    if sum(info.file_size for info in infos) > limits.max_uncompressed_bytes:
        raise AppError("ARCHIVE_TOO_LARGE", "압축을 풀었을 때의 크기가 너무 큽니다.", 413)

    names = {info.filename for info in infos}
    if names & OOXML_MACRO_PARTS:
        raise AppError("MACRO_NOT_ALLOWED", "매크로가 포함된 문서는 업로드할 수 없습니다.", 415)
    if "[Content_Types].xml" not in names or OOXML_PARTS[extension] not in names:
        raise _invalid(extension, f"{extension} 문서 구조가 아닙니다.")


def _detect_other(path: Path, extension: str, header: bytes) -> DetectedFormat:
    """지원 목록에 없는 확장자를 기본 정보만 등록할 other 형식으로 판별한다.

    실행 파일 서명이 있거나, 내용이 PDF·PNG·JPEG인데 확장자만 다른 파일은 거부한다.

    파라미터:
        path: 검사할 파일 경로
        extension: 파일명에서 얻은 확장자(소문자, 점 없음, 없을 수 있음)
        header: 파일의 처음 8바이트
    """
    if header.startswith(EXECUTABLE_MAGICS):
        raise AppError("UNSUPPORTED_FILE_TYPE", "실행 파일은 업로드할 수 없습니다.", 415)
    signature = sniff_signature(header)
    if signature in ("pdf", "png", "jpeg"):
        raise AppError(
            "FILE_TYPE_MISMATCH",
            f"확장자({extension or '없음'})와 실제 파일 형식({signature})이 다릅니다.",
            415,
        )
    if signature is None:
        with path.open("rb") as f:
            signature = "text" if decode_text_sample(f.read(TEXT_SAMPLE_BYTES)) else "binary"
    return DetectedFormat(
        extension=extension, mime_type=OTHER_MIME_TYPES[signature], category="other"
    )


def detect_format(path: Path, extension: str, limits: ZipLimits) -> DetectedFormat:
    """확장자와 실제 내용이 같은 형식인지 검증하고 형식 정보를 돌려준다.

    지원 목록에 없는 확장자는 other로 등록한다(내용 분석 없음). 매크로·실행 파일 확장자,
    실행 파일 서명, 확장자와 서명이 다른 파일, 손상된 파일, 매크로 문서,
    압축 폭탄은 AppError로 거부한다.

    파라미터:
        path: 검사할 파일 경로(업로드 임시 파일)
        extension: 파일명에서 얻은 확장자(소문자, 점 없음)
        limits: 압축 해제 제한
    """
    if extension in BLOCKED_EXTENSIONS:
        raise AppError(
            "UNSUPPORTED_FILE_TYPE",
            f"보안상 업로드할 수 없는 파일 형식입니다: {extension}",
            415,
        )
    with path.open("rb") as f:
        header = f.read(8)
    spec = SUPPORTED.get(extension)
    if spec is None:
        return _detect_other(path, extension, header)

    actual = sniff_signature(header) or "text"
    if actual != spec.signature:
        raise AppError(
            "FILE_TYPE_MISMATCH",
            f"확장자({extension})와 실제 파일 형식({actual})이 다릅니다.",
            415,
        )

    if spec.signature == "text":
        _check_text(path, extension)
    elif spec.signature == "zip":
        _check_ooxml(path, extension, limits)

    return DetectedFormat(extension=extension, mime_type=spec.mime_type, category=spec.category)
