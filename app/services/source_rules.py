"""소스별 정형 문구 규칙(PRD 3장 김천, 9장 정형 문구 규칙).

김천 안전신문고 제목 "(SPP-번호)횡단보도 불법 주정차 신고입니다.* ..."에서 위반 장소 유형을,
처리결과(민원요지)에서는 오타 변형(과태교·과태룔·과태료미부과 등)을 표준값으로 바꾼다.
"""

import re

# 위반 장소 표준값. 앞에 있을수록 우선한다(예: "어린이 보호구역 횡단보도" → 어린이보호구역)
VIOLATION_PLACES = (
    "어린이보호구역",
    "소방차전용구역",
    "교차로모퉁이",
    "버스정류소",
    "횡단보도",
    "소화전",
    "안전지대",
    "인도",
)
PLACE_LABELS = {"교차로모퉁이": "교차로 모퉁이", "소방차전용구역": "소방차 전용구역"}
# "(SPP-...)" 뒤에서 "불법 주정차" 앞까지
TITLE_PLACE = re.compile(r"\)\s*(.*?)\s*불법\s*주\s*정\s*차")
PLACE_OTHER = "기타"
PLACE_UNKNOWN = "미추출"

RESULT_IMPOSED = "과태료 부과"
RESULT_NOT_IMPOSED = "과태료 미부과"
RESULT_WARNING = "경고장"
RESULT_EMPTY = "미기재"
RESULT_OTHER = "기타 답변"
# "과태료 부과"의 오타·축약으로 볼 최대 길이(공백 제외). 이보다 길면 개별 답변 문구다
RESULT_SHORT_MAX = 6


def extract_violation_place(title: str) -> str:
    """김천 제목에서 위반 장소 유형을 뽑는다.

    정형 문구가 없으면 미추출, 문구는 있지만 표준값이 없으면 기타("기타 불법 주정차" 포함).

    파라미터:
        title: 민원제목
    """
    match = TITLE_PLACE.search(title)
    if not match:
        return PLACE_UNKNOWN
    compact = re.sub(r"\s+", "", match.group(1))
    for place in VIOLATION_PLACES:
        if place in compact:
            return PLACE_LABELS.get(place, place)
    return PLACE_OTHER


def normalize_result(text: str) -> str:
    """김천 처리결과를 표준값으로 바꾼다.

    공백을 지운 뒤 "미부과"가 있으면 과태료 미부과, 짧고 "부과"로 끝나면(과태교 부과, 과태 부과,
    부과 등) 과태료 부과, "경고"가 있으면 경고장. 빈 값은 미기재, 나머지 개별 답변은 기타 답변.

    파라미터:
        text: 민원요지 값
    """
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return RESULT_EMPTY
    if len(compact) <= RESULT_SHORT_MAX + 1 and compact.endswith("미부과"):
        return RESULT_NOT_IMPOSED
    if len(compact) <= RESULT_SHORT_MAX and compact.endswith("부과"):
        return RESULT_IMPOSED
    if compact.startswith("경고"):
        return RESULT_WARNING
    return RESULT_OTHER
