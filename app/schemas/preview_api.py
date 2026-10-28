from typing import Literal

from app.schemas.common import ApiModel

# 표 셀에 올 수 있는 JSON 기본값. 날짜는 미리보기에서 ISO 문자열로 바꿔 넣는다
PreviewCell = str | int | float | bool | None


class FilePreview(ApiModel):
    """GET /api/v1/files/{file_id}/preview 응답 — 원본 앞부분.

    kind가 table이면 columns·rows, text면 lines를 채운다.
    이미지는 thumbnail(긴 변 320px 이하 JPEG의 data URI)을 함께 채운다.
    """

    file_id: str
    kind: Literal["table", "text"]
    # 원본 형식(csv·xlsx·txt 등). encoding은 텍스트 계열만, sheet는 XLSX에서 읽은 시트 이름
    format: str
    encoding: str | None = None
    sheet: str | None = None
    # 미리보기 최대 행·줄 수(20). 실제 rows·lines 길이는 이보다 짧을 수 있다
    limit: int
    columns: list[str] | None = None
    rows: list[list[PreviewCell]] | None = None
    lines: list[str] | None = None
    thumbnail: str | None = None
