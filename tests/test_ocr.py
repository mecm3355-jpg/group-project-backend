from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.core.config import Settings
from app.core.errors import AppError
from app.processors.document import DocLine, OcrOptions, open_document
from app.processors.ocr import OcrLine, TesseractEngine, find_tesseract, parse_tsv
from app.services.ocr_setup import ocr_options
from app.services.profiling import profile_document
from tests.document_files import make_pdf

KOREAN_FONT = Path(r"C:\Windows\Fonts\malgun.ttf")


class FakeEngine:
    """페이지마다 정해 둔 줄을 돌려주는 가짜 OCR. 받은 이미지 수를 센다."""

    name = "fake-ocr"

    def __init__(self, pages: list[list[OcrLine]]) -> None:
        """가짜 엔진을 만든다.

        파라미터:
            pages: 호출 순서대로 돌려줄 줄 목록
        """
        self._pages = list(pages)
        self.calls = 0

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        """다음 페이지의 줄을 돌려준다.

        파라미터:
            image: 이미지(크기만 확인)
        """
        assert image.width > 0
        self.calls += 1
        return self._pages.pop(0)


def read_entries(path: Path, extension: str, engine: FakeEngine) -> list[DocLine]:
    """가짜 OCR로 문서를 끝까지 읽는다.

    파라미터:
        path: 파일 경로
        extension: 확장자
        engine: 가짜 OCR 엔진
    """
    with open_document(path, extension, OcrOptions(engine, dpi=72)) as document:
        return list(document.entries)


def test_parse_tsv_groups_words_into_lines() -> None:
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "1\t1\t0\t0\t0\t0\t0\t0\t10\t10\t-1\t",
            "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t90\t가로등이",
            "5\t1\t1\t1\t1\t2\t0\t0\t10\t10\t70\t고장",
            "5\t1\t1\t1\t2\t1\t0\t0\t10\t10\t50\t수청동",
        ]
    )  # fmt: skip

    assert parse_tsv(tsv) == [OcrLine("가로등이 고장", 0.8), OcrLine("수청동", 0.5)]


def test_scanned_pdf_pages_are_ocr_ed_with_page_and_confidence(tmp_path: Path) -> None:
    path = tmp_path / "scan"
    path.write_bytes(make_pdf([["Text layer"], [], []]))
    engine = FakeEngine([[OcrLine("가로등 고장", 0.95)], []])

    with open_document(path, "pdf", OcrOptions(engine, dpi=72, low_confidence=0.6)) as document:
        entries = list(document.entries)
        profile = profile_document(document)

    assert engine.calls == 2  # 글자 층이 없는 2·3쪽만 OCR한다
    assert entries == [
        DocLine("Text layer", page=1),
        DocLine("가로등 고장", page=2, confidence=0.95),
    ]
    # 3쪽은 OCR해도 글자가 없어 "글자 없는 페이지"로 남는다
    assert (document.ocr_pages, document.pages_without_text) == (1, 1)
    assert profile.analysis["method"] == "text+ocr"


def test_low_confidence_lines_need_review(tmp_path: Path) -> None:
    path = tmp_path / "scan"
    path.write_bytes(make_pdf([[], []]))
    engine = FakeEngine([[OcrLine("좋음", 0.9), OcrLine("흐림", 0.4)], [OcrLine("흐림", 0.3)]])

    with open_document(path, "pdf", OcrOptions(engine, dpi=72, low_confidence=0.6)) as document:
        profile = profile_document(document)

    [issue] = profile.quality.issues
    assert (issue.type, issue.count, issue.location) == ("low_ocr_confidence", 2, "page 1, 2")
    assert profile.analysis["method"] == "ocr"
    assert profile.analysis["ocr"] == {
        "engine": "fake-ocr",
        "pages": 2,
        "lineCount": 3,
        "meanConfidence": round((0.9 + 0.4 + 0.3) / 3, 4),
        "lowConfidenceLines": 2,
    }


def test_image_is_read_by_ocr(tmp_path: Path) -> None:
    path = tmp_path / "photo"
    Image.new("RGB", (40, 20), "white").save(path, format="PNG")

    entries = read_entries(path, "png", FakeEngine([[OcrLine("불법 주차", 0.88)]]))

    assert entries == [DocLine("불법 주차", page=1, confidence=0.88)]


def test_image_without_ocr_is_error(tmp_path: Path) -> None:
    path = tmp_path / "photo"
    Image.new("RGB", (40, 20), "white").save(path, format="PNG")

    with pytest.raises(AppError) as info, open_document(path, "png") as document:
        list(document.entries)
    assert info.value.code == "OCR_NOT_AVAILABLE"


def test_broken_image_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "photo"
    path.write_bytes(b"\x89PNG\r\n\x1a\nbroken")

    with pytest.raises(AppError) as info:
        read_entries(path, "png", FakeEngine([]))
    assert info.value.code == "INVALID_FILE"


def test_docx_lines_keep_paragraph_and_table_row(tmp_path: Path) -> None:
    from tests.document_files import make_docx

    path = tmp_path / "doc"
    path.write_bytes(make_docx(["첫 문단"], table=[["번호", "제목"], ["1", "가로등"]]))

    with open_document(path, "docx") as document:
        entries = list(document.entries)

    assert entries == [
        DocLine("첫 문단", paragraph=0),
        DocLine("번호\t제목", paragraph=1, table_row=0),
        DocLine("1\t가로등", paragraph=1, table_row=1),
    ]


def test_missing_tesseract_gives_no_engine(tmp_path: Path) -> None:
    assert find_tesseract(str(tmp_path / "none.exe")) is None
    settings = Settings(mongodb_uri="mongodb://unused", tesseract_cmd=str(tmp_path / "none.exe"))

    assert ocr_options(settings) is None


def test_engine_without_language_model_is_unavailable(tmp_path: Path) -> None:
    command = find_tesseract(None)
    if command is None:
        pytest.skip("Tesseract가 설치되어 있지 않습니다")

    engine = TesseractEngine(command, "kor", tessdata_dir=tmp_path)  # 빈 폴더: 언어 모델 없음

    assert not engine.has_languages()


def test_real_tesseract_reads_korean() -> None:
    import os

    tessdata = os.environ.get("TESSERACT_TESSDATA_DIR")
    options = ocr_options(
        Settings(
            mongodb_uri="mongodb://unused",
            tesseract_tessdata_dir=Path(tessdata) if tessdata else None,
        )
    )
    if options is None or not KOREAN_FONT.is_file():
        pytest.skip("Tesseract 한국어 모델 또는 한글 글꼴이 없습니다")
    image = Image.new("RGB", (900, 160), "white")
    ImageDraw.Draw(image).text(
        (30, 40), "가로등 고장 신고", fill="black", font=ImageFont.truetype(str(KOREAN_FONT), 64)
    )

    lines = options.engine.recognize(image)

    assert "".join(line.text for line in lines).replace(" ", "") == "가로등고장신고"
    assert all(0 < line.confidence <= 1 for line in lines)
