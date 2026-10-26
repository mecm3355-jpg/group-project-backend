"""민원 속성 6종 추출(PRD 5.5, 6장). 규칙 방식(A)과 LLM 방식(B)을 같은 결과 형식으로 돌려준다.

본문에 근거가 없는 속성은 None으로 둔다. 추측으로 채우지 않는다.
M2에서 두 방식을 비교해 어느 쪽을 기본으로 쓸지 정한다(PRD 12장).
"""

import re
from dataclasses import dataclass, field
from typing import TypeVar

from app.core.errors import AppError
from app.schemas.complaint_analysis import (
    Affected,
    Attributes,
    ComplaintRecord,
    LlmAttributeBatch,
    Place,
    RequestType,
    Urgency,
)
from app.services.llm import LlmClient

T = TypeVar("T")

RULE_ANALYZER = "rule-dict@1"
LLM_PROMPT_VERSION = "complaint-attributes@1"

# 시설 사전: 본문 표현 → 표준 이름. 본문에서 가장 먼저 나온 시설을 대상 시설로 본다
FACILITY_SYNONYMS: dict[str, str] = {
    "가로등": "가로등", "보안등": "가로등", "공원등": "가로등", "조명": "가로등",
    "신호등": "신호등", "점자블록": "점자블록", "점자 블록": "점자블록", "보도블록": "보도블록",
    "맨홀": "맨홀", "반사경": "반사경", "벤치": "벤치", "화장실": "화장실", "변기": "화장실",
    "세면대": "화장실", "버스정류장": "버스정류장", "정류장": "버스정류장", "정류소": "버스정류장",
    "승강장": "버스정류장", "쓰레기통": "쓰레기통", "배수구": "배수구", "빗물받이": "배수구",
    "하수구": "배수구", "배수로": "배수로", "하수관": "하수관", "cctv": "CCTV",
    "횡단보도": "횡단보도", "과속방지턱": "과속방지턱", "놀이기구": "놀이기구",
    "운동기구": "운동기구", "거치대": "자전거 거치대", "차단봉": "차단봉", "난간": "난간",
    "펜스": "펜스", "계단": "계단", "엘리베이터": "엘리베이터", "경사로": "경사로",
    "현수막": "현수막", "전단지": "전단지", "입간판": "입간판", "광고판": "광고판", "벽보": "벽보",
    "음수대": "음수대", "무인민원기": "무인민원기", "택배함": "무인택배함", "우편함": "우편함",
    "에어컨": "에어컨", "실외기": "실외기", "계량기": "수도계량기", "소화전": "소화전",
    "표지판": "표지판", "안내판": "안내판", "전선": "전선", "충전시설": "충전시설",
    "징검다리": "징검다리", "교량": "교량", "배수문": "배수문", "도로": "도로", "인도": "인도",
    "보도": "인도", "산책로": "산책로", "자전거길": "자전거길",
}  # fmt: skip
FACILITY_PATTERN = re.compile(
    "|".join(re.escape(word) for word in sorted(FACILITY_SYNONYMS, key=len, reverse=True)),
    re.IGNORECASE,
)

# 행정동·읍·면: 띄어쓰기로 떨어진 2~4글자 + 동·읍·면(예: 수청동, 송악읍, 대호지면)
DONG = re.compile(r"(?<![가-힣])([가-힣]{1,3}(?:동|읍|면))(?![가-힣])")
# 동 이름으로 보이지만 일반 낱말인 것
DONG_STOPWORDS = {
    "이동", "활동", "행동", "자동", "운동", "공동", "진동", "작동", "출동", "전면",
    "정면", "측면", "지면", "노면", "표면", "화면", "방면", "내면", "외면", "반면",
}  # fmt: skip
# 동 이름 뒤 지물 표현이 끝나는 곳(조사·서술)
LANDMARK_END = re.compile(
    r"\s*(?:에서|에 있는|관련하여|을 지나는데|를 지나는데|쪽을|쪽에서|입니다|이고|,|\.)"
)
LANDMARK_MAX_CHARS = 30

# 긴급도(PRD 5.5): 인명·사고 위험 = 긴급, 생활 지장 = 높음, 불편 = 보통, 건의·문의 = 낮음
URGENCY_RULES: list[tuple[Urgency, re.Pattern[str]]] = [
    ("긴급", re.compile(
        r"사고|다칠|다쳤|부상|넘어질|넘어졌|붕괴|감전|화재|불이 |가스 냄새|위험|생명"
    )),
    ("높음", re.compile(r"할 수 없|못 하|못하|어렵습니다|어려워|막혀|막아|끊겨|고장|단수|잠을")),
    ("보통", re.compile(r"불편|지저분|냄새|소음|시끄")),
    ("낮음", re.compile(r"건의|제안|문의|궁금|바랍니다|좋겠습니다")),
]  # fmt: skip
REPEAT = re.compile(r"다시|또 |여러 번|반복|계속|재발|지난(?:달|주|번)에도|재신고|몇 번째|이전에도")
# 요청 유형: 앞에 있는 규칙이 먼저 적용된다
REQUEST_RULES: list[tuple[RequestType, re.Pattern[str]]] = [
    ("단속", re.compile(r"단속|과태료|계도|견인|불법")),
    ("신설·개선", re.compile(r"설치|신설|늘려|확충|개선|만들어|추가")),
    ("수리·조치", re.compile(r"고쳐|수리|보수|조치|교체|정비|치워|청소|제거|점검|확인해")),
    ("질의", re.compile(r"궁금|문의|알려|되나요|인가요|\?")),
    ("불만", re.compile(r"불만|화가|짜증|어이없|왜 ")),
]  # fmt: skip
AFFECTED_RULES: list[tuple[Affected, re.Pattern[str]]] = [
    ("장애인", re.compile(r"장애인|휠체어|시각장애|점자")),
    ("어린이", re.compile(r"어린이|아이|아동|학생|유모차|초등|등하교|통학")),
    ("노인", re.compile(r"어르신|노인|할머니|할아버지|노약자")),
    ("일반", re.compile(r"주민|시민|보행자|운전자|방문객|이용객")),
]  # fmt: skip


