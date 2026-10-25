"""원본 데이터 미리보기(GET /api/v1/files/{file_id}/preview). 앞부분만 읽고 저장하지 않는다."""

from datetime import date, datetime, time
from itertools import islice
from pathlib import Path

from app.core.errors import AppError
from app.processors.document import DOCUMENT_EXTENSIONS, open_document
from app.processors.tabular import TABULAR_EXTENSIONS, Cell, open_table
from app.processors.text import TEXT_EXTENSIONS, open_text
from app.schemas.preview_api import FilePreview, PreviewCell

PREVIEW_LIMIT = 20


def to_preview_cell(value: Cell) -> PreviewCell:
    """칸 값을 JSON으로 보낼 수 있는 값으로 바꾼다(날짜·시각은 ISO 문자열).

    파라미터:
        value: 칸 값
    """
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return value


def build_preview(file_id: str, path: Path, extension: str) -> FilePreview:
    """원본 파일의 앞 20행(표) 또는 앞 20줄(텍스트·문서)을 읽는다. 스레드에서 실행한다.

    파라미터:
        file_id: 파일 ID(응답에 담는다)
        path: 원본 파일 경로
        extension: 파일 확장자
    """
    if extension in TABULAR_EXTENSIONS:
        with open_table(path, extension) as table:
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
    if extension in DOCUMENT_EXTENSIONS:
        # 문서는 빈 줄을 뺀 글자 줄을 보여준다. DOCX 표의 행은 칸을 탭으로 이은 한 줄이다
        with open_document(path, extension) as document:
            return FilePreview(
                file_id=file_id,
                kind="text",
                format=document.format,
                limit=PREVIEW_LIMIT,
                lines=list(islice(document.lines, PREVIEW_LIMIT)),
            )
    raise AppError(
        "PREVIEW_NOT_SUPPORTED", f"{extension} 파일의 미리보기는 아직 지원하지 않습니다.", 422
    )
