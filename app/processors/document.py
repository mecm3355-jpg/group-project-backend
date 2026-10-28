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

# 글자 층을 직접 읽는 문서 형식(python-docx·pdfplumber가 읽을 수 있는 것만)
DOCUMENT_EXTENSIONS = {"docx", "pdf"}
# OCR로만 글자를 얻는 이미지 형식. jpg·jpeg는 같은 형식이지만 확장자 표기가 둘이라 둘 다 둔다
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
    # 300dpi는 Tesseract가 권장하는 해상도. 낮추면 한글 획이 뭉개지고, 높이면 시간만 늘어난다
    dpi: int = 300
    # 이 값 미만인 줄은 사람이 다시 볼 줄로 센다(PRD 7.6 검수 대상). 설정으로 바꿀 수 있다
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
    # 이미지 파일일 때만 있다. source_image는 with 블록 안에서만 쓸 수 있다(블록이 끝나면 닫힘)
    image: ImageInfo | None = None
    source_image: Image.Image | None = None

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
        # 왼쪽 들여쓰기는 표·목록 구조일 수 있어 남기고, 끝 공백만 지운다
        line = line.rstrip()
        # 공백만 있는 줄은 내용이 없으므로 줄 수·민원 분리에서 빠지도록 버린다
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
    # 엔진이 돌려준 줄 중 빈 줄은 버리고, 띄어쓰기 교정은 신뢰도와 무관하게 글자만 바꾼다
    entries = [
        DocLine(
            text=fix_spacing(line.text) if options.fix_spacing else line.text,
            page=page,
            confidence=line.confidence,
        )
        for line in options.engine.recognize(image)
        if line.text.strip()
    ]
    # 글자가 하나라도 나온 페이지만 "OCR한 페이지"로 센다(빈 페이지는 pages_without_text 몫)
    if entries:
        result.ocr_pages += 1
    low = 0
    for entry in entries:
        # 신뢰도를 모르는 줄은 0으로 보아 검수 대상에 넣는다(놓치는 쪽보다 안전)
        confidence = entry.confidence or 0.0
        result.ocr_line_count += 1
        result.ocr_confidence_total += confidence
        if confidence < options.low_confidence:
            low += 1
    result.ocr_low_confidence_lines += low
    # 검수할 페이지 목록은 페이지 단위로 한 번만 넣어 화면에서 바로 찾아가게 한다
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
    # 문단은 줄바꿈(Shift+Enter)이 섞여 있을 수 있어 줄로 다시 나눈다
    if isinstance(block, Paragraph):
        for line in _split_lines(block.text):
            yield DocLine(text=line, paragraph=index)
        return
    result.table_count += 1
    for row_index, row in enumerate(block.rows):
        # 칸 안의 줄바꿈·연속 공백은 한 칸으로 접어, 탭이 칸 경계로만 쓰이게 한다
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
        # entries는 아래에서 지연 반복자로 바꿔 끼운다. 먼저 빈 반복자로 객체를 만들어
        # 반복 중에 표 개수 같은 통계를 이 객체에 쌓을 수 있게 한다
        result = DocumentText("docx", iter(()))

        def entries() -> Iterator[DocLine]:
            """본문 요소를 차례로 줄로 바꾼다.

            파라미터: 없음(바깥의 document·result를 쓴다)
            """
            # iter_inner_content는 문단과 표를 문서에 나온 순서대로 준다(paragraphs·tables를
            # 따로 돌면 표가 본문 어디에 있었는지 잃는다)
            for index, block in enumerate(document.iter_inner_content()):
                yield from _docx_entries(block, index, result)

        result.entries = entries()
        # with 블록 안에서만 읽게 해, 큰 문서도 줄 단위로 흘려보내고 파일은 블록 끝에 닫는다
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
        # 여기부터 예외가 나도 pdf를 닫도록 try/finally로 감싼다(pdfplumber는 파일을 붙잡는다)
        try:
            result = DocumentText("pdf", iter(()), page_count=page_count)
            # OCR 엔진 이름은 엔진이 있을 때만 남겨, 진단에서 "OCR 미설정"과 구분하게 한다
            if ocr is not None:
                result.ocr_engine = ocr.engine.name
                result.low_confidence = ocr.low_confidence

            def entries() -> Iterator[DocLine]:
                """페이지를 하나씩 읽어 줄로 바꾼다. 읽은 페이지는 바로 닫아 메모리를 줄인다.

                파라미터: 없음(바깥의 pdf·ocr·result를 쓴다)
                """
                for page in pdf.pages:
                    number = page.page_number
                    try:
                        # 글자 층이 있으면 OCR보다 정확하고 빠르므로 먼저 읽는다
                        text = page.extract_text() or ""
                        scanned: list[DocLine] = []
                        # 글자 층이 비었을 때만 렌더링해 OCR한다(스캔 페이지). 텍스트형 PDF에
                        # 렌더링 비용을 쓰지 않으려는 것
                        if not text.strip() and ocr is not None:
                            image = page.to_image(resolution=ocr.dpi).original
                            scanned = _ocr_entries(image, number, ocr, result)
                    except AppError:
                        # OCR 쪽에서 이미 의미 있는 오류로 바꾼 것은 그대로 올린다
                        raise
                    except Exception as exc:  # 페이지 내용이 깨진 경우
                        # 몇 쪽이 깨졌는지 알려 사용자가 원본을 확인할 수 있게 한다
                        raise AppError(
                            "INVALID_FILE", f"PDF {number}쪽을 읽을 수 없습니다.", 422
                        ) from exc
                    finally:
                        # pdfplumber는 페이지 객체에 글자·도형을 캐시하므로 바로 닫아야
                        # 수백 쪽 PDF에서도 메모리가 쌓이지 않는다
                        page.close()
                    # yield는 페이지를 닫은 뒤에 한다(소비자가 오래 붙잡아도 페이지는 풀림)
                    if text.strip():
                        for line in _split_lines(text):
                            yield DocLine(text=line, page=number)
                    elif scanned:
                        yield from scanned
                    else:
                        # 글자 층도 OCR 결과도 없는 페이지: 품질 진단의 "OCR 필요" 근거가 된다
                        result.pages_without_text += 1

            result.entries = entries()
            yield result
        finally:
            pdf.close()


