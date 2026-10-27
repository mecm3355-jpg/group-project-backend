"""군집 핵심어(PRD 6장 명명, BERTopic 방식 c-TF-IDF).

명사는 Kiwi 형태소 분석으로 뽑는다. 군집마다 본문을 하나로 합쳐 단어 빈도를 세고,
여러 군집에 두루 나오는 단어(예: "민원", "부탁")는 가중치를 낮춘다.
"""

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence

from app.processors.korean import kiwi

Tokenizer = Callable[[str], list[str]]

# 일반 명사·고유 명사·외국어(CCTV 등)
NOUN_TAGS = {"NNG", "NNP", "SL"}
# 민원 본문에 두루 나오는 말. 핵심어에서 뺀다
STOPWORDS = {
    "민원", "신고", "부탁", "요청", "조치", "확인", "문제", "불편", "개선", "처리", "때문",
    "생각", "정도", "부분", "주변", "인근", "근처", "오늘", "어제", "아침", "저녁", "오후",
    "오전", "주말", "시간", "전부", "이상",
}  # fmt: skip
# 마스킹 표지([전화], [상세주소] 등). 형태소 분석 전에 지운다
MASK_TAG = re.compile(r"\[[^\]]{1,10}\]")


def noun_tokens(text: str) -> list[str]:
    """본문에서 두 글자 이상 명사를 뽑는다. 마스킹 표지([전화] 등)는 뺀다.

    띄어쓰기 없이 붙은 명사는 하나로 묶는다(점자+블록 → 점자블록, 버스+정류장 → 버스정류장).

    파라미터:
        text: 마스킹된 본문
    """
    nouns: list[tuple[int, int, str]] = []  # (시작, 끝, 낱말)
    # 마스킹 표지를 지우지 않으면 "전화"·"인명" 같은 표지 속 낱말이 핵심어로 뽑힌다.
    # 빈 문자열이 아니라 공백으로 바꿔 앞뒤 명사가 붙어 버리지 않게 한다
    for token in kiwi().tokenize(MASK_TAG.sub(" ", text)):
        if token.tag not in NOUN_TAGS:
            continue
        start, end = token.start, token.start + token.len
        # 앞 명사의 끝과 이번 명사의 시작이 맞닿아 있으면(띄어쓰기 없음) 복합 명사로 잇는다.
        # 외국어(SL)는 앞 명사에 잇지 않고 따로 둔다(예: CCTV는 CCTV 그대로 핵심어가 된다)
        if nouns and nouns[-1][1] == start and token.tag != "SL":
            prev_start, _, prev_form = nouns[-1]
            nouns[-1] = (prev_start, end, prev_form + token.form)
        else:
            nouns.append((start, end, token.form))
    # 한 글자 명사("곳", "것" 등)는 뜻이 약해 핵심어로 쓸모가 적으므로 뺀다
    return [form for _, _, form in nouns if len(form) >= 2 and form not in STOPWORDS]


def class_tfidf(
    groups: Sequence[Sequence[str]], top_n: int, tokenizer: Tokenizer = noun_tokens
) -> list[list[str]]:
    """군집마다 c-TF-IDF 점수가 높은 단어 top_n개를 고른다.

    점수 = (군집 내 단어 빈도 / 군집 전체 단어 수) × log(1 + 군집당 평균 단어 수 / 전체 빈도)

    파라미터:
        groups: 군집별 본문 목록
        top_n: 군집마다 고를 핵심어 수
        tokenizer: 본문 → 단어 목록(기본: Kiwi 명사)
    """
    # 군집 하나를 문서 하나로 보고(class-based) 군집별 단어 빈도를 센다
    counts = [Counter(token for text in group for token in tokenizer(text)) for group in groups]
    # 전체 빈도: 모든 군집에 걸친 단어별 등장 수. IDF 쪽 분모가 된다
    total: Counter[str] = Counter()
    for count in counts:
        total.update(count)
    # 명사가 하나도 없으면(빈 본문·마스킹 표지뿐) 나눗셈을 하지 않고 빈 핵심어를 돌려준다
    if not total:
        return [[] for _ in groups]
    average_words = sum(total.values()) / len(groups)

    keywords: list[list[str]] = []
    for count in counts:
        size = sum(count.values())
        # TF는 군집 안 비율이라 군집 크기 차이를 지우고, IDF는 여러 군집에 두루 많이 나오는
        # 단어일수록 total이 커져 log 값이 작아진다. 그래서 그 군집에만 많은 단어가 위로 온다
        scored = [
            (freq / size * math.log(1 + average_words / total[word]), word)
            for word, freq in count.items()
        ]
        # 점수가 같으면 낱말 순으로 정렬해 실행할 때마다 같은 핵심어가 나오게 한다(재현성)
        scored.sort(key=lambda item: (-item[0], item[1]))
        keywords.append([word for _, word in scored[:top_n]])
    return keywords
