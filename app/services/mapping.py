"""원본 열 → 공통 필드 매핑 제안.

AI(fast 배포)에는 데이터 값을 보내지 않고 열 이름·추정 타입·결측·고유값 수·숫자 범위와
마스킹한 파일명만 보낸다. AI를 쓸 수 없으면 열 이름 규칙으로 제안한다.
제안은 담당자가 확인·수정한 뒤 POST /files/merge에 넣는다.
"""

import json
import re

from pydantic import JsonValue

from app.schemas.dataset import (
    FIELD_LABELS,
    AiMappingProposal,
    CanonicalField,
    FieldConstants,
    FieldMapping,
)
from app.services.llm import LlmClient
from app.services.masking import mask_text

# 프롬프트를 고치면 올린다(LLM 응답 캐시 키에 들어가 옛 응답을 다시 쓰지 않게 된다)
PROMPT_VERSION = "column-mapping@1"

SYSTEM_PROMPT = """당신은 여러 기관의 민원·신고 데이터를 하나로 합치는 데이터 엔지니어다.
입력은 한 파일의 열 목록(열 이름, 추정 타입, 결측 수, 고유값 수, 숫자·날짜 범위)과 파일명이다.
원본 값은 포함되어 있지 않다.

각 공통 필드에 해당하는 열 이름을 고르라. 반드시 입력에 있는 열 이름을 그대로 쓰고,
맞는 열이 없으면 null로 둔다. 한 열을 두 필드에 쓰지 않는다.
- sourceId: 원본 민원 번호·ID
- title: 제목
- content: 민원 본문·내용(자유 서술). 제목만 있으면 null
- receivedAt: 접수(등록·신고) 일시. 처리완료일·데이터 기준일은 아니다
- region: 행정동·시군구·지역 이름
- address: 주소·발생 장소
- latitude, longitude: 좌표. 한국의 위도는 약 33~39, 경도는 약 124~132다.
  열 이름과 값 범위가 반대면 값 범위를 따라 바꿔서 매핑하고 notes에 적는다.
- agency: 처리 기관·부서
- sourceCategory: 기관이 붙여 온 기존 분류·유형(대분류 우선)
- result: 처리 결과·상태

constants: 열이 없지만 파일 전체에 같은 값이 분명한 경우(예: 파일명의 시군구 → region)만 채우고,
아니면 null. notes: 판단 근거나 담당자가 확인할 점을 짧게.
"""

# 열 이름 규칙(앞에 있는 단어일수록 우선). exclude에 걸리는 열은 제외
# 필드 순서도 의미가 있다: 앞 필드가 먼저 열을 가져가고, 가져간 열은 뒤 필드가 못 쓴다.
# 그래서 "번호"·"ID"처럼 여러 열에 걸리기 쉬운 source_id를 맨 뒤에 둔다
RULES: dict[CanonicalField, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "content": (("상담내용", "민원내용", "신고내용", "본문", "내용", "요지"), ()),
    "title": (("제목",), ()),
    "received_at": (
        ("접수일", "등록일", "신고일", "작성일", "일시"),
        ("처리", "완료", "기준", "원래"),
    ),
    "region": (("행정동", "읍면동", "시군구", "지역"), ()),
    "address": (("주소", "소재지", "발생장소", "장소", "위치"), ()),
    "latitude": (("위도", "lat"), ()),
    "longitude": (("경도", "lon", "lng"), ()),
    "agency": (("처리기관", "처리부서", "담당부서", "기관", "부서"), ()),
    "source_category": (("대분류", "민원유형", "분류", "유형"), ("세부", "정답)_세부")),
    "result": (("처리결과", "처리상태", "결과"), ()),
    "source_id": (("민원ID", "신고번호", "민원번호", "번호", "ID"), ()),
}
# 한국 좌표 범위(제주 남단~휴전선 북쪽 여유, 서해 섬~독도). 두 범위가 겹치지 않아
# 값 범위만으로 위도·경도 열이 뒤바뀌었는지 판단할 수 있다
KOREA_LATITUDE = (33.0, 39.5)
KOREA_LONGITUDE = (124.0, 132.0)
# 파일명의 시군구 이름(예: "포항시_민원.csv" → 포항시). 지역 열이 없을 때 고정값 후보로 쓴다
SIGUNGU = re.compile(r"([가-힣]{2,4}(?:시|군|구))")


