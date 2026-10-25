import uuid
from pathlib import Path

from app.core.errors import AppError


class LocalStorage:
    """원본 파일을 서버 로컬 디스크에 두는 저장소(files.storage.provider = "local").

    DB에는 파일 내용 대신 key(저장소 안의 상대 경로)만 기록한다.
    객체 저장소(S3 등)로 바꿀 때는 같은 메서드를 가진 클래스를 새로 만든다(PRD 12장).
    """

    provider = "local"

    def __init__(self, root: Path) -> None:
        """저장소를 만든다.

        파라미터:
            root: 저장소 최상위 폴더
        """
        self._root = root.resolve()

    def path(self, key: str) -> Path:
        """key를 실제 파일 경로로 바꾼다. 저장소 밖을 가리키는 key는 거부한다.

        파라미터:
            key: 저장소 안의 상대 경로(예: uploads/2026/10/<fileId>)
        """
        target = (self._root / key).resolve()
        if not target.is_relative_to(self._root):
            raise AppError("INVALID_STORAGE_KEY", "잘못된 저장 위치입니다.", 400)
        return target

    def new_temp_path(self) -> Path:
        """업로드를 받는 동안 쓸 임시 파일 경로를 만든다. 파일은 만들지 않는다.

        파라미터: 없음
        """
        temp_dir = self._root / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        return temp_dir / uuid.uuid4().hex

    def put(self, source: Path, key: str) -> None:
        """임시 파일을 key 위치로 옮긴다. 같은 key가 있으면 덮어쓴다.

        파라미터:
            source: 옮길 임시 파일 경로
            key: 저장할 위치
        """
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)

    def delete(self, key: str) -> bool:
        """key 위치의 파일을 지운다. 지웠으면 True, 없었으면 False.

        파라미터:
            key: 지울 파일 위치
        """
        target = self.path(key)
        if not target.exists():
            return False
        target.unlink()
        return True
