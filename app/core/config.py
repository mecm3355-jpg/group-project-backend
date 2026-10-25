from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경 변수(.env)에서 읽는 애플리케이션 설정. 비밀 값은 코드에 두지 않는다."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "DataLens"
    mongodb_uri: str
    mongodb_db_name: str = "datalens"


@lru_cache
def get_settings() -> Settings:
    """설정 객체를 한 번만 만들어 재사용한다.

    파라미터: 없음. 값은 환경 변수와 .env 파일에서 읽는다.
    """
    return Settings()
