"""텍스트 파일(TXT·LOG)을 줄 단위로 읽는다."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import AppError
from app.processors.encoding import detect_encoding

TEXT_EXTENSIONS = {"txt", "log"}


@dataclass
class TextLines:
    """읽어 들인 텍스트. lines는 줄바꿈을 뺀 줄의 반복자(한 번만 돌 수 있음)."""

    format: str
    encoding: str
    lines: Iterator[str]


@contextmanager
def open_text(path: Path, extension: str) -> Iterator[TextLines]:
    """텍스트 파일을 연다. 인코딩은 자동으로 정한다.

    파라미터:
        path: 파일 경로
        extension: txt 또는 log
    """
    encoding = detect_encoding(path)
    try:
        with path.open(encoding=encoding, newline=None) as f:
            yield TextLines(extension, encoding, (line.rstrip("\n") for line in f))
    except UnicodeDecodeError as exc:
        raise AppError("ENCODING_ERROR", "파일 인코딩을 읽는 중 오류가 발생했습니다.", 422) from exc
