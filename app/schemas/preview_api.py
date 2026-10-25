from typing import Literal

from app.schemas.common import ApiModel

PreviewCell = str | int | float | bool | None


class FilePreview(ApiModel):
    """GET /api/v1/files/{file_id}/preview 응답 — 원본 앞부분.

    kind가 table이면 columns·rows, text면 lines를 채운다.
    """

    file_id: str
    kind: Literal["table", "text"]
    format: str
    encoding: str | None = None
    sheet: str | None = None
    limit: int
    columns: list[str] | None = None
    rows: list[list[PreviewCell]] | None = None
    lines: list[str] | None = None
