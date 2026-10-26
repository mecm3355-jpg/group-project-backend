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
    for token in kiwi().tokenize(MASK_TAG.sub(" ", text)):
        if token.tag not in NOUN_TAGS:
            continue
        start, end = token.start, token.start + token.len
        if nouns and nouns[-1][1] == start and token.tag != "SL":
            prev_start, _, prev_form = nouns[-1]
            nouns[-1] = (prev_start, end, prev_form + token.form)
        else:
            nouns.append((start, end, token.form))
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
    counts = [Counter(token for text in group for token in tokenizer(text)) for group in groups]
    total: Counter[str] = Counter()
    for count in counts:
        total.update(count)
    if not total:
        return [[] for _ in groups]
    average_words = sum(total.values()) / len(groups)

    keywords: list[list[str]] = []
    for count in counts:
        size = sum(count.values())
        scored = [
            (freq / size * math.log(1 + average_words / total[word]), word)
            for word, freq in count.items()
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))
        keywords.append([word for _, word in scored[:top_n]])
    return keywords
