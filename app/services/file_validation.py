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

# 형식별 파일 서명(매직 바이트). 파일 맨 앞 몇 바이트만으로 형식을 알 수 있어 내용을 다
# 읽기 전에 빠르게 거른다. ZIP은 XLSX·DOCX, OLE는 구형 XLS·DOC가 공통으로 쓰는 컨테이너다
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

# 텍스트 판별에 읽는 앞부분 크기(64KB). 바이너리면 대개 앞부분에서 NUL이나 깨진 바이트가
# 나오므로 이 정도로 충분하고, 큰 파일 전체를 업로드 요청 안에서 읽지 않아도 된다
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
    # 415(지원하지 않는 미디어 타입): 확장자가 약속한 형식의 내용이 아니므로 받을 수 없는 파일이다
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
    # UTF-16은 영문 글자마다 NUL이 끼므로 NUL 검사보다 먼저 BOM으로 가려낸다.
    # 샘플이 홀수 바이트에서 잘리면 마지막 글자가 반쪽이 되므로 짝수 길이로 자른다
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return sample[: len(sample) // 2 * 2].decode("utf-16")
        except UnicodeDecodeError:
            return None
    # UTF-8·CP949 텍스트에는 NUL이 나오지 않으므로, 있으면 바이너리로 본다
    if b"\x00" in sample:
        return None
    # 순서는 processors/encoding.py와 같다(UTF-8이 더 엄격해 먼저 본다). utf-8-sig는 BOM이
    # 없어도 utf-8과 같게 풀리므로 둘을 한 번에 본다
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
    # JSON은 앞부분만으로는 끝까지 올바른지 알 수 없고, 깨진 JSON은 분석 단계에서 반드시
    # 실패하므로 업로드 때 전체를 파싱해 바로 거부한다(CSV·TXT는 깨진 줄이 있어도 진단 대상)
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
    # 목차(central directory)만 읽고 압축은 풀지 않는다. 압축 폭탄이어도 여기서는 안전하다
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise _invalid(extension, "압축 구조가 손상되었습니다.") from exc

    # 목차에 적힌 원래 크기로 미리 막는다. openpyxl·python-docx가 나중에 풀다가 메모리를
    # 다 쓰는 일을 업로드 단계에서 차단하려는 것
    if len(infos) > limits.max_entries:
        raise AppError("ARCHIVE_TOO_LARGE", "압축 안의 파일 수가 너무 많습니다.", 413)
    if sum(info.file_size for info in infos) > limits.max_uncompressed_bytes:
        raise AppError("ARCHIVE_TOO_LARGE", "압축을 풀었을 때의 크기가 너무 큽니다.", 413)

    names = {info.filename for info in infos}
    # 매크로 파트는 확장자를 .xlsx로 바꿔도 남는다. 확장자 차단(BLOCKED_EXTENSIONS)만으로는
    # 못 막으므로 내부 파트 이름으로 한 번 더 본다
    if names & OOXML_MACRO_PARTS:
        raise AppError("MACRO_NOT_ALLOWED", "매크로가 포함된 문서는 업로드할 수 없습니다.", 415)
    # 모든 OOXML에 있는 [Content_Types].xml과 형식별 본문 파트가 있어야 한다. 일반 ZIP이나
    # DOCX를 .xlsx로 바꾼 파일(서명은 둘 다 ZIP)을 여기서 가려낸다
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
    # 확장자를 바꿔(예: .exe → .dat) 차단 목록을 피한 실행 파일을 내용으로 막는다
    if header.startswith(EXECUTABLE_MAGICS):
        raise AppError("UNSUPPORTED_FILE_TYPE", "실행 파일은 업로드할 수 없습니다.", 415)
    signature = sniff_signature(header)
    # 지원 형식인데 확장자만 틀린 파일은 other로 받으면 분석 기회를 잃으므로, 확장자를 고쳐
    # 다시 올리도록 거부한다. ZIP·OLE는 다른 형식(HWPX·DOC 등)도 많이 써서 other로 둔다
    if signature in ("pdf", "png", "jpeg"):
        raise AppError(
            "FILE_TYPE_MISMATCH",
            f"확장자({extension or '없음'})와 실제 파일 형식({signature})이 다릅니다.",
            415,
        )
    # 서명이 없으면 텍스트인지 한 번 더 보고 mimeType을 text/plain 또는 octet-stream으로 정한다
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
    # 1) 확장자 차단이 가장 싸므로 먼저 한다(파일을 열 필요가 없다)
    if extension in BLOCKED_EXTENSIONS:
        raise AppError(
            "UNSUPPORTED_FILE_TYPE",
            f"보안상 업로드할 수 없는 파일 형식입니다: {extension}",
            415,
        )
    # 2) 서명은 가장 긴 것(PNG·OLE)이 8바이트라 그만큼만 읽는다
    with path.open("rb") as f:
        header = f.read(8)
    spec = SUPPORTED.get(extension)
    if spec is None:
        return _detect_other(path, extension, header)

    # 3) 서명이 없는 파일은 텍스트 후보로 본다(CSV·TXT 등은 서명이 없다).
    # 이렇게 하면 PDF를 .csv로 바꾼 파일은 pdf ≠ text로 걸린다
    actual = sniff_signature(header) or "text"
    if actual != spec.signature:
        raise AppError(
            "FILE_TYPE_MISMATCH",
            f"확장자({extension})와 실제 파일 형식({actual})이 다릅니다.",
            415,
        )

    # 4) 서명이 맞아도 내부 구조를 본다. 서명 확인은 싸고 구조 확인은 비싸므로 이 순서다.
    # PDF·이미지·XLS는 여기서 깊게 보지 않고, 읽을 때 손상이 드러나면 INVALID_FILE로 알린다
    if spec.signature == "text":
        _check_text(path, extension)
    elif spec.signature == "zip":
        _check_ooxml(path, extension, limits)

    return DetectedFormat(extension=extension, mime_type=spec.mime_type, category=spec.category)
