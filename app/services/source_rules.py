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
# 비교는 공백을 지운 문자열로 하고, 보여줄 때만 띄어 쓴 이름으로 바꾼다
PLACE_LABELS = {"교차로모퉁이": "교차로 모퉁이", "소방차전용구역": "소방차 전용구역"}
# "(SPP-...)" 뒤에서 "불법 주정차" 앞까지
# "주 정 차"처럼 띄어 쓴 변형도 받고, 게으른 매칭(.*?)으로 첫 "불법 주정차" 앞에서 멈춘다
TITLE_PLACE = re.compile(r"\)\s*(.*?)\s*불법\s*주\s*정\s*차")
# 문구는 있는데 목록에 없는 장소(기타)와 문구 자체가 없는 제목(미추출)을 나눠, 정형 문구가 아닌
# 민원이 얼마나 섞였는지 따로 셀 수 있게 한다
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
    # "어린이 보호구역"·"어린이보호구역"처럼 띄어쓰기만 다른 표기를 같게 보려고 공백을 지운다
    compact = re.sub(r"\s+", "", match.group(1))
    # 포함 여부로 본다(앞뒤에 수식어가 붙어도 잡히게). 여러 개가 들면 목록 앞쪽이 이긴다
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
    # 오타 변형은 띄어쓰기가 제각각이라(과태료 부과, 과태료부과) 공백을 지우고 본다
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return RESULT_EMPTY
    # "미부과"는 "부과"로도 끝나므로 먼저 본다. "미" 한 글자만큼 길이 한도를 1 늘린다
    if len(compact) <= RESULT_SHORT_MAX + 1 and compact.endswith("미부과"):
        return RESULT_NOT_IMPOSED
    # 앞부분 오타(과태교·과태룔)는 다양해서 끝말 "부과"와 짧은 길이로만 판정한다.
    # 긴 문장이 "부과"로 끝나면 개별 답변이므로 길이 한도로 거른다
    if len(compact) <= RESULT_SHORT_MAX and compact.endswith("부과"):
        return RESULT_IMPOSED
    # 경고는 "경고장 발부", "경고 조치"처럼 뒤가 달라 시작 글자로 본다
    if compact.startswith("경고"):
        return RESULT_WARNING
    return RESULT_OTHER
