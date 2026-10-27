import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from app.core.errors import AppError
from app.services.preview import make_thumbnail


def decode(uri: str) -> Image.Image:
    """data URI 썸네일을 이미지로 연다.

    파라미터:
        uri: make_thumbnail이 돌려준 data URI
    """
    prefix = "data:image/jpeg;base64,"
    assert uri.startswith(prefix)
    return Image.open(io.BytesIO(base64.b64decode(uri[len(prefix) :])))


def test_thumbnail_shrinks_long_side_and_keeps_ratio(tmp_path: Path) -> None:
    path = tmp_path / "photo"
    Image.new("RGB", (1600, 800), "red").save(path, format="PNG")

    thumbnail = decode(make_thumbnail(path))

    assert (thumbnail.format, thumbnail.size) == ("JPEG", (320, 160))


def test_thumbnail_fills_transparency_with_white(tmp_path: Path) -> None:
    path = tmp_path / "logo"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(path, format="PNG")

    thumbnail = decode(make_thumbnail(path))

    assert thumbnail.size == (10, 10)  # 작은 이미지는 키우지 않는다
    red, green, blue = thumbnail.convert("RGB").getpixel((5, 5))  # type: ignore[misc]
    assert min(red, green, blue) > 240


def test_thumbnail_of_broken_image_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "broken"
    path.write_bytes(b"\x89PNG\r\n\x1a\nbroken")

    with pytest.raises(AppError) as info:
        make_thumbnail(path)
    assert info.value.code == "INVALID_FILE"
