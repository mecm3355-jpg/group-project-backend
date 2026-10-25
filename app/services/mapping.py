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
KOREA_LATITUDE = (33.0, 39.5)
KOREA_LONGITUDE = (124.0, 132.0)
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
    used: set[str] = set()
    for field, (keywords, excludes) in RULES.items():
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
    lat, lon = chosen.get("latitude"), chosen.get("longitude")
    if lat and lon:
        lat_span, lon_span = _number_range(statistics, lat), _number_range(statistics, lon)
        if _within(lat_span, KOREA_LONGITUDE) and _within(lon_span, KOREA_LATITUDE):
            chosen["latitude"], chosen["longitude"] = lon, lat
            notes.append(f"'{lat}'·'{lon}' 열의 값 범위가 이름과 반대여서 바꿔 매핑했습니다.")

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
    payload = {
        "fileName": mask_text(file_name).text,
        "columns": columns,
        "ranges": statistics,
    }
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
    for field in FIELD_LABELS:
        column = getattr(proposal.mapping, field)
        if not isinstance(column, str) or not column:
            continue
        if column not in columns:
            notes.append(f"AI가 제안한 '{column}' 열이 원본에 없어 {field} 매핑에서 뺐습니다.")
        elif column in used:
            notes.append(f"'{column}' 열이 여러 필드에 제안되어 {field} 매핑에서 뺐습니다.")
        else:
            chosen[field] = column
            used.add(column)
    constants = FieldConstants(**proposal.constants.model_dump())
    return FieldMapping(**chosen), constants, notes
