"""원본 데이터 미리보기(GET /api/v1/files/{file_id}/preview). 앞부분만 읽고 저장하지 않는다."""

import base64
import io
from datetime import date, datetime, time
from itertools import islice
from pathlib import Path

from PIL import Image, ImageOps

from app.core.errors import AppError
from app.processors.document import DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS, OcrOptions, open_document
from app.processors.tabular import TABULAR_EXTENSIONS, Cell, open_table
from app.processors.text import TEXT_EXTENSIONS, open_text
from app.schemas.preview_api import FilePreview, PreviewCell

# 미리보기 행·줄 수. 화면 한 장에 들어갈 만큼만 읽어 큰 파일·스캔본도 API 응답이 빠르다
PREVIEW_LIMIT = 20
# 이미지 미리보기 썸네일: 긴 변 최대 픽셀과 JPEG 품질
# (JSON 응답에 base64로 넣으므로 수십 KB 안쪽이 되도록 작게 잡는다)
THUMBNAIL_MAX_SIZE = 320
THUMBNAIL_QUALITY = 80


def to_preview_cell(value: Cell) -> PreviewCell:
    """칸 값을 JSON으로 보낼 수 있는 값으로 바꾼다(날짜·시각은 ISO 문자열).

    파라미터:
        value: 칸 값
    """
    # 날짜 객체는 JSON 직렬화가 안 되므로 문자열로 바꾸고, 나머지(숫자·불리언)는 타입을 살린다
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return value


def make_thumbnail(source: Image.Image) -> str:
    """이미지를 긴 변 320px 이하 JPEG로 줄여 data URI(data:image/jpeg;base64,...)로 돌려준다.

    원본 이미지는 바꾸지 않는다. 촬영 방향(EXIF)을 반영하고, 투명한 부분은 흰색으로 채운다.

    파라미터:
        source: 이미 연(load한) 이미지
    """
    # 휴대폰 사진은 픽셀은 가로로 두고 EXIF에 회전 정보만 남기므로, 먼저 돌려야 바로 보인다
    image: Image.Image = ImageOps.exif_transpose(source)  # 원본이 아닌 복사본을 돌려준다
    # thumbnail은 비율을 지키며 줄이기만 한다(작은 이미지는 키우지 않는다)
    image.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE))
    # JPEG는 투명도를 못 담는다. 그냥 RGB로 바꾸면 투명 부분이 검게 나오므로 흰 바탕에 합성한다
    # (P는 팔레트에 투명색이 있을 수 있어 RGBA를 거친다)
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        image = background
    # CMYK·흑백(L)·16비트 등도 JPEG 저장 전에 RGB로 맞춘다
    elif image.mode != "RGB":
        image = image.convert("RGB")
    # 썸네일은 저장소에 두지 않고 응답에 data URI로 바로 싣는다(별도 파일·정리 작업이 필요 없다)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=THUMBNAIL_QUALITY)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def build_preview(
    file_id: str, path: Path, extension: str, ocr: OcrOptions | None = None
) -> FilePreview:
    """원본 파일의 앞 20행(표) 또는 앞 20줄(텍스트·문서·이미지)을 읽는다. 스레드에서 실행한다.

    스캔 PDF·이미지는 앞 20줄이 나올 때까지만 OCR한다. 이미지는 썸네일도 만든다.

    파라미터:
        file_id: 파일 ID(응답에 담는다)
        path: 원본 파일 경로
        extension: 파일 확장자
        ocr: 스캔 PDF·이미지를 읽을 OCR 설정(없으면 OCR하지 않는다)
    """
    if extension in TABULAR_EXTENSIONS:
        with open_table(path, extension) as table:
            # 빈 줄을 건너뛴 뒤 앞 20행만 꺼낸다. islice라 나머지 행은 읽지 않는다
            rows = [
                [to_preview_cell(v) for v in row]
                for row in islice((r for r in table.rows if r), PREVIEW_LIMIT)
            ]
            return FilePreview(
                file_id=file_id,
                kind="table",
                format=table.format,
                encoding=table.encoding,
                sheet=table.sheet,
                limit=PREVIEW_LIMIT,
                columns=table.columns,
                rows=rows,
            )
    if extension in TEXT_EXTENSIONS:
        with open_text(path, extension) as text:
            return FilePreview(
                file_id=file_id,
                kind="text",
                format=text.format,
                encoding=text.encoding,
                limit=PREVIEW_LIMIT,
                lines=list(islice(text.lines, PREVIEW_LIMIT)),
            )
    if extension in DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS:
        # 문서는 빈 줄을 뺀 글자 줄을 보여준다. DOCX 표의 행은 칸을 탭으로 이은 한 줄이다
        # OCR이 없어도 이미지 썸네일은 보여 줄 수 있도록 allow_missing_ocr로 연다
        with open_document(path, extension, ocr, allow_missing_ocr=True) as document:
            return FilePreview(
                file_id=file_id,
                kind="text",
                format=document.format,
                limit=PREVIEW_LIMIT,
                # 줄 반복자가 지연이라 스캔 PDF는 20줄이 찰 때까지의 페이지만 OCR한다
                lines=list(islice(document.lines, PREVIEW_LIMIT)),
                # source_image는 with 블록이 끝나면 닫히므로 블록 안에서 썸네일을 만든다
                thumbnail=make_thumbnail(document.source_image)
                if document.source_image is not None
                else None,
            )
    raise AppError(
        "PREVIEW_NOT_SUPPORTED", f"{extension} 파일의 미리보기는 아직 지원하지 않습니다.", 422
    )
