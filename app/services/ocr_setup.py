"""설정으로 OCR 엔진을 준비한다. Tesseract나 언어 모델이 없으면 None을 돌려준다."""

from functools import lru_cache
from pathlib import Path

from app.core.config import Settings
from app.processors.document import OcrOptions
from app.processors.ocr import CachedOcrEngine, OcrEngine, TesseractEngine, find_tesseract


# 엔진 찾기·언어 확인은 외부 프로그램을 실행해 느리므로 설정 조합별로 결과를 기억한다.
# 4개면 테스트·스크립트에서 설정을 바꿔 가며 써도 충분하다
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
    # OCR이 없는 환경도 정상 동작해야 하므로 오류 대신 None을 돌려, 호출하는 쪽이
    # "OCR 필요" 진단이나 메타데이터만 읽기로 넘어가게 한다
    if engine is None:
        return None
    # 캐시는 저장소 안에 둔다. 업로드 진단과 민원 분석이 같은 스캔본을 다시 OCR하지 않게 하고,
    # 원본과 같은 위치라 외부로 나가지 않는다
    cached: OcrEngine = CachedOcrEngine(engine, (settings.storage_root / "ocr-cache").resolve())
    return OcrOptions(
        engine=cached, dpi=settings.ocr_dpi, low_confidence=settings.ocr_low_confidence
    )
