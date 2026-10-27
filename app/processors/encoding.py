import codecs
from pathlib import Path

from app.core.errors import AppError

# 인코딩 확인 때 한 번에 읽는 크기(1MB). 큰 파일도 메모리에 통째로 올리지 않으려는 것
CHUNK_BYTES = 1024 * 1024


def _decodes_fully(path: Path, encoding: str) -> bool:
    """파일 전체가 해당 인코딩으로 풀리는지 조각 단위로 확인한다.

    파라미터:
        path: 확인할 파일
        encoding: 시도할 인코딩
    """
    # 증분 디코더는 조각 경계에서 잘린 멀티바이트 글자(한글 3바이트 등)를 다음 조각과
    # 이어 풀어 주므로, 경계 때문에 잘못 실패하지 않는다
    decoder = codecs.getincrementaldecoder(encoding)()
    try:
        with path.open("rb") as f:
            while chunk := f.read(CHUNK_BYTES):
                decoder.decode(chunk)
        # 마지막에 남은 바이트가 글자를 완성하지 못하면 여기서 오류가 난다(잘린 파일)
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
    # BOM은 파일 맨 앞 최대 4바이트(UTF-32)라 그만큼만 본다
    with path.open("rb") as f:
        head = f.read(4)
    # 엑셀이 "CSV UTF-8"로 저장하면 BOM이 붙는다. utf-8-sig로 읽어야 첫 머리글에 BOM이 안 섞인다
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    # "utf-16"은 BOM으로 바이트 순서를 스스로 판단하고 BOM을 떼어 준다
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    # UTF-8을 먼저 본다. CP949는 받아들이는 바이트 범위가 넓어 UTF-8 파일도 깨진 글자로
    # 풀리는 경우가 많지만, UTF-8은 규칙이 엄격해 CP949 파일을 잘못 통과시키는 일이 드물다.
    # CP949는 한국 공공기관·구형 엑셀 CSV의 기본 인코딩이라 두 번째 후보로 둔다
    for encoding in ("utf-8", "cp949"):
        if _decodes_fully(path, encoding):
            return encoding
    raise AppError("ENCODING_ERROR", "파일 인코딩을 알 수 없습니다(UTF-8·CP949가 아님).", 422)