def _number_range(statistics: dict[str, JsonValue], column: str) -> tuple[float, float] | None:
    """분석 결과 통계에서 숫자 열의 (최솟값, 최댓값)을 꺼낸다.

    파라미터:
        statistics: processing_results.analysis.statistics
        column: 열 이름
    """
    stats = statistics.get(column)
    if not isinstance(stats, dict):
        return None
    low, high = stats.get("min"), stats.get("max")
    # 날짜 열의 min·max는 문자열(ISO)이라 여기서 걸러진다. 좌표 판단에는 숫자 열만 필요하다
    if isinstance(low, int | float) and isinstance(high, int | float):
        return float(low), float(high)
    return None


def _within(span: tuple[float, float] | None, bounds: tuple[float, float]) -> bool:
    """값 범위가 기준 범위 안에 있는지 본다.

    파라미터:
        span: (최솟값, 최댓값). 없으면 False
        bounds: 기준 (하한, 상한)
    """
    return span is not None and bounds[0] <= span[0] and span[1] <= bounds[1]


def suggest_by_rules(
    columns: list[str], statistics: dict[str, JsonValue], file_name: str
) -> tuple[FieldMapping, FieldConstants, list[str]]:
    """열 이름 규칙으로 매핑을 제안한다. 좌표는 값 범위로 위도·경도 뒤바뀜을 바로잡는다.

    파라미터:
        columns: 원본 열 이름
        statistics: 분석 결과의 숫자·날짜 통계(열 이름 → {min, max})
        file_name: 원래 파일명(시군구 고정값 추정에만 쓴다)
    """
    chosen: dict[CanonicalField, str] = {}
    # 한 열을 두 필드에 매핑하지 않는다(AI 지시문의 "한 열을 두 필드에 쓰지 않는다"와 같은 원칙)
    used: set[str] = set()
    for field, (keywords, excludes) in RULES.items():
        # 단어 순서대로 찾고, 단어마다 원본 열 순서에서 처음 맞는 열을 고른다.
        # 영문 약어(lat·ID)를 위해 대소문자는 무시하고, 부분 일치("민원접수일" ⊃ "접수일")로 본다
        for keyword in keywords:
            match = next(
                (
                    c
                    for c in columns
                    if c not in used
                    and keyword.lower() in c.lower()
                    and not any(x in c for x in excludes)
                ),
                None,
            )
            if match:
                chosen[field] = match
                used.add(match)
                break

    notes: list[str] = []
    # 원본 중에 위도·경도 열 이름이 반대로 붙은 자료가 있어(PRD 3장 포항) 값 범위로 바로잡는다.
    # 두 열 모두 범위가 분명히 반대일 때만 바꾸고, 담당자가 확인하도록 메모를 남긴다
    lat, lon = chosen.get("latitude"), chosen.get("longitude")
    if lat and lon:
        lat_span, lon_span = _number_range(statistics, lat), _number_range(statistics, lon)
        if _within(lat_span, KOREA_LONGITUDE) and _within(lon_span, KOREA_LATITUDE):
            chosen["latitude"], chosen["longitude"] = lon, lat
            notes.append(f"'{lat}'·'{lon}' 열의 값 범위가 이름과 반대여서 바꿔 매핑했습니다.")

    # 기관별 파일은 지역 열 없이 파일명에만 시군구가 있는 경우가 많다. 통합 뒤 지역별 분포를
    # 보려면 값이 필요해 파일명에서 고정값을 제안한다(확정은 담당자가 한다)
    constants = FieldConstants()
    if "region" not in chosen and (found := SIGUNGU.search(file_name)):
        constants.region = found.group(1)
        notes.append(f"지역 열이 없어 파일명의 '{found.group(1)}'을 고정값으로 제안합니다.")
    return FieldMapping(**chosen), constants, notes


