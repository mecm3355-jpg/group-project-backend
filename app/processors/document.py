"""문서 파일(DOCX·PDF)과 이미지(PNG·JPG)에서 글자를 줄 단위로 뽑는다.

- 텍스트형 PDF·DOCX는 글자 층을 그대로 읽는다.
- 글자 층이 없는 PDF 페이지와 이미지는 OCR 엔진이 있으면 300dpi로 렌더링해 OCR한다(PRD 7.6).
  OCR 엔진이 없으면 PDF는 글자 없는 페이지로 세어 품질 진단에서 "OCR 필요"로 알리고,
  이미지는 읽을 수 없다는 오류를 낸다(품질 진단·미리보기는 allow_missing_ocr로 크기 정보만 읽는다).
- 이미지는 형식·가로·세로·색상 모드를 함께 남긴다.
- 줄마다 원문 위치(PDF·이미지 page, DOCX paragraph·tableRow)와 OCR 신뢰도를 남긴다
  (complaints.sourceLocator·extractionConfidence, PRD 7.4).
민원 단위 분리는 app/processors/splitter.py가 맡는다.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import docx
import pdfplumber
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph
from PIL import Image, UnidentifiedImageError

from app.core.errors import AppError
from app.processors.korean import fix_spacing
from app.processors.ocr import OcrEngine

DOCUMENT_EXTENSIONS = {"docx", "pdf"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg"}


@dataclass(frozen=True)
class DocLine:
    """문서의 글자 한 줄과 원문 위치. confidence는 OCR로 읽은 줄만 있다(0~1)."""

    text: str
    page: int | None = None
    paragraph: int | None = None
    table_row: int | None = None
    confidence: float | None = None


@dataclass
class OcrOptions:
    """OCR 설정: 엔진, 렌더링 해상도, 검수 기준 신뢰도."""

    engine: OcrEngine
    dpi: int = 300
    low_confidence: float = 0.6
    # OCR 줄의 띄어쓰기를 다시 잡는다(Tesseract 한국어는 글자 사이에 공백을 자주 넣는다)
    fix_spacing: bool = True


@dataclass(frozen=True)
class ImageInfo:
    """이미지 메타데이터: 실제 형식(PNG·JPEG), 가로·세로 픽셀, 색상 모드(RGB 등)."""

    format: str | None
    width: int
    height: int
    mode: str


@dataclass
class DocumentText:
    """읽어 들인 문서. entries는 빈 줄을 뺀 글자 줄의 반복자(한 번만 돌 수 있음).

    page_count는 PDF·이미지를 연 직후에 정해지고, 나머지 수치는 entries를 끝까지 읽은 뒤에 확정된다.
    """

    format: str
    entries: Iterator[DocLine]
    page_count: int | None = None
    pages_without_text: int = 0
    table_count: int = 0
    # OCR 통계: 엔진 이름, OCR한 페이지 수, OCR 줄 수, 신뢰도 합, 검수 기준 미만 줄 수
    ocr_engine: str | None = None
    ocr_pages: int = 0
    ocr_line_count: int = 0
    ocr_confidence_total: float = 0.0
    ocr_low_confidence_lines: int = 0
    low_confidence: float = 0.6
    ocr_low_confidence_pages: list[int] = field(default_factory=list)
    # 이미지 파일일 때만 있다
    image: ImageInfo | None = None

    @property
    def lines(self) -> Iterator[str]:
        """글자만 필요한 곳(미리보기·품질 진단)에서 쓰는 줄 반복자. entries와 같은 반복을 쓴다.

        파라미터: 없음
        """
        return (entry.text for entry in self.entries)


def _split_lines(text: str) -> Iterator[str]:
    """여러 줄 글자를 줄로 나누고 빈 줄은 버린다(오른쪽 공백은 지운다).

    파라미터:
        text: 문단·페이지에서 뽑은 글자
    """
    for line in text.splitlines():
        line = line.rstrip()
        if line.strip():
            yield line


def _ocr_entries(
    image: Image.Image, page: int, options: OcrOptions, result: DocumentText
) -> list[DocLine]:
    """이미지 한 장을 OCR해 줄로 바꾸고 OCR 통계를 쌓는다.

    파라미터:
        image: 렌더링한 페이지 또는 원본 이미지
        page: 페이지 번호(1부터)
        options: OCR 설정
        result: 통계를 쌓을 문서 객체
    """
    entries = [
        DocLine(
            text=fix_spacing(line.text) if options.fix_spacing else line.text,
            page=page,
            confidence=line.confidence,
        )
        for line in options.engine.recognize(image)
        if line.text.strip()
    ]
    if entries:
        result.ocr_pages += 1
    low = 0
    for entry in entries:
        confidence = entry.confidence or 0.0
        result.ocr_line_count += 1
        result.ocr_confidence_total += confidence
        if confidence < options.low_confidence:
            low += 1
    result.ocr_low_confidence_lines += low
    if low:
        result.ocr_low_confidence_pages.append(page)
    return entries


def _docx_entries(
    block: DocxTable | Paragraph, index: int, result: DocumentText
) -> Iterator[DocLine]:
    """DOCX 본문 요소 하나(문단 또는 표)를 줄로 바꾼다. 표는 행마다 칸을 탭으로 이어 한 줄로 만든다.

    병합된 칸은 python-docx가 칸마다 같은 글자를 돌려주므로 그대로 반복된다.

    파라미터:
        block: 본문의 문단 또는 표
        index: 본문 요소 순번(0부터, 원문 위치 paragraph)
        result: 표 개수를 쌓을 문서 객체
    """
    if isinstance(block, Paragraph):
        for line in _split_lines(block.text):
            yield DocLine(text=line, paragraph=index)
        return
    result.table_count += 1
    for row_index, row in enumerate(block.rows):
        line = "\t".join(" ".join(cell.text.split()) for cell in row.cells)
        if line.strip():
            yield DocLine(text=line, paragraph=index, table_row=row_index)


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

        def entries() -> Iterator[DocLine]:
            """본문 요소를 차례로 줄로 바꾼다."""
            for index, block in enumerate(document.iter_inner_content()):
                yield from _docx_entries(block, index, result)

        result.entries = entries()
        yield result


@contextmanager
def _open_pdf(path: Path, ocr: OcrOptions | None) -> Iterator[DocumentText]:
    """PDF를 연다. 페이지마다 글자 층을 읽고, 글자 층이 없으면 OCR한다(엔진이 있을 때).

    파라미터:
        path: 파일 경로
        ocr: OCR 설정. None이면 글자 층이 없는 페이지 수만 센다
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
            if ocr is not None:
                result.ocr_engine = ocr.engine.name
                result.low_confidence = ocr.low_confidence

            def entries() -> Iterator[DocLine]:
                """페이지를 하나씩 읽어 줄로 바꾼다. 읽은 페이지는 바로 닫아 메모리를 줄인다."""
                for page in pdf.pages:
                    number = page.page_number
                    try:
                        text = page.extract_text() or ""
                        scanned: list[DocLine] = []
                        if not text.strip() and ocr is not None:
                            image = page.to_image(resolution=ocr.dpi).original
                            scanned = _ocr_entries(image, number, ocr, result)
                    except AppError:
                        raise
                    except Exception as exc:  # 페이지 내용이 깨진 경우
                        raise AppError(
                            "INVALID_FILE", f"PDF {number}쪽을 읽을 수 없습니다.", 422
                        ) from exc
                    finally:
                        page.close()
                    if text.strip():
                        for line in _split_lines(text):
                            yield DocLine(text=line, page=number)
                    elif scanned:
                        yield from scanned
                    else:
                        result.pages_without_text += 1

            result.entries = entries()
            yield result
        finally:
            pdf.close()


