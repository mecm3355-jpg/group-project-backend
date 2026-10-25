from datetime import UTC, datetime

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


def utc_now() -> datetime:
    """현재 시각을 UTC(타임존 포함)로 돌려준다. MongoDB 정밀도에 맞춰 밀리초까지만 남긴다.

    파라미터: 없음
    """
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


class MongoModel(BaseModel):
    """MongoDB 문서 모델의 공통 기반.

    파이썬 속성은 snake_case, DB 필드는 camelCase 별칭을 쓴다(예: size_bytes ↔ sizeBytes).
    DB에 저장할 때는 model_dump(by_alias=True)를 쓴다.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    def to_document(self) -> dict[str, object]:
        """DB에 저장할 dict로 바꾼다. 필드명은 스키마 기준(camelCase·_id)을 따른다.

        파라미터: 없음
        """
        return self.model_dump(by_alias=True)


class MongoDocument(MongoModel):
    """_id를 가진 최상위 문서. _id는 저장 전에 미리 만든다."""

    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
