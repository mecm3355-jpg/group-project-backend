from pathlib import Path

import pytest

from app.core.errors import AppError
from app.services.storage import LocalStorage


def test_put_and_delete(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    temp = storage.new_temp_path()
    temp.write_bytes(b"data")

    storage.put(temp, "uploads/2026/10/abc")

    stored = storage.path("uploads/2026/10/abc")
    assert stored.read_bytes() == b"data"
    assert not temp.exists()
    assert storage.delete("uploads/2026/10/abc")
    assert not stored.exists()
    assert not storage.delete("uploads/2026/10/abc")


@pytest.mark.parametrize("key", ["../outside", "uploads/../../outside"])
def test_rejects_key_outside_root(tmp_path: Path, key: str) -> None:
    storage = LocalStorage(tmp_path / "root")

    with pytest.raises(AppError) as info:
        storage.path(key)
    assert info.value.code == "INVALID_STORAGE_KEY"