@contextmanager
def _open_image(
    path: Path, ocr: OcrOptions | None, allow_missing_ocr: bool
) -> Iterator[DocumentText]:
    """이미지(PNG·JPG)를 OCR로 읽는다.

    OCR 엔진이 없으면 AppError(OCR_NOT_AVAILABLE). allow_missing_ocr이면 오류 대신
    글자 없이(ocr_engine None) 메타데이터만 채운다.

    파라미터:
        path: 파일 경로
        ocr: OCR 설정
        allow_missing_ocr: OCR 엔진이 없어도 메타데이터만으로 열지 여부
    """
    if ocr is None and not allow_missing_ocr:
        raise AppError(
            "OCR_NOT_AVAILABLE",
            "이미지를 읽으려면 OCR(Tesseract)이 필요합니다. 설치·설정(TESSERACT_*)을 확인하세요.",
            503,
        )
    try:
        image = Image.open(path)
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise AppError("INVALID_FILE", "이미지 파일을 읽을 수 없습니다.", 422) from exc
    info = ImageInfo(image.format, image.width, image.height, image.mode)
    if ocr is None:
        image.close()
        yield DocumentText("image", iter(()), page_count=1, image=info)
        return
    result = DocumentText(
        "image",
        iter(()),
        page_count=1,
        ocr_engine=ocr.engine.name,
        low_confidence=ocr.low_confidence,
        image=info,
    )
    try:

        def entries() -> Iterator[DocLine]:
            """이미지 한 장을 OCR한다."""
            found = _ocr_entries(image.convert("RGB"), 1, ocr, result)
            if not found:
                result.pages_without_text += 1
            yield from found

        result.entries = entries()
        yield result
    finally:
        image.close()


@contextmanager
def open_document(
    path: Path,
    extension: str,
    ocr: OcrOptions | None = None,
    allow_missing_ocr: bool = False,
) -> Iterator[DocumentText]:
    """확장자에 맞는 추출기로 문서를 연다. with 블록 안에서 document.entries를 끝까지 읽는다.

    파라미터:
        path: 파일 경로
        extension: docx, pdf, png, jpg, jpeg 중 하나
        ocr: OCR 설정(스캔 PDF·이미지용). None이면 OCR하지 않는다
        allow_missing_ocr: OCR 없이 이미지를 열 때 오류 대신 메타데이터만 채울지 여부
            (품질 진단·미리보기용. 민원 추출처럼 글자가 꼭 필요한 곳은 False)
    """
    if extension == "docx":
        opener = _open_docx(path)
    elif extension == "pdf":
        opener = _open_pdf(path, ocr)
    elif extension in IMAGE_EXTENSIONS:
        opener = _open_image(path, ocr, allow_missing_ocr)
    else:
        raise AppError("NOT_DOCUMENT", f"{extension} 파일은 문서로 읽을 수 없습니다.", 422)
    with opener as document:
        yield document
