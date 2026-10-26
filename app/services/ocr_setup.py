"""설정으로 OCR 엔진을 준비한다. Tesseract나 언어 모델이 없으면 None을 돌려준다."""

from functools import lru_cache
from pathlib import Path

from app.core.config import Settings
from app.processors.document import OcrOptions
from app.processors.ocr import TesseractEngine, find_tesseract


@lru_cache(maxsize=4)
def _engine(command: str | None, languages: str, tessdata: Path | None) -> TesseractEngine | None:
    """엔진을 한 번만 찾아 만든다.

    파라미터:
        command: TESSERACT_CMD 설정값
        languages: 언어 모델
        tessdata: 언어 모델 폴더
    """
    found = find_tesseract(command)
    if found is None:
        return None
    engine = TesseractEngine(found, languages, tessdata)
    # 언어 모델(kor.traineddata)이 없으면 OCR이 모두 실패하므로 없는 것으로 본다
    return engine if engine.has_languages() else None


def ocr_options(settings: Settings) -> OcrOptions | None:
    """설정(TESSERACT_*, OCR_*)으로 OCR 설정을 만든다. Tesseract를 못 찾으면 None.

    파라미터:
        settings: 앱 설정
    """
    engine = _engine(
        settings.tesseract_cmd, settings.ocr_languages, settings.tesseract_tessdata_dir
    )
    if engine is None:
        return None
    return OcrOptions(
        engine=engine, dpi=settings.ocr_dpi, low_confidence=settings.ocr_low_confidence
    )
