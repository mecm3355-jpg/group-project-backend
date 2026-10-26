"""스캔 PDF·이미지 OCR(PRD 7.6, M3). 로컬 Tesseract(한국어)로만 읽는다. 원본이 밖으로 나가지 않는다.

Tesseract를 명령줄로 직접 부르고 TSV 출력에서 단어별 신뢰도를 모아 줄 단위 신뢰도(0~1)를 만든다.
신뢰도가 낮은 줄은 사람이 검수한다(국민신문고 500건은 OCR만으로 약 28%를 고쳐야 했다).
"""

import csv
import hashlib
import io
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image

from app.core.errors import AppError

# Windows 기본 설치 위치(UB-Mannheim 배포판). PATH에 없을 때 찾아본다
WINDOWS_DEFAULT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
OCR_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class OcrLine:
    """OCR로 읽은 글자 한 줄과 신뢰도(0~1, 단어 신뢰도 평균)."""

    text: str
    confidence: float


class OcrEngine(Protocol):
    """이미지 한 장을 글자 줄로 읽는다. 실제 구현은 TesseractEngine, 테스트에서는 가짜 구현."""

    name: str

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        """이미지의 글자를 위에서 아래 순서의 줄로 돌려준다.

        파라미터:
            image: 읽을 이미지(300dpi 권장)
        """
        ...


def find_tesseract(configured: str | None) -> Path | None:
    """Tesseract 실행 파일 위치. 설정값 → PATH → Windows 기본 설치 위치 순으로 찾는다.

    파라미터:
        configured: 설정(TESSERACT_CMD)에 적힌 경로. 없으면 None
    """
    if configured:
        path = Path(configured)
        return path if path.is_file() else None
    found = shutil.which("tesseract")
    if found:
        return Path(found)
    return WINDOWS_DEFAULT if WINDOWS_DEFAULT.is_file() else None


def parse_tsv(tsv: str) -> list[OcrLine]:
    """Tesseract TSV 출력을 줄 단위로 묶는다. 신뢰도가 -1인 항목(글자 없음)은 뺀다.

    파라미터:
        tsv: tesseract ... tsv 출력
    """
    lines: dict[tuple[int, int, int, int], list[tuple[str, float]]] = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        try:
            confidence = float(row.get("conf") or -1)
        except ValueError:
            continue
        if not text or confidence < 0:
            continue
        key = (
            int(row["page_num"]),
            int(row["block_num"]),
            int(row["par_num"]),
            int(row["line_num"]),
        )
        lines.setdefault(key, []).append((text, confidence))
    result: list[OcrLine] = []
    for key in sorted(lines):
        words = lines[key]
        result.append(
            OcrLine(
                text=" ".join(word for word, _ in words),
                confidence=round(sum(c for _, c in words) / len(words) / 100, 4),
            )
        )
    return result


class TesseractEngine:
    """로컬 Tesseract로 OCR한다(한국어 모델 kor.traineddata 필요)."""

    def __init__(
        self, command: Path, languages: str = "kor", tessdata_dir: Path | None = None
    ) -> None:
        """엔진을 만든다.

        파라미터:
            command: tesseract 실행 파일
            languages: 언어 모델(예: kor, kor+eng)
            tessdata_dir: 언어 모델 폴더. None이면 Tesseract 기본 폴더
        """
        self.name = f"tesseract:{languages}"
        self._command = command
        self._languages = languages
        self._tessdata_dir = tessdata_dir

    def _base_args(self) -> list[str]:
        """실행 파일과 언어 모델 폴더 인자.

        파라미터: 없음
        """
        args = [str(self._command)]
        if self._tessdata_dir is not None:
            args += ["--tessdata-dir", str(self._tessdata_dir)]
        return args

    def has_languages(self) -> bool:
        """필요한 언어 모델(예: kor)이 모두 설치돼 있는지 본다. 실행에 실패해도 False.

        파라미터: 없음
        """
        try:
            done = subprocess.run(
                [*self._base_args(), "--list-langs"], capture_output=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        installed = set(done.stdout.decode("utf-8", errors="replace").split())
        return done.returncode == 0 and set(self._languages.split("+")) <= installed

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        """이미지를 PNG로 넘겨 TSV로 받는다. 실행 실패는 AppError(OCR_FAILED).

        페이지 분할 모드 4(크기가 다른 글자 단이 섞인 한 단 문서)를 쓴다.

        파라미터:
            image: 읽을 이미지
        """
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        args = [*self._base_args(), "stdin", "stdout", "-l", self._languages, "--psm", "4"]
        try:
            done = subprocess.run(
                # TSV는 설정 파일(configs/tsv) 대신 변수로 켠다(tessdata 폴더를 바꿔도 동작)
                [*args, "-c", "tessedit_create_tsv=1"],
                input=buffer.getvalue(),
                capture_output=True,
                timeout=OCR_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AppError(
                "OCR_FAILED", f"OCR 실행에 실패했습니다: {type(exc).__name__}", 500
            ) from exc
        if done.returncode != 0:
            message = done.stderr.decode("utf-8", errors="replace").strip()[:200]
            raise AppError("OCR_FAILED", f"OCR 실행에 실패했습니다: {message}", 500)
        return parse_tsv(done.stdout.decode("utf-8", errors="replace"))


class CachedOcrEngine:
    """같은 이미지의 OCR 결과를 파일에 저장해 두고 다시 읽지 않는다.

    업로드 진단과 민원 분석이 같은 스캔본을 두 번 OCR하는 것을 막는다. 이미지(PNG) 해시와
    엔진 이름으로 찾으므로, 엔진·언어를 바꾸면 새로 읽는다.
    """

    def __init__(self, inner: OcrEngine, cache_dir: Path) -> None:
        """캐시를 붙인 엔진을 만든다.

        파라미터:
            inner: 실제 OCR 엔진
            cache_dir: 결과(JSON)를 둘 폴더
        """
        self.name = inner.name
        self._inner = inner
        self._dir = cache_dir

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        """캐시에 있으면 그 결과를, 없으면 OCR한 뒤 저장한 결과를 돌려준다.

        파라미터:
            image: 읽을 이미지
        """
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        digest = hashlib.sha256(self.name.encode() + buffer.getvalue()).hexdigest()
        path = self._dir / digest[:2] / f"{digest}.json"
        if path.is_file():
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
                return [OcrLine(str(r["text"]), float(r["confidence"])) for r in rows]
            except (ValueError, KeyError, TypeError):
                pass  # 깨진 캐시는 다시 읽는다
        lines = self._inner.recognize(image)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [{"text": x.text, "confidence": x.confidence} for x in lines], ensure_ascii=False
            ),
            encoding="utf-8",
        )
        return lines
