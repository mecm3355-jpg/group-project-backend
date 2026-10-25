import codecs
from pathlib import Path

from app.core.errors import AppError

CHUNK_BYTES = 1024 * 1024


def _decodes_fully(path: Path, encoding: str) -> bool:
    """파일 전체가 해당 인코딩으로 풀리는지 조각 단위로 확인한다.

    파라미터:
        path: 확인할 파일
        encoding: 시도할 인코딩
    """
    decoder = codecs.getincrementaldecoder(encoding)()
    try:
        with path.open("rb") as f:
            while chunk := f.read(CHUNK_BYTES):
                decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def detect_encoding(path: Path) -> str:
    """텍스트 파일의 인코딩을 알아낸다. BOM이 있으면 그것을, 없으면 UTF-8 → CP949 순으로 본다.

    어느 것으로도 끝까지 읽히지 않으면 AppError(ENCODING_ERROR).

    파라미터:
        path: 텍스트 파일 경로
    """
    with path.open("rb") as f:
        head = f.read(4)
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    for encoding in ("utf-8", "cp949"):
        if _decodes_fully(path, encoding):
            return encoding
    raise AppError("ENCODING_ERROR", "파일 인코딩을 알 수 없습니다(UTF-8·CP949가 아님).", 422)
