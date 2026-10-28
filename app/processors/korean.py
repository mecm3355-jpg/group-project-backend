"""한국어 처리 공통 도구(Kiwi 형태소 분석기). 분석기는 한 번만 만든다(로딩 1~2초)."""

import re
from functools import lru_cache

from kiwipiepy import Kiwi

# 완성형 한글 음절 범위. 띄어쓰기 교정이 필요한 줄인지 가려내는 데만 쓴다
HANGUL = re.compile(r"[가-힣]")


# 모델 로딩이 느리므로 프로세스당 하나만 만들어 재사용한다(워커가 첫 사용 때 한 번 낸다)
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
    # 숫자·영문 줄(날짜·번호)은 Kiwi가 오히려 띄어쓰기를 흩뜨릴 수 있어 건드리지 않는다
    if not HANGUL.search(text):
        return text
    # reset_whitespace=True: OCR이 넣은 공백을 모두 지우고 처음부터 다시 띄운다
    # (기존 공백을 살리면 글자 사이에 낀 잘못된 공백이 그대로 남는다)
    return str(kiwi().space(text, reset_whitespace=True))
