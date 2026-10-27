import shutil
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
        # resolve로 ".."·심볼릭 링크를 먼저 풀어야 "../../etc" 같은 key가
        # 저장소 안처럼 보이지 않는다
        target = (self._root / key).resolve()
        if not target.is_relative_to(self._root):
            raise AppError("INVALID_STORAGE_KEY", "잘못된 저장 위치입니다.", 400)
        return target

    def new_temp_path(self) -> Path:
        """업로드를 받는 동안 쓸 임시 파일 경로를 만든다. 파일은 만들지 않는다.

        파라미터: 없음
        """
        # 임시 폴더를 저장소 안에 둔다. 같은 디스크라 put의 replace가 복사 없이 이름만 바꾸는
        # 원자적 이동이 된다(다른 드라이브면 replace가 실패한다)
        temp_dir = self._root / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        # 동시에 여러 업로드가 와도 겹치지 않도록 무작위 이름을 쓴다
        return temp_dir / uuid.uuid4().hex

    def put(self, source: Path, key: str) -> None:
        """임시 파일을 key 위치로 옮긴다. 같은 key가 있으면 덮어쓴다.

        파라미터:
            source: 옮길 임시 파일 경로
            key: 저장할 위치
        """
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # replace는 Windows에서도 기존 파일을 덮어쓴다(rename은 실패). 읽는 쪽은 옮기기 전
        # 또는 후의 온전한 파일만 보게 된다
        source.replace(target)

    def write_text(self, key: str, text: str) -> None:
        """작은 텍스트(작업 파라미터 등)를 key 위치에 UTF-8로 쓴다.

        파라미터:
            key: 저장할 위치
            text: 쓸 내용
        """
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def read_text(self, key: str) -> str:
        """key 위치의 텍스트를 읽는다. 없으면 AppError(STORAGE_NOT_FOUND).

        파라미터:
            key: 읽을 위치
        """
        target = self.path(key)
        # FileNotFoundError 대신 AppError로 바꿔 작업 오류 코드로 남게 한다
        if not target.is_file():
            raise AppError("STORAGE_NOT_FOUND", "저장된 파일을 찾을 수 없습니다.", 404)
        return target.read_text(encoding="utf-8")

    def delete_prefix(self, prefix: str) -> None:
        """prefix 폴더를 통째로 지운다(파일별 산출물·작업 파라미터 정리용). 없으면 그냥 둔다.

        파라미터:
            prefix: 지울 폴더 key(예: results/<fileId>)
        """
        target = self.path(prefix)
        # 빈 문자열이나 "." 같은 prefix가 들어와도 저장소 전체가 날아가지 않게 막는다
        if target == self._root:
            raise AppError("INVALID_STORAGE_KEY", "저장소 전체는 지울 수 없습니다.", 400)
        # 정리용이라 일부 파일이 잠겨 있어도(Windows) 본 작업·삭제 요청을 실패시키지 않는다
        shutil.rmtree(target, ignore_errors=True)

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
