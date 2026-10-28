"""민원 속성 6종 추출(PRD 5.5, 6장). 규칙 방식(A)과 LLM 방식(B)을 같은 결과 형식으로 돌려준다.

본문에 근거가 없는 속성은 None으로 둔다. 추측으로 채우지 않는다.
M2에서 두 방식을 비교해 어느 쪽을 기본으로 쓸지 정한다(PRD 12장).
"""

import re
from dataclasses import dataclass, field
from typing import TypeVar

import numpy as np

from app.core.analysis_config import AttributeConfig
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
from app.services.embedding import Embedder, Vectors, normalize
from app.services.llm import LlmClient

T = TypeVar("T")

# 결과 행마다 남기는 분석기 이름·버전(PRD 6장). 사전·규칙을 고치면 번호를 올려 이전 결과와
# 구분한다
RULE_ANALYZER = "rule-dict@2"
# 원형 분류로 보강한 행은 규칙 분석기 이름 뒤에 이 표시를 붙여, 어느 값이 임베딩 추정인지 남긴다
PROTOTYPE_SUFFIX = "+proto@1"
# 프롬프트(LLM_SYSTEM)를 고치면 올린다. LLM 응답 캐시가 이 버전 + 입력 해시로 묶인다(PRD 6.1)
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
    # M3 보강(포항·가상민원에서 LLM이 찾았지만 사전에 없던 시설·물건)
    "배수시설": "배수구", "덮개": "덮개", "뚜껑": "덮개", "담장": "담장",
    "와이파이": "와이파이", "wifi": "와이파이", "안내문": "안내판", "그늘막": "그늘막",
    "충전기": "충전시설", "콘센트": "콘센트", "자동문": "자동문", "냉방": "에어컨",
    "배기구": "배기구", "바닥분수": "바닥분수", "분수": "바닥분수", "벌집": "벌집",
    "쓰레기": "쓰레기", "폐기물": "쓰레기", "적치물": "적치물", "방치물": "적치물",
    "빈집": "빈집", "폐가": "빈집", "잔디": "잔디", "나뭇가지": "가로수", "가로수": "가로수",
    "수목": "가로수", "쉼터": "쉼터", "방지턱": "과속방지턱", "차선": "차선", "노면": "도로",
    "고드름": "고드름", "수도": "수도", "수돗물": "수도", "장애인주차구역": "장애인주차구역",
    "택시": "택시", "자전거": "자전거", "킥보드": "킥보드", "주차장": "주차장",
    "수도꼭지": "수도", "손잡이": "난간", "울타리": "펜스",
}  # fmt: skip
# 장소에 가까운 시설. 본문에 다른 시설이 있으면 그쪽을 대상 시설로 본다
# (예: "산책로 운동기구가 고장" → 운동기구)
PLACE_LIKE_FACILITIES = {"도로", "인도", "산책로", "교량", "버스정류장", "자전거길", "주차장"}
# 긴 표현부터 시도해야 "점자블록"이 "점자"보다, "버스정류장"이 "정류장"보다 먼저 맞는다.
# IGNORECASE는 cctv·wifi처럼 영문 대소문자가 섞여 쓰이는 표현용(사전 키는 소문자)
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
# 끝 표현을 못 찾아 문장 끝까지 이어 붙은 경우를 거르는 길이. 이보다 길면 지물이 아니라
# 문장 일부로 보고 버린다
LANDMARK_MAX_CHARS = 30