def build_mapping_input(
    columns: list[JsonValue], statistics: dict[str, JsonValue], file_name: str
) -> str:
    """AI에 보낼 입력(열 정보와 마스킹한 파일명)을 만든다. 데이터 값은 넣지 않는다.

    파라미터:
        columns: 분석 결과의 열 정보(name, inferredType, missingCount, uniqueCount)
        statistics: 분석 결과의 숫자·날짜 통계
        file_name: 원래 파일명
    """
    # 파일명은 "홍길동_민원.csv"처럼 개인정보가 들어갈 수 있어 따로 먼저 마스킹한다.
    # 범위(ranges)는 좌표 열 판단에 필요해 숫자 통계만 넣는다(호출한 쪽이 topValues를 뺀다)
    payload = {
        "fileName": mask_text(file_name).text,
        "columns": columns,
        "ranges": statistics,
    }
    # 열 이름에도 개인정보가 섞일 수 있어(예: 담당자 이름을 머리글로 쓴 표) 전체를 한 번 더
    # 마스킹한다. LLM 어댑터가 전송 직전에 다시 검사하므로 여기서 놓쳐도 밖으로 나가지 않는다
    return mask_text(json.dumps(payload, ensure_ascii=False, indent=1)).text


async def suggest_by_ai(
    llm: LlmClient,
    columns: list[str],
    column_info: list[JsonValue],
    statistics: dict[str, JsonValue],
    file_name: str,
) -> tuple[FieldMapping, FieldConstants, list[str]]:
    """AI로 매핑을 제안받는다. 원본에 없는 열 이름·중복 사용은 버린다.

    파라미터:
        llm: fast 배포 LLM 호출기
        columns: 원본 열 이름(검증용)
        column_info: 분석 결과의 열 정보
        statistics: 분석 결과의 숫자·날짜 통계
        file_name: 원래 파일명
    """
    proposal = await llm.generate(
        prompt_version=PROMPT_VERSION,
        system=SYSTEM_PROMPT,
        masked_input=build_mapping_input(column_info, statistics, file_name),
        response_model=AiMappingProposal,
    )
    notes = list(proposal.notes)
    chosen: dict[str, str] = {}
    used: set[str] = set()
    # AI 응답은 스키마는 맞아도 내용이 틀릴 수 있다. 지시문을 어긴 매핑은 조용히 쓰지 않고
    # 빼면서 메모로 남겨 담당자가 직접 채우게 한다
    for field in FIELD_LABELS:
        column = getattr(proposal.mapping, field)
        if not isinstance(column, str) or not column:
            continue
        # 없는 열 이름(지어낸 이름, 마스킹으로 바뀐 이름)은 통합 작업에서 UNKNOWN_COLUMN이 된다
        if column not in columns:
            notes.append(f"AI가 제안한 '{column}' 열이 원본에 없어 {field} 매핑에서 뺐습니다.")
        # 중복이면 FIELD_LABELS 순서에서 먼저 나온 필드가 열을 갖는다
        elif column in used:
            notes.append(f"'{column}' 열이 여러 필드에 제안되어 {field} 매핑에서 뺐습니다.")
        else:
            chosen[field] = column
            used.add(column)
    # AI 응답 모델의 고정값을 API 응답 모델로 옮긴다(두 모델을 분리해 AI 스키마만 바꿀 수 있게)
    constants = FieldConstants(**proposal.constants.model_dump())
    return FieldMapping(**chosen), constants, notes
