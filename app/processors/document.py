"""문서 파일(DOCX·텍스트형 PDF)에서 글자를 줄 단위로 뽑는다.

스캔 PDF·이미지 OCR(PRD M3)과 민원 단위 분리 규칙(PRD 12장 미결정)은 아직 하지 않는다.
글자가 없는 PDF 페이지는 세어 두었다가 품질 진단에서 "OCR 필요"로 알린다.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import docx
import pdfplumber
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph

from app.core.errors import AppError

DOCUMENT_EXTENSIONS = {"docx", "pdf"}


@dataclass
class DocumentText:
    """읽어 들인 문서. lines는 빈 줄을 뺀 글자 줄의 반복자(한 번만 돌 수 있음).

    page_count는 PDF만 연 직후에 정해지고, pages_without_text·table_count는
    lines를 끝까지 읽은 뒤에 확정된다.
    """

    format: str
    lines: Iterator[str]
    page_count: int | None = None
    pages_without_text: int = 0
    table_count: int = 0


def _split_lines(text: str) -> Iterator[str]:
    """여러 줄 글자를 줄로 나누고 빈 줄은 버린다(오른쪽 공백은 지운다).

    파라미터:
        text: 문단·페이지에서 뽑은 글자
    """
    for line in text.splitlines():
        line = line.rstrip()
        if line.strip():
            yield line


def _docx_lines(block: DocxTable | Paragraph, result: DocumentText) -> Iterator[str]:
    """DOCX 본문 요소 하나(문단 또는 표)를 줄로 바꾼다. 표는 행마다 칸을 탭으로 이어 한 줄로 만든다.

    병합된 칸은 python-docx가 칸마다 같은 글자를 돌려주므로 그대로 반복된다.

    파라미터:
        block: 본문의 문단 또는 표
        result: 표 개수를 쌓을 문서 객체
    """
    if isinstance(block, Paragraph):
        yield from _split_lines(block.text)
        return
    result.table_count += 1
    for row in block.rows:
        line = "\t".join(" ".join(cell.text.split()) for cell in row.cells)
        if line.strip():
            yield line


@contextmanager
def _open_docx(path: Path) -> Iterator[DocumentText]:
    """DOCX를 연다. 본문의 문단과 표를 문서 순서대로 읽는다(머리글·바닥글·메모는 뺀다).

    원본은 저장소에 확장자 없이 저장되므로 경로 대신 열린 파일 객체를 넘긴다.

    파라미터:
        path: 파일 경로
    """
    with path.open("rb") as stream:
        try:
            document = docx.Document(stream)
        except Exception as exc:  # python-docx는 손상 유형마다 다른 예외를 던진다
            raise AppError("INVALID_FILE", "DOCX 파일을 읽을 수 없습니다.", 422) from exc
        result = DocumentText("docx", iter(()))

        def lines() -> Iterator[str]:
            """본문 요소를 차례로 줄로 바꾼다."""
            for block in document.iter_inner_content():
                yield from _docx_lines(block, result)

        result.lines = lines()
        yield result


@contextmanager
def _open_pdf(path: Path) -> Iterator[DocumentText]:
    """텍스트형 PDF를 연다. 페이지마다 글자 층을 읽고, 글자가 없는 페이지 수를 센다.

    파라미터:
        path: 파일 경로
    """
    with path.open("rb") as stream:
        try:
            pdf = pdfplumber.open(stream)
            page_count = len(pdf.pages)
        except Exception as exc:  # 손상·암호 PDF는 pdfminer가 여러 예외를 던진다
            raise AppError(
                "INVALID_FILE", "PDF 파일을 읽을 수 없습니다(손상 또는 암호 설정).", 422
            ) from exc
        try:
            result = DocumentText("pdf", iter(()), page_count=page_count)

            def lines() -> Iterator[str]:
                """페이지를 하나씩 읽어 줄로 바꾼다. 읽은 페이지는 바로 닫아 메모리를 줄인다."""
                for page in pdf.pages:
                    try:
                        text = page.extract_text() or ""
                    except Exception as exc:  # 페이지 내용이 깨진 경우
                        raise AppError(
                            "INVALID_FILE",
                            f"PDF {page.page_number}쪽을 읽을 수 없습니다.",
                            422,
                        ) from exc
                    finally:
                        page.close()
                    if not text.strip():
                        result.pages_without_text += 1
                    yield from _split_lines(text)

            result.lines = lines()
            yield result
        finally:
            pdf.close()


@contextmanager
def open_document(path: Path, extension: str) -> Iterator[DocumentText]:
    """확장자에 맞는 추출기로 문서를 연다. with 블록 안에서 document.lines를 끝까지 읽는다.

    파라미터:
        path: 파일 경로
        extension: docx 또는 pdf
    """
    if extension == "docx":
        opener = _open_docx(path)
    elif extension == "pdf":
        opener = _open_pdf(path)
    else:
        raise AppError("NOT_DOCUMENT", f"{extension} 파일은 문서로 읽을 수 없습니다.", 422)
    with opener as document:
        yield document