# 긴급도(PRD 5.5): 인명·사고 위험 = 긴급, 생활 지장 = 높음, 불편 = 보통, 건의·문의 = 낮음.
# 높은 단계부터 검사해, 위험 표현과 건의 표현이 함께 있으면 더 높은 쪽을 택한다
URGENCY_RULES: list[tuple[Urgency, re.Pattern[str]]] = [
    ("긴급", re.compile(
        r"사고|다칠|다쳤|부상|넘어질|넘어졌|붕괴|감전|화재|불이 |가스 냄새|위험|생명"
    )),
    ("높음", re.compile(r"할 수 없|못 하|못하|어렵습니다|어려워|막혀|막아|끊겨|고장|단수|잠을")),
    ("보통", re.compile(r"불편|지저분|냄새|소음|시끄")),
    ("낮음", re.compile(r"건의|제안|문의|궁금|바랍니다|좋겠습니다")),
]  # fmt: skip
# 재신고 표현(PRD 5.5). "또 "는 "또한"·"또는"을 피하려고 뒤에 공백을 붙였다
REPEAT = re.compile(r"다시|또 |여러 번|반복|계속|재발|지난(?:달|주|번)에도|재신고|몇 번째|이전에도")
# 요청 유형: 앞에 있는 규칙이 먼저 적용된다
REQUEST_RULES: list[tuple[RequestType, re.Pattern[str]]] = [
    ("단속", re.compile(r"단속|과태료|계도|견인|불법")),
    ("신설·개선", re.compile(r"설치|신설|늘려|확충|개선|만들어|추가")),
    ("수리·조치", re.compile(r"고쳐|수리|보수|조치|교체|정비|치워|청소|제거|점검|확인해")),
    ("질의", re.compile(r"궁금|문의|알려|되나요|인가요|\?")),
    ("불만", re.compile(r"불만|화가|짜증|어이없|왜 ")),
]  # fmt: skip
# 영향 대상: 취약계층(장애인·어린이·노인)을 먼저 보고, 언급이 없을 때만 일반으로 둔다.
# 일반 대상 표현조차 없으면 근거가 없으므로 None
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
    # 본문에서 처음 나온 진짜 동 이름 하나만 쓴다("운동"·"정면" 같은 일반 낱말은 건너뜀)
    for match in DONG.finditer(text):
        dong = match.group(1)
        if dong in DONG_STOPWORDS:
            continue
        # 동 이름 바로 뒤부터 조사·서술 표현이 나오기 전까지를 지물로 본다
        rest = text[match.end() :].lstrip()
        end = LANDMARK_END.search(rest)
        landmark = rest[: end.start()].strip() if end else None
        if landmark and len(landmark) > LANDMARK_MAX_CHARS:
            landmark = None
        # 빈 문자열 지물은 None으로 바꿔 "근거 없음"과 같게 다룬다
        return Place(dong=dong, landmark=landmark or None)
    return Place()


