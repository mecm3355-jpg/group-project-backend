from pydantic import BaseModel


class HealthStatus(BaseModel):
    """상태 확인 응답."""

    # 서버 상태와 DB 상태. DB가 끊기면 이 응답 대신 503 실패 응답이 나간다
    status: str
    database: str
