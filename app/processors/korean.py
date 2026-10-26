"""한국어 처리 공통 도구(Kiwi 형태소 분석기). 분석기는 한 번만 만든다(로딩 1~2초)."""

import re
from functools import lru_cache

from kiwipiepy import Kiwi

HANGUL = re.compile(r"[가-힣]")


@lru_cache(maxsize=1)
def kiwi() -> Kiwi:
    """Kiwi 분석기를 한 번만 만든다.

    파라미터: 없음
    """
    return Kiwi()


def fix_spacing(text: str) -> str:
    """띄어쓰기를 다시 잡는다(OCR이 글자마다 띄운 "정 보 공개 요 청" → "정보 공개 요청").

    한글이 없는 줄(숫자·영문만)은 그대로 둔다.

    파라미터:
        text: 한 줄 글자
    """
    if not HANGUL.search(text):
        return text
    return str(kiwi().space(text, reset_whitespace=True))
