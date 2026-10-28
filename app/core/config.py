from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경 변수(.env)에서 읽는 애플리케이션 설정. 비밀 값은 코드에 두지 않는다."""

    # extra=ignore: .env에 다른 도구용 변수(MONGODB_TEST_URI 등)가 있어도 시작을 막지 않는다
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "DataLens"
    # 연결 문자열은 비밀 값이라 기본값 없이 필수로 둔다(없으면 시작할 때 바로 실패)
    mongodb_uri: str
    mongodb_db_name: str = "datalens"

    # 원본 파일·산출물 로컬 저장소 루트(files.storage.provider = local)
    storage_root: Path = Path("storage")
    # 업로드 제한(PRD 10장). 압축 기반 형식(XLSX·DOCX)은 풀었을 때 크기·파일 수도 제한
    # 100MB. 압축 해제 1GB·항목 1만 개 제한은 압축 폭탄(zip bomb) 방어용
    max_upload_bytes: int = 100 * 1024 * 1024
    max_zip_uncompressed_bytes: int = 1024 * 1024 * 1024
    max_zip_entries: int = 10_000

    # 워커(python -m app.worker): 대기 작업이 없을 때 다시 확인하는 간격,
    # 이 시간보다 오래 processing에 머문 작업은 워커가 멈춘 것으로 보고 실패 처리
    worker_poll_seconds: float = 1.0
    job_timeout_seconds: int = 60 * 60

    # Azure OpenAI(PRD 6.1). 값은 .env에만 둔다.
    # 엔드포인트가 .../openai/v1 이면 v1 API(API 버전 불필요), 아니면 API 버전이 필요하다.
    # 배포 이름(모델명 아님): FAST는 대량·정형 작업(속성 추출 등), DEEP은 깊은 추론(리포트 등)
    azure_openai_endpoint: str | None = None
    # SecretStr: 로그나 repr에 키가 그대로 찍히지 않게 한다. 비우면 LLM 없이 규칙 방식으로 동작
    azure_openai_api_key: SecretStr | None = None
    azure_openai_api_version: str | None = None
    azure_openai_deployment_fast: str | None = None
    azure_openai_deployment_deep: str | None = None
    llm_timeout_seconds: float = 120.0

    # OCR(PRD 7.6): 로컬 Tesseract. 경로를 비우면 PATH와 Windows 기본 설치 위치에서 찾는다.
    # 한국어 모델(kor.traineddata)이 기본 폴더에 없으면 TESSERACT_TESSDATA_DIR로 지정한다
    tesseract_cmd: str | None = None
    tesseract_tessdata_dir: Path | None = None
    ocr_languages: str = "kor"
    # 300dpi: Tesseract 권장 해상도. 더 낮으면 한글 획이 뭉개져 정확도가 떨어진다
    ocr_dpi: int = 300
    # 줄 신뢰도가 이 값 미만이면 사람 검수 대상
    ocr_low_confidence: float = 0.6

    # 민원 분석 임베딩(PRD 6장, 로컬). 캐시 폴더를 비우면 저장소의 embedding-cache를 쓴다
    embedding_model: str = "BAAI/bge-m3"
    embedding_cache_dir: Path | None = None


# 요청마다 .env를 다시 읽지 않도록 캐시한다. 테스트는 create_app에 설정을 직접 넘긴다
@lru_cache
def get_settings() -> Settings:
    """설정 객체를 한 번만 만들어 재사용한다.

    파라미터: 없음. 값은 환경 변수와 .env 파일에서 읽는다.
    """
    return Settings()
