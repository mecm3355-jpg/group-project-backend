"""개인정보 마스킹(PRD 10장). 모든 분석보다 먼저 실행하고, 이후 단계와 LLM에는 마스킹본만 넘긴다.

| 대상 | 방법 | 치환 |
| 주민등록번호·계좌번호 | 정규식 | [식별번호] |
| 전화번호 | 정규식 | [전화] |
| 이메일 | 정규식 | [이메일] |
| 차량번호 | 정규식 | [차량] |
| 사람 이름 | 직함·호칭 패턴("○○○ 주무관", "성명: ○○○") | [인명] |
| 상세 주소(번지·호수) | 정규식, 행정동까지는 남김 | 수청동 [상세주소] |

사람 이름은 개체명 인식 모델 없이 패턴으로만 찾으므로 놓치는 경우가 있다.
PRD 10장대로 표본 100건을 사람이 점검해 누락률을 기록한다.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

PII_TYPES = ("식별번호", "전화", "이메일", "차량", "인명", "상세주소")

# 주민등록번호: 생년월일 6자리 + 성별 자리(1~4) + 6자리
RESIDENT_ID = re.compile(r"(?<!\d)\d{6}\s?-\s?[1-4]\d{6}(?!\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 휴대전화·지역번호 전화, 대표번호(15xx·16xx·18xx-xxxx)
PHONE = re.compile(
    r"(?<!\d)(?:0(?:1[016789]|2|[3-6][1-5]|70|50\d?)[-.\s)]?\d{3,4}[-.\s]?\d{4}"
    r"|1[568]\d{2}-\d{4})(?!\d)"
)
# 계좌번호: 하이픈으로 이어진 숫자 묶음 3~4개, 숫자 합계 10자리 이상(날짜 2026-07-14 제외)
ACCOUNT_CANDIDATE = re.compile(r"(?<![\d-])\d{2,6}(?:-\d{2,8}){2,3}(?![\d-])")
ACCOUNT_MIN_DIGITS = 10
# 차량번호: (지역명) + 2~3자리 + 용도 한글 1자 + 4자리 (예: 12가3456, 서울 34나 5678)
VEHICLE = re.compile(
    r"(?<![\d가-힣])(?:[가-힣]{2}\s?)?\d{2,3}\s?"
    r"[가나다라마거너더러머버서어저고노도로모보소오조구누두루무부수우주아바사자배하허호]"
    r"\s?\d{4}(?!\d)"
)
SURNAMES = (
    "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구민진나지엄채원"
    "천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은봉경편용예봉"
)
NAME = rf"[{SURNAMES}][가-힣]{{1,2}}"
# "홍길동 주무관", "김철수씨" — 이름 뒤 직함·호칭(뒤에 조사가 붙어도 된다)
TITLED_NAME = re.compile(
    rf"(?<![가-힣])({NAME})(\s?)(주무관|팀장|과장|계장|국장|실장|소장|주임|대리|반장|통장|이장|씨)"
)
# 표지 뒤 이름 다음에 올 수 있는 것: 공백·기호·끝, 또는 조사·서술어
NAME_END = r"(?=[^가-힣]|$|입니다|이에요|예요|이고|이며|이|가|은|는|의|께|님|씨)"
# "성명: 홍길동", "이름 홍길동", "작성자 : 홍길동"
LABELED_NAME = re.compile(rf"((?:성명|이름|작성자|신고인|민원인)\s*[:：]?\s*)({NAME}){NAME_END}")
# 상세 주소: 도로명 + 건물번호, 지번(번지), 산 번지, 동·호수. 행정동 이름은 남긴다
# "번길"이 바로 뒤에 오는 숫자는 도로명의 일부(한동로106번길)라 건물번호로 보지 않는다
ROAD_ADDRESS = re.compile(
    r"([가-힣A-Za-z\d]+(?:로|길))\s?\d+(?:-\d+)?(?:번길\s?\d+(?:-\d+)?)?(?!\d|번길)"
)
# 지번 뒤에 . : / 가 오면 날짜·시각(2023.05.12, 10:58)이므로 제외한다
LOT_ADDRESS = re.compile(
    r"((?<![가-힣])[가-힣]+(?:동|리|가)\s)(?:산\s?)?\d+(?:-\d+)?(?:\s?번지)?(?![\d가-힣.:/])"
)
LOT_NUMBER = re.compile(r"(?<!\d)(?:산\s?)?\d+(?:-\d+)?\s?번지")
UNIT = re.compile(r"(?<!\d)\d+\s?동\s?\d+\s?호(?![가-힣])|(?<![\d가-힣])\d{2,4}\s?호(?![가-힣])")


@dataclass
class MaskResult:
    """마스킹 결과: 치환한 본문과 대상별 치환 횟수."""

    text: str
    counts: Counter[str] = field(default_factory=Counter)

    @property
    def masked(self) -> bool:
        """하나라도 치환했는지.

        파라미터: 없음
        """
        return sum(self.counts.values()) > 0


def _sub(pattern: re.Pattern[str], replacement: str, label: str, result: MaskResult) -> None:
    """패턴에 맞는 부분을 치환하고 횟수를 센다. replacement에는 \\1 같은 그룹 참조를 쓸 수 있다.

    파라미터:
        pattern: 찾을 패턴
        replacement: 바꿀 문자열
        label: 대상 이름(PII_TYPES 중 하나)
        result: 마스킹 중인 결과(본문과 횟수가 바뀐다)
    """
    result.text, n = pattern.subn(replacement, result.text)
    if n:
        result.counts[label] += n


def _mask_accounts(result: MaskResult) -> None:
    """계좌번호 후보 중 숫자가 10자리 이상인 것만 치환한다(날짜·짧은 번호 제외).

    파라미터:
        result: 마스킹 중인 결과
    """

    def replace(match: re.Match[str]) -> str:
        """후보 하나를 판정해 치환 문자열을 돌려준다."""
        digits = sum(c.isdigit() for c in match.group(0))
        if digits < ACCOUNT_MIN_DIGITS:
            return match.group(0)
        result.counts["식별번호"] += 1
        return "[식별번호]"

    result.text = ACCOUNT_CANDIDATE.sub(replace, result.text)


def mask_text(text: str) -> MaskResult:
    """본문의 개인정보를 치환한다. 숫자 패턴은 겹치지 않도록 긴 것부터 처리한다.

    순서: 이메일 → 주민등록번호 → 전화번호 → 계좌번호 → 차량번호 → 상세 주소 → 사람 이름

    파라미터:
        text: 마스킹할 원문
    """
    result = MaskResult(text)
    _sub(EMAIL, "[이메일]", "이메일", result)
    _sub(RESIDENT_ID, "[식별번호]", "식별번호", result)
    _sub(PHONE, "[전화]", "전화", result)
    _mask_accounts(result)
    _sub(VEHICLE, "[차량]", "차량", result)
    _sub(UNIT, "[상세주소]", "상세주소", result)
    _sub(LOT_NUMBER, "[상세주소]", "상세주소", result)
    _sub(ROAD_ADDRESS, r"\1 [상세주소]", "상세주소", result)
    _sub(LOT_ADDRESS, r"\1[상세주소]", "상세주소", result)
    _sub(TITLED_NAME, r"[인명]\2\3", "인명", result)
    _sub(LABELED_NAME, r"\1[인명]", "인명", result)
    return result


def find_pii(text: str) -> list[str]:
    """본문에 남아 있는 개인정보 종류를 돌려준다. LLM 전송 직전 이중 검사에 쓴다(PRD 12장 리스크).

    파라미터:
        text: 검사할 본문(마스킹본이어야 한다)
    """
    return sorted(mask_text(text).counts)