def load_image(path: Path) -> Image.Image:
    """이미지 파일을 열어 픽셀까지 읽는다. 다 쓰면 close해야 한다.

    손상된 파일은 INVALID_FILE(422), 화소 수가 Pillow 한도(약 1억 8천만)를 넘는
    파일(압축 폭탄 방지)은 IMAGE_TOO_LARGE(413).

    파라미터:
        path: 파일 경로
    """
    # Image.open은 머리말만 읽는다. 화소 수 한도는 여기서 걸리므로 압축 폭탄은 픽셀을
    # 풀기 전에 막힌다
    try:
        image = Image.open(path)
    except Image.DecompressionBombError as exc:
        raise AppError(
            "IMAGE_TOO_LARGE", "이미지 화소 수가 너무 많아 읽을 수 없습니다.", 413
        ) from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise AppError("INVALID_FILE", "이미지 파일을 읽을 수 없습니다.", 422) from exc
    # 픽셀을 지금 다 읽어 둔다. 머리말은 멀쩡하고 본문이 잘린 파일은 여기서야 드러나므로,
    # 나중에 OCR·썸네일 도중 터지지 않고 422로 바로 알린다
    try:
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        # 실패해도 파일 핸들은 닫아야 Windows에서 원본 삭제가 막히지 않는다
        image.close()
        raise AppError("INVALID_FILE", "이미지 파일을 읽을 수 없습니다.", 422) from exc
    return image


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
    # 파일 문제가 아니라 서버 설정 문제이므로 422가 아닌 503으로 알린다
    if ocr is None and not allow_missing_ocr:
        raise AppError(
            "OCR_NOT_AVAILABLE",
            "이미지를 읽으려면 OCR(Tesseract)이 필요합니다. 설치·설정(TESSERACT_*)을 확인하세요.",
            503,
        )
    image = load_image(path)
    # 메타데이터는 OCR 여부와 상관없이 남긴다(OCR 없는 환경의 이미지 진단 근거)
    info = ImageInfo(image.format, image.width, image.height, image.mode)
    try:
        # OCR 없이 연 경우: 글자 줄은 비우고, 썸네일·메타데이터용 원본만 넘긴다
        if ocr is None:
            yield DocumentText("image", iter(()), page_count=1, image=info, source_image=image)
            return
        result = DocumentText(
            "image",
            iter(()),
            page_count=1,
            ocr_engine=ocr.engine.name,
            low_confidence=ocr.low_confidence,
            image=info,
            source_image=image,
        )

        def entries() -> Iterator[DocLine]:
            """이미지 한 장을 OCR한다.

            파라미터: 없음(바깥의 image·ocr·result를 쓴다)
            """
            # 팔레트(P)·투명(RGBA)·CMYK 이미지는 Tesseract가 잘못 읽으므로 RGB로 맞춘다
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
    # 확장자는 업로드 때 실제 형식 검증(file_validation)을 마친 값이라 그대로 믿고 고른다
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
