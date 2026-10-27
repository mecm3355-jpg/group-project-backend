"""텍스트 파일(TXT·LOG)을 줄 단위로 읽는다."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import AppError
from app.processors.encoding import detect_encoding

# 분석 결과 format 값(schema.md TXT·LOG 예시의 "log")으로 확장자를 그대로 쓴다
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
    # 인코딩 판별은 파일 전체를 한 번 훑는다. 판별이 끝난 뒤에 줄 단위로 흘려 읽는다
    encoding = detect_encoding(path)
    # 줄이 지연으로 읽히므로 디코딩 오류는 호출자가 반복하는 도중 이 yield 지점으로 돌아온다
    try:
        # newline=None(유니버설 줄바꿈): \r\n·\r도 \n으로 바꿔 줘 윈도·맥 로그를 같게 다룬다
        with path.open(encoding=encoding, newline=None) as f:
            # 줄바꿈 문자만 떼고 다른 공백은 남긴다(줄 끝 공백도 품질 진단 대상일 수 있음)
            yield TextLines(extension, encoding, (line.rstrip("\n") for line in f))
    except UnicodeDecodeError as exc:
        raise AppError("ENCODING_ERROR", "파일 인코딩을 읽는 중 오류가 발생했습니다.", 422) from exc
