"""이슈 묶기(PRD 4장 7단계): 같은 장소·같은 문제의 반복 민원을 하나의 이슈로 묶는다.

묶는 조건: 같은 카테고리 + 같은 장소(좌표 radius_meters 이내 또는 같은 장소 표현)
+ 본문 유사도 text_similarity 이상. 기존 열린 이슈와는 대표 문장(summary) 벡터로 비교한다.
새 묶음은 2건 이상일 때만 이슈로 만든다(한 건짜리는 이슈 없음).
"""

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from bson import ObjectId

from app.core.analysis_config import IssueConfig
from app.schemas.complaint import IssueDocument
from app.services.embedding import Vectors, normalize

# 긴급도 4단계(PRD 5.5)의 높낮이. 이슈의 최고 긴급도를 고를 때 비교 기준으로 쓴다
URGENCY_RANK = {"낮음": 0, "보통": 1, "높음": 2, "긴급": 3}
# 소속 민원 어디에도 긴급도 근거가 없을 때의 값(추측으로 채우지 않는다, PRD 5.5)
UNKNOWN_URGENCY = "미상"
# 지구 평균 반지름. 100m 수준 거리에서는 구면 근사 오차가 무시할 만하다
EARTH_RADIUS_METERS = 6_371_000


@dataclass
class IssueInput:
    """이슈 묶기에 넣을 배정된 민원 한 건."""

    complaint_id: ObjectId
    category_id: str
    vector: Vectors
    text: str
    place: str | None
    latitude: float | None
    longitude: float | None
    received_at: datetime
    urgency: str | None


@dataclass
class _Group:
    """묶는 중인 이슈(기존 이슈면 issue가 있다)."""

    category_id: str
    place: str
    vector: Vectors
    latitude: float | None
    longitude: float | None
    issue: IssueDocument | None = None
    members: list[IssueInput] = field(default_factory=list)


@dataclass
class IssueResult:
    """묶기 결과: 새로 만들 이슈, 고칠 기존 이슈, 민원 → 이슈 _id."""

    created: list[IssueDocument] = field(default_factory=list)
    updated: list[IssueDocument] = field(default_factory=list)
    assignments: dict[ObjectId, ObjectId] = field(default_factory=dict)


def distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 좌표 사이 거리(하버사인, 미터).

    파라미터:
        lat1: 첫 위도
        lon1: 첫 경도
        lat2: 둘째 위도
        lon2: 둘째 경도
    """
    # 하버사인 공식은 가까운 두 점에서도 반올림 오차가 작아 100m 반경 판정에 알맞다
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def _same_place(item: IssueInput, group: _Group, config: IssueConfig) -> bool:
    """좌표가 둘 다 있으면 거리로, 아니면 장소 표현이 같은지로 본다.

    파라미터:
        item: 민원
        group: 묶음
        config: 이슈 기준값
    """
    if (
        item.latitude is not None
        and item.longitude is not None
        and group.latitude is not None
        and group.longitude is not None
    ):
        distance = distance_meters(item.latitude, item.longitude, group.latitude, group.longitude)
        return distance <= config.radius_meters
    # 좌표가 한쪽이라도 없으면 장소 표현이 글자 그대로 같을 때만 같은 장소로 본다.
    # 장소가 없는 민원끼리(None == None)는 같은 장소로 치지 않는다
    return item.place is not None and item.place == group.place


def _max_urgency(values: list[str | None]) -> str:
    """가장 높은 긴급도(없으면 미상).

    파라미터:
        values: 긴급도 목록
    """
    known = [v for v in values if v in URGENCY_RANK]
    return max(known, key=lambda v: URGENCY_RANK[v]) if known else UNKNOWN_URGENCY


def group_issues(
    items: list[IssueInput],
    open_issues: list[IssueDocument],
    issue_vectors: Vectors,
    config: IssueConfig,
) -> IssueResult:
    """민원을 기존 열린 이슈 또는 새 묶음에 넣는다. 장소 정보(좌표·장소 표현)가 없는 민원은 뺀다.

    파라미터:
        items: 카테고리에 배정된 민원
        open_issues: 기존 열린 이슈
        issue_vectors: open_issues 대표 문장의 정규화된 임베딩(같은 순서)
        config: 이슈 기준값
    """
    # 1) 기존 열린 이슈를 먼저 묶음으로 올려, 이번 달 민원이 새 이슈보다 기존 이슈에 붙게 한다
    groups: list[_Group] = []
    for issue, vector in zip(open_issues, issue_vectors, strict=True):
        # GeoJSON Point 좌표는 [경도, 위도] 순서다
        point = issue.location.coordinates if issue.location else None
        groups.append(
            _Group(
                category_id=issue.category_id,
                place=issue.place,
                vector=vector,
                latitude=point[1] if point else None,
                longitude=point[0] if point else None,
                issue=issue,
            )
        )
    # 2) 접수 순으로 넣는다. 먼저 들어온 민원이 새 묶음의 기준(장소·벡터)이 되므로, 순서를
    #    고정해야 같은 입력에서 같은 이슈가 나온다
    for item in sorted(items, key=lambda i: i.received_at):
        # 장소 근거가 전혀 없으면 "같은 장소" 조건을 확인할 수 없어 이슈에 넣지 않는다
        if item.place is None and item.latitude is None:
            continue
        # 본문 유사도 기준(초기 0.8, PRD 4장 7단계)을 넘는 묶음 가운데 가장 비슷한 것을 고른다
        best: _Group | None = None
        best_similarity = config.text_similarity
        for group in groups:
            if group.category_id != item.category_id or not _same_place(item, group, config):
                continue
            # 둘 다 정규화된 벡터라 내적이 코사인 유사도다. 묶음 벡터는 첫 민원(또는 기존 이슈
            # 대표 문장)의 것으로 고정해, 묶음이 커지며 기준이 흘러가지 않게 한다
            similarity = float(item.vector @ group.vector)
            if similarity >= best_similarity:
                best, best_similarity = group, similarity
        # 맞는 묶음이 없으면 이 민원으로 새 묶음을 연다. 장소 표현이 없으면 좌표 문자열을
        # 장소 이름 대신 쓴다(소수 5자리 ≈ 1m)
        if best is None:
            best = _Group(
                category_id=item.category_id,
                place=item.place or f"{item.latitude:.5f},{item.longitude:.5f}",
                vector=item.vector,
                latitude=item.latitude,
                longitude=item.longitude,
            )
            groups.append(best)
        best.members.append(item)

    # 3) 묶음을 결과로 바꾼다. 새 민원이 붙지 않은 기존 이슈는 고칠 것이 없으므로 건너뛴다
    result = IssueResult()
    for group in groups:
        if not group.members:
            continue
        if group.issue is not None:
            # 기존 이슈: 건수·기간·최고 긴급도만 넓힌다. 대표 문장은 바꾸지 않는다
            issue = group.issue
            issue.complaint_count += len(group.members)
            issue.first_received_at = min(
                issue.first_received_at, *(m.received_at for m in group.members)
            )
            issue.last_received_at = max(
                issue.last_received_at, *(m.received_at for m in group.members)
            )
            issue.max_urgency = _max_urgency(
                [issue.max_urgency, *(m.urgency for m in group.members)]
            )
            result.updated.append(issue)
        elif len(group.members) >= 2:
            # 새 이슈: 한 건만으로는 "반복"이 아니므로 2건 이상일 때만 만든다. 대표 문장은
            # 묶음 평균에 가장 가까운 민원 본문으로, 다음 달 기존 이슈와 비교할 때 쓰인다
            matrix = np.stack([m.vector for m in group.members])
            centroid = normalize(matrix.mean(axis=0, keepdims=True))[0]
            summary = group.members[int(np.argmax(matrix @ centroid))].text
            issue = IssueDocument.model_validate(
                {
                    "categoryId": group.category_id,
                    "place": group.place,
                    "location": {"type": "Point", "coordinates": [group.longitude, group.latitude]}
                    if group.latitude is not None and group.longitude is not None
                    else None,
                    "complaintCount": len(group.members),
                    "firstReceivedAt": min(m.received_at for m in group.members),
                    "lastReceivedAt": max(m.received_at for m in group.members),
                    "summary": summary,
                    "maxUrgency": _max_urgency([m.urgency for m in group.members]),
                }
            )
            result.created.append(issue)
        else:
            continue  # 한 건짜리 새 묶음은 이슈 없음으로 둔다
        # 기존·새 이슈 모두 소속 민원에 이슈 _id를 이어 준다(complaints 갱신용)
        for member in group.members:
            result.assignments[member.complaint_id] = issue.id
    return result