def _first_match(rules: list[tuple[T, re.Pattern[str]]], text: str) -> T | None:
    """규칙 목록에서 처음으로 맞는 값.

    파라미터:
        rules: (값, 패턴) 목록. 앞에 있을수록 우선
        text: 본문
    """
    for value, pattern in rules:
        if pattern.search(text):
            return value
    return None


def extract_place(text: str) -> Place:
    """본문에서 행정동과 그 뒤 지물 표현을 뽑는다(예: "수청동 골목 입구에서" → 수청동 / 골목 입구).

    파라미터:
        text: 마스킹된 본문
    """
    for match in DONG.finditer(text):
        dong = match.group(1)
        if dong in DONG_STOPWORDS:
            continue
        rest = text[match.end() :].lstrip()
        end = LANDMARK_END.search(rest)
        landmark = rest[: end.start()].strip() if end else None
        if landmark and len(landmark) > LANDMARK_MAX_CHARS:
            landmark = None
        return Place(dong=dong, landmark=landmark or None)
    return Place()


def extract_by_rules(record: ComplaintRecord) -> Attributes:
    """규칙·사전으로 속성 6종을 뽑는다(PRD 6장 A. 로컬 방식).

    파라미터:
        record: 마스킹된 민원
    """
    text = record.text
    facility = FACILITY_PATTERN.search(text)
    return Attributes(
        record_id=record.record_id,
        facility=FACILITY_SYNONYMS[facility.group(0).lower()] if facility else None,
        place=extract_place(text),
        urgency=_first_match(URGENCY_RULES, text),
        repeat="재신고" if REPEAT.search(text) else None,
        request_type=_first_match(REQUEST_RULES, text),
        affected=_first_match(AFFECTED_RULES, text),
        analyzer=RULE_ANALYZER,
    )


LLM_SYSTEM = """너는 지방자치단체 민원 분석 도우미다. 민원 본문마다 속성 6종을 뽑아 JSON으로 답한다.
- facility: 문제가 생긴 물건·시설(가로등, 보도블록, 버스정류장 등). 짧은 명사로.
- dong: 본문에 적힌 동·읍·면 이름(예: 수청동). landmark: 그 장소의 지물 표현(예: 놀이터 옆).
- urgency: 인명·사고 위험 언급=긴급, 생활에 지장=높음, 불편=보통, 건의·문의=낮음.
- repeat: "다시", "여러 번", "지난달에도" 같은 표현이 있으면 재신고, 처음이라고 하면 최초.
- request_type: 민원인이 바라는 행동(수리·조치, 단속, 신설·개선, 질의, 불만).
- affected: 본문에 언급된 대상(어린이, 노인, 장애인). 취약계층 언급 없이 주민·보행자만 있으면 일반.
본문에 근거가 없는 항목은 반드시 null로 둔다. 추측하지 않는다.
[전화], [인명] 같은 표시는 개인정보를 가린 것이다. 입력의 id를 그대로 돌려준다."""


@dataclass
class LlmExtraction:
    """LLM 추출 결과와 실패한 묶음(행 단위 오류로 기록)."""

    attributes: list[Attributes] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def extract_by_llm(
    records: list[ComplaintRecord], client: LlmClient, batch_size: int = 25
) -> LlmExtraction:
    """민원 batch_size건씩 묶어 LLM에 보내 속성 6종을 받는다(PRD 6.1: 20~50건씩).

    실패한 묶음과 응답에서 빠진 민원은 errors에 남기고 나머지는 계속 처리한다.

    파라미터:
        records: 마스킹된 민원
        client: LLM 호출기(fast 배포)
        batch_size: 한 번에 보낼 민원 수
    """
    result = LlmExtraction()
    analyzer = f"llm:{client.usage.deployment}:{LLM_PROMPT_VERSION}"
    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        lines = "\n".join(f"[{r.record_id}] {r.text}" for r in batch)
        try:
            reply = await client.generate(
                prompt_version=LLM_PROMPT_VERSION,
                system=LLM_SYSTEM,
                masked_input=f"민원 {len(batch)}건:\n{lines}",
                response_model=LlmAttributeBatch,
            )
        except AppError as exc:
            result.errors.extend(f"{r.record_id}: {exc.code}" for r in batch)
            continue
        by_id = {item.id: item for item in reply.items}
        for record in batch:
            item = by_id.get(record.record_id)
            if item is None:
                result.errors.append(f"{record.record_id}: LLM 응답에서 빠짐")
                continue
            result.attributes.append(
                Attributes(
                    record_id=record.record_id,
                    facility=item.facility,
                    place=Place(dong=item.dong, landmark=item.landmark),
                    urgency=item.urgency,
                    repeat=item.repeat,
                    request_type=item.request_type,
                    affected=item.affected,
                    analyzer=analyzer,
                )
            )
    return result
