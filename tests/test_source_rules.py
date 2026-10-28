import pytest

from app.services.source_rules import extract_violation_place, normalize_result


@pytest.mark.parametrize(
    ("title", "place"),
    [
        ("(SPP-2401-0101651)소화전 불법 주정차 신고입니다.* 안전신문고 신고파", "소화전"),
        ("(SPP-2401-0103599)횡단보도 불법 주정차 신고입니다.* 안전신문고", "횡단보도"),
        ("(SPP-2401-01)교차로 모퉁이 불법 주정차 신고입니다.", "교차로 모퉁이"),
        ("(SPP-2401-01)어린이 보호구역 횡단보도 불법 주정차 신고입니다.", "어린이보호구역"),
        ("(SPP-2401-01)1, 횡단보도 불법 주정차 신고입니다.", "횡단보도"),
        ("(SPP-2401-01)기타 불법 주정차 신고입니다.", "기타"),
        ("(SPP-2401-01)회전교차로 불법 주정차 신고입니다.", "기타"),
        ("(SPP-2312-3003291)갓길주차를안했으면좋겠어요", "미추출"),
        ("스마트국민제보 민원이송 - [차량]", "미추출"),
    ],
)
def test_extract_violation_place(title: str, place: str) -> None:
    assert extract_violation_place(title) == place


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("과태료 부과", "과태료 부과"),
        (" 과태료 부과", "과태료 부과"),
        ("과태교 부과", "과태료 부과"),
        ("과태룔 부과", "과태료 부과"),
        ("과태 부과", "과태료 부과"),
        ("부과", "과태료 부과"),
        ("과태료 미부과", "과태료 미부과"),
        ("과태료미부과", "과태료 미부과"),
        ("경고장", "경고장"),
        ("", "미기재"),
        ("주정차위반 과태료 부과에 대한 답변", "기타 답변"),
        ("의견 검토중", "기타 답변"),
    ],
)
def test_normalize_result(raw: str, normalized: str) -> None:
    assert normalize_result(raw) == normalized
