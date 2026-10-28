from datetime import UTC, datetime

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


def utc_now() -> datetime:
    """현재 시각을 UTC(타임존 포함)로 돌려준다. MongoDB 정밀도에 맞춰 밀리초까지만 남긴다.

    파라미터: 없음
    """
    now = datetime.now(UTC)
    # BSON Date는 밀리초 정밀도라, 저장 전후 값이 달라 비교가 어긋나지 않도록 미리 자른다
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


class MongoModel(BaseModel):
    """MongoDB 문서 모델의 공통 기반.

    파이썬 속성은 snake_case, DB 필드는 camelCase 별칭을 쓴다(예: size_bytes ↔ sizeBytes).
    DB에 저장할 때는 model_dump(by_alias=True)를 쓴다.
    """

    model_config = ConfigDict(
        # schema.md 필드명이 camelCase라 별칭으로 맞추고, 코드는 파이썬 관례(snake_case)를 지킨다
        alias_generator=to_camel,
        # DB에서 읽을 때는 별칭, 코드에서 만들 때는 속성 이름 둘 다 받도록 한다
        populate_by_name=True,
        # ObjectId는 pydantic 기본 타입이 아니라서 허용해야 필드로 쓸 수 있다
        arbitrary_types_allowed=True,
    )

    def to_document(self) -> dict[str, object]:
        """DB에 저장할 dict로 바꾼다. 필드명은 스키마 기준(camelCase·_id)을 따른다.

        파라미터: 없음
        """
        return self.model_dump(by_alias=True)


class MongoDocument(MongoModel):
    """_id를 가진 최상위 문서. _id는 저장 전에 미리 만든다."""

    # _id를 클라이언트에서 미리 만들어, 저장 전에도 다른 문서가 이 ID를 참조할 수 있게 한다.
    # 속성 이름 _id는 pydantic이 private로 보므로 id로 두고 별칭으로 연결한다
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
