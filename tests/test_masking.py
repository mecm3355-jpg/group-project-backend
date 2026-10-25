import pytest

from app.services.masking import find_pii, mask_text


@pytest.mark.parametrize(
    ("text", "expected", "label"),
    [
        # 전화번호
        ("연락처 010-1234-5678 입니다", "연락처 [전화] 입니다", "전화"),
        ("01012345678로 연락", "[전화]로 연락", "전화"),
        ("054.270.1234", "[전화]", "전화"),
        ("(02)123-4567", "([전화]", "전화"),
        ("콜센터 1588-1234", "콜센터 [전화]", "전화"),
        # 주민등록번호·계좌번호
        ("주민번호 900101-1234567", "주민번호 [식별번호]", "식별번호"),
        ("계좌 110-123-456789 입금", "계좌 [식별번호] 입금", "식별번호"),
        ("1002-123-456789", "[식별번호]", "식별번호"),
        # 이메일
        ("메일 hong.gd@example.co.kr 로", "메일 [이메일] 로", "이메일"),
        # 차량번호
        ("12가3456 차량이 불법주차", "[차량] 차량이 불법주차", "차량"),
        ("123나 4567 견인 요청", "[차량] 견인 요청", "차량"),
        ("경북 34바5678", "[차량]", "차량"),
        # 사람 이름(직함·호칭, 표지)
        ("홍길동 주무관님께 문의", "[인명] 주무관님께 문의", "인명"),
        ("김철수씨가 신고", "[인명]씨가 신고", "인명"),
        ("성명: 이영희 / 내용", "성명: [인명] / 내용", "인명"),
        # 상세 주소(행정동은 남김)
        ("수청동 123-4 앞 가로등", "수청동 [상세주소] 앞 가로등", "상세주소"),
        ("양덕동 산12-3번지", "양덕동 [상세주소]", "상세주소"),
        ("중앙로 123 앞", "중앙로 [상세주소] 앞", "상세주소"),
        ("새천년대로123번길 45", "새천년대로123번길 [상세주소]", "상세주소"),
        ("현대아파트 101동 1203호", "현대아파트 [상세주소]", "상세주소"),
    ],
)
def test_masks_each_type(text: str, expected: str, label: str) -> None:
    result = mask_text(text)

    assert result.text == expected
    assert result.counts[label] >= 1


@pytest.mark.parametrize(
    "text",
    [
        "2026-07-14 접수",  # 날짜는 계좌번호가 아니다
        "수청동 놀이터 옆 가로등이 깜빡입니다",  # 행정동·지물은 남긴다
        "지하철 1호선 역 앞",  # 호선
        "30분 1000원",
        "고객님 불편을 드려 죄송합니다",  # 직함 없는 일반 명사
        "도로 파손이 3군데",
        "가로등 5개가 꺼짐",
        "가로수/녹지관리 2023.05.12 10:58",  # "관리"는 지명이 아니다
    ],
)
def test_keeps_non_pii(text: str) -> None:
    result = mask_text(text)

    assert result.text == text
    assert not result.masked


def test_masks_mixed_complaint() -> None:
    text = (
        "신고인 홍길동입니다. 수청동 123-4 앞 12가3456 차량이 매일 불법주차합니다. "
        "010-9876-5432로 연락주세요. 김민수 주무관께 전달 부탁드립니다."
    )

    result = mask_text(text)

    assert result.text == (
        "신고인 [인명]입니다. 수청동 [상세주소] 앞 [차량] 차량이 매일 불법주차합니다. "
        "[전화]로 연락주세요. [인명] 주무관께 전달 부탁드립니다."
    )
    assert result.counts == {"인명": 2, "상세주소": 1, "차량": 1, "전화": 1}


def test_masking_is_idempotent() -> None:
    once = mask_text("홍길동 주무관 010-1234-5678 수청동 12-3").text

    assert mask_text(once).text == once
    assert find_pii(once) == []


def test_find_pii_lists_remaining_types() -> None:
    assert find_pii("연락 010-1234-5678, a@b.com") == ["이메일", "전화"]