def extract_by_rules(record: ComplaintRecord) -> Attributes:
    """규칙·사전으로 속성 6종을 뽑는다(PRD 6장 A. 로컬 방식).

    파라미터:
        record: 마스킹된 민원
    """
    text = record.text
    # 본문에 나온 순서대로 시설을 표준 이름으로 바꾼다. 장소 성격의 시설(도로·산책로 등)은
    # 다른 시설이 없을 때만 대상 시설로 쓴다
    found = [FACILITY_SYNONYMS[m.group(0).lower()] for m in FACILITY_PATTERN.finditer(text)]
    specific = [f for f in found if f not in PLACE_LIKE_FACILITIES]
    # 각 속성은 규칙에 걸리는 표현이 없으면 None으로 둔다(추측하지 않음, PRD 5.5)
    return Attributes(
        record_id=record.record_id,
        facility=specific[0] if specific else found[0] if found else None,
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
    # 배포 이름과 프롬프트 버전을 분석기 이름에 넣어, 어느 모델·프롬프트로 뽑은 값인지 남긴다
    analyzer = f"llm:{client.usage.deployment}:{LLM_PROMPT_VERSION}"
    # 여러 건을 한 번에 묶으면 호출 수와 고정 프롬프트 토큰이 줄어든다(PRD 6.1).
    # 너무 크게 묶으면 응답이 길어져 형식이 깨질 때 잃는 건수가 커진다
    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        # 민원마다 [id]를 앞에 붙여, 응답 항목을 입력 민원과 id로 다시 맞춘다
        lines = "\n".join(f"[{r.record_id}] {r.text}" for r in batch)
        try:
            reply = await client.generate(
                prompt_version=LLM_PROMPT_VERSION,
                system=LLM_SYSTEM,
                masked_input=f"민원 {len(batch)}건:\n{lines}",
                response_model=LlmAttributeBatch,
            )
        except AppError as exc:
            # 한 묶음 실패로 작업을 멈추지 않고 행 단위 오류로 남긴다(PRD 10장 내결함)
            result.errors.extend(f"{r.record_id}: {exc.code}" for r in batch)
            continue
        # LLM이 순서를 바꾸거나 일부를 빼먹을 수 있어 위치가 아니라 id로 맞춘다
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


# 원형 문장: 값마다 그 값의 전형적인 민원 문장. 중심 벡터와의 유사도로 판정한다.
# 문장을 고치면 PROTOTYPE_SUFFIX 버전을 올린다
PROTOTYPES: dict[str, dict[str, list[str]]] = {
    "urgency": {
        "긴급": [
            "사고가 날 뻔했습니다 매우 위험합니다",
            "아이가 다칠 수 있어 위험합니다",
            "넘어질 위험이 있어 빨리 조치가 필요합니다",
            "화재나 감전이 걱정됩니다",
        ],
        "높음": [
            "고장이 나서 사용할 수 없습니다",
            "통행할 수 없을 정도입니다",
            "생활에 큰 지장이 있습니다",
            "물이 나오지 않아 곤란합니다",
        ],
        "보통": [
            "조금 불편합니다",
            "보기에 좋지 않습니다",
            "냄새가 나서 불쾌합니다",
            "지저분해서 정비가 필요합니다",
        ],
        "낮음": [
            "건의드립니다",
            "궁금한 점이 있어 문의합니다",
            "이런 시설이 있으면 좋겠습니다",
            "제도 개선을 제안합니다",
        ],
    },
    "request_type": {
        "수리·조치": [
            "고장 난 시설을 고쳐 주세요",
            "파손된 곳을 수리해 주세요",
            "쓰레기를 치워 주세요",
            "현장을 점검하고 조치해 주세요",
        ],
        "단속": [
            "불법 주차를 단속해 주세요",
            "무단투기를 단속해 주세요",
            "위반 차량에 과태료를 부과해 주세요",
        ],
        "신설·개선": [
            "가로등을 설치해 주세요",
            "횡단보도를 새로 만들어 주세요",
            "시설을 늘리고 개선해 주세요",
            "CCTV를 추가로 설치해 주세요",
        ],
        "질의": [
            "어떻게 해야 하는지 문의드립니다",
            "법령 해석을 요청합니다",
            "가능한지 알려 주세요",
            "기준이 무엇인지 궁금합니다",
        ],
        "불만": ["직원이 불친절합니다", "처리가 늦어 불만입니다", "왜 아직도 해결이 안 됩니까"],
    },
}


def refine_with_prototypes(
    attributes: list[Attributes], vectors: Vectors, embedder: Embedder, config: AttributeConfig
) -> list[Attributes]:
    """규칙으로 비어 있는 긴급도·요청 유형을 원형 문장과의 임베딩 유사도로 채운다.

    유사도가 기준 이상이고 1·2위 차이가 충분할 때만 채운다(근거가 약하면 비워 둔다).

    파라미터:
        attributes: 규칙 속성(vectors와 같은 순서)
        vectors: 민원의 정규화된 임베딩
        embedder: 원형 문장을 임베딩할 임베더(민원과 같은 모델)
        config: 원형 분류 기준
    """
    if not attributes:
        return attributes
    updates: list[dict[str, str]] = [{} for _ in attributes]
    for name, classes in PROTOTYPES.items():
        # 값마다 원형 문장 여러 개의 평균 벡터를 중심으로 쓴다. 문장 하나보다 표현 차이에 덜
        # 흔들린다. 민원과 같은 임베더라 같은 벡터 공간에서 비교된다
        labels = list(classes)
        centroids = normalize(np.stack([embedder.embed(classes[c]).mean(axis=0) for c in labels]))
        similarities = vectors @ centroids.T
        order = np.argsort(-similarities, axis=1)
        for index, attr in enumerate(attributes):
            # 규칙이 이미 채운 값은 본문 표현이라는 직접 근거가 있으므로 덮어쓰지 않는다
            if getattr(attr, name) is not None:
                continue
            first, second = int(order[index, 0]), int(order[index, 1])
            top = float(similarities[index, first])
            # 1위가 충분히 비슷하고 2위와도 구분될 때만 채운다. 아니면 근거가 약해 비워 둔다
            if (
                top >= config.prototype_min_similarity
                and top - float(similarities[index, second]) >= config.prototype_margin
            ):
                updates[index][name] = labels[first]
    # 하나라도 보강된 행만 새 객체로 만들고 분석기 이름에 원형 표시를 붙인다
    return [
        attr.model_copy(update={**update, "analyzer": attr.analyzer + PROTOTYPE_SUFFIX})
        if update
        else attr
        for attr, update in zip(attributes, updates, strict=True)
    ]
