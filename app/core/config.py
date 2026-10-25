from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경 변수(.env)에서 읽는 애플리케이션 설정. 비밀 값은 코드에 두지 않는다."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "DataLens"
    mongodb_uri: str
    mongodb_db_name: str = "datalens"

    # 원본 파일·산출물 로컬 저장소 루트(files.storage.provider = local)
    storage_root: Path = Path("storage")
    # 업로드 제한(PRD 10장). 압축 기반 형식(XLSX·DOCX)은 풀었을 때 크기·파일 수도 제한
    max_upload_bytes: int = 100 * 1024 * 1024
    max_zip_uncompressed_bytes: int = 1024 * 1024 * 1024
    max_zip_entries: int = 10_000


@lru_cache
def get_settings() -> Settings:
    """설정 객체를 한 번만 만들어 재사용한다.

    파라미터: 없음. 값은 환경 변수와 .env 파일에서 읽는다.
    """
    return Settings()
