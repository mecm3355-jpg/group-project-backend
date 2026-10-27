import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from app.core.errors import AppError
from app.processors.document import load_image
from app.services.preview import make_thumbnail


def decode(uri: str) -> Image.Image:
    """data URI 썸네일을 이미지로 연다.

    파라미터:
        uri: make_thumbnail이 돌려준 data URI
    """
    prefix = "data:image/jpeg;base64,"
    assert uri.startswith(prefix)
    return Image.open(io.BytesIO(base64.b64decode(uri[len(prefix) :])))


def test_thumbnail_shrinks_long_side_and_keeps_original(tmp_path: Path) -> None:
    path = tmp_path / "photo"
    Image.new("RGB", (1600, 800), "red").save(path, format="PNG")

    with load_image(path) as image:
        thumbnail = decode(make_thumbnail(image))
        assert image.size == (1600, 800)  # 원본은 그대로

    assert (thumbnail.format, thumbnail.size) == ("JPEG", (320, 160))


def test_thumbnail_fills_transparency_with_white(tmp_path: Path) -> None:
    path = tmp_path / "logo"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(path, format="PNG")

    with load_image(path) as image:
        thumbnail = decode(make_thumbnail(image))

    assert thumbnail.size == (10, 10)  # 작은 이미지는 키우지 않는다
    red, green, blue = thumbnail.convert("RGB").getpixel((5, 5))  # type: ignore[misc]
    assert min(red, green, blue) > 240


def test_broken_image_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "broken"
    path.write_bytes(b"\x89PNG\r\n\x1a\nbroken")

    with pytest.raises(AppError) as info:
        load_image(path)
    assert info.value.code == "INVALID_FILE"


def test_decompression_bomb_is_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "huge"
    Image.new("L", (100, 100)).save(path, format="PNG")
    # 한도의 2배를 넘으면 Pillow가 DecompressionBombError를 낸다(실제 한도는 약 9천만 화소)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)

    with pytest.raises(AppError) as info:
        load_image(path)
    assert (info.value.code, info.value.status_code) == ("IMAGE_TOO_LARGE", 413)


def test_thumbnail_applies_exif_rotation(tmp_path: Path) -> None:
    path = tmp_path / "phone"
    exif = Image.Exif()
    exif[0x0112] = 6  # 시계 방향 90도로 돌려 봐야 하는 사진
    Image.new("RGB", (800, 400), "blue").save(path, format="JPEG", exif=exif)

    with load_image(path) as image:
        thumbnail = decode(make_thumbnail(image))

    assert thumbnail.size == (160, 320)
