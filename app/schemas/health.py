from pydantic import BaseModel


class HealthStatus(BaseModel):
    """상태 확인 응답."""

    status: str
    database: str
