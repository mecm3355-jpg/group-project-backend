"""여러 원본을 공통 필드로 합치는 통합 데이터셋(실제 MongoDB 필요, 임시 DB 자동)."""

import csv
import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import JsonValue
from pymongo import MongoClient

from app.core.database import Document
from app.schemas.dataset import FieldMapping
from app.services.mapping import build_mapping_input, suggest_by_rules
from tests.conftest import TEST_URI, requires_db
from tests.test_analyze_flow import run_worker, upload

# 기관마다 열 이름·인코딩·좌표 표기가 다른 세 파일(포항·아산·김천 형태를 흉내 낸 가상 데이터)
POHANG = (
    "신고번호,경도,위도,행정동,신고상담내용,접수일시,분류번호\n"
    "1,36.03,129.36,양덕동,가로등이 꺼졌어요,2023-05-01 10:00,3\n"
    "2,36.01,129.34,대잠동,도로가 파였습니다,2023-06-02 09:30,7\n"
).encode("cp949")
ASAN = (
    "처리부서,처리팀,민원유형-중,접수일시,데이터 기준일\n"
    "도로과,보수팀,05.도로관리,2024-07-03 11:00,2025-09-01\n"
    "교통과,주차팀,01.교통관리,2024-07-15 14:00,2025-09-01\n"
    "교통과,주차팀,01.교통관리,2024-08-01 08:00,2025-09-01\n"
).encode("cp949")
KIMCHEON = (
    "시도,시군구,접수일,민원요지,민원제목,민원유형\n"
    "경북,김천시,2024-10-02,과태료 부과,(SPP-1) 횡단보도 불법 주정차 신고입니다.,일반\n"
).encode("cp949")


def test_rules_map_columns_and_fix_swapped_coordinates() -> None:
    columns = ["신고번호", "경도", "위도", "행정동", "신고상담내용", "접수일시", "처리완료일시"]
    statistics: dict[str, JsonValue] = {
        "경도": {"min": 35.8, "max": 36.1},
        "위도": {"min": 128.5, "max": 129.4},
    }

    mapping, constants, notes = suggest_by_rules(columns, statistics, "포항시_불편신고.csv")

    assert mapping == FieldMapping(
        source_id="신고번호",
        content="신고상담내용",
        received_at="접수일시",  # 처리완료일시는 아니다
        region="행정동",
        latitude="경도",  # 값 범위가 위도라서 바꿔 매핑
        longitude="위도",
    )
    assert constants.region is None  # 지역 열이 있으므로 고정값 없음
    assert any("반대" in n for n in notes)


def test_rules_suggest_region_constant_from_file_name() -> None:
    _, constants, _ = suggest_by_rules(
        ["처리부서", "민원유형-중", "접수일시"], {}, "충청남도 아산시_현황.csv"
    )

    assert constants.region == "아산시"


def test_mapping_input_has_no_values() -> None:
    text = build_mapping_input(
        [{"name": "신고상담내용", "inferredType": "string", "missingCount": 0, "uniqueCount": 2}],
        {"경도": {"min": 35.8, "max": 36.1}},
        "홍길동 주무관_010-1234-5678.csv",
    )

    payload = json.loads(text)
    assert payload["columns"][0]["name"] == "신고상담내용"
    assert "010-1234-5678" not in text
    assert "홍길동" not in text


def analyzed(client: TestClient, name: str, content: bytes) -> str:
    """파일을 올려 진단까지 끝낸 뒤 fileId를 돌려준다.

    파라미터:
        client: 테스트 클라이언트
        name: 파일명
        content: 파일 내용
    """
    file_id = upload(client, name, content)
    assert run_worker(client) == 1
    return file_id


@requires_db
def test_suggest_then_merge_three_sources(db_client: TestClient) -> None:
    pohang = analyzed(db_client, "경상북도 포항시_불편신고.csv", POHANG)
    asan = analyzed(db_client, "충청남도 아산시_생활민원.csv", ASAN)
    kimcheon = analyzed(db_client, "경상북도 김천시_불법주정차.csv", KIMCHEON)

    # AI 연결 정보가 없는 테스트 설정이므로 열 이름 규칙으로 제안한다
    suggestions = {}
    for file_id in (pohang, asan, kimcheon):
        res = db_client.post(f"/api/v1/files/{file_id}/mapping-suggestion")
        assert res.status_code == 200, res.text
        suggestions[file_id] = res.json()["data"]
    assert suggestions[pohang]["method"] == "rule"
    assert suggestions[pohang]["mapping"]["latitude"] == "경도"
    assert suggestions[asan]["constants"]["region"] == "아산시"

    # 담당자가 확인·수정: 김천은 제목을 본문으로, 지역은 시군구 열로
    kimcheon_mapping = suggestions[kimcheon]["mapping"]
    kimcheon_mapping.update({"content": "민원제목", "title": None, "region": "시군구"})
    body = {
        "name": "3개 시 통합",
        "sources": [
            {
                "fileId": f,
                "mapping": suggestions[f]["mapping"],
                "constants": suggestions[f]["constants"],
            }
            for f in (pohang, asan)
        ]
        + [{"fileId": kimcheon, "mapping": kimcheon_mapping}],
    }
    started = db_client.post("/api/v1/files/merge", json=body)
    assert started.status_code == 202, started.text
    merged_id = started.json()["data"]["fileId"]
    assert run_worker(db_client) == 1

    detail = db_client.get(f"/api/v1/files/{merged_id}").json()["data"]
    assert detail["status"] == "completed"
    assert detail["originalName"] == "3개 시 통합.csv"
    assert detail["sizeBytes"] > 0

    # 통합 파일은 일반 파일처럼 미리보기·진단·다운로드 API를 그대로 쓴다
    preview = db_client.get(f"/api/v1/files/{merged_id}/preview").json()["data"]
    assert preview["columns"][:6] == ["출처파일ID", "원본행", "원본ID", "제목", "본문", "접수일시"]
    first = dict(zip(preview["columns"], preview["rows"][0], strict=True))
    assert first["출처파일ID"] == pohang
    assert first["원본행"] == "2"
    assert first["본문"] == "가로등이 꺼졌어요"
    assert first["접수일시"] == "2023-05-01T10:00:00"
    assert (first["위도"], first["경도"]) == ("36.03", "129.36")  # 뒤바뀐 좌표가 바로잡힘

    versions = db_client.get(
        f"/api/v1/files/{merged_id}/versions", params={"type": "analysis"}
    ).json()["data"]["items"]
    assert versions[0]["rowCount"] == 6
    quality = db_client.get(f"/api/v1/files/{merged_id}/quality").json()["data"]
    assert quality["version"] == 1

    # 통합 파일도 정제할 수 있고, 정제 결과를 CSV로 내려받을 수 있다
    cleaned = db_client.post(
        f"/api/v1/files/{merged_id}/clean", json={"rules": [{"type": "remove_duplicates"}]}
    )
    assert cleaned.status_code == 202
    assert run_worker(db_client) == 1
    exported = db_client.get(f"/api/v1/files/{merged_id}/export", params={"format": "csv"})
    table = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert {row["지역"] for row in table} == {"양덕동", "대잠동", "아산시", "김천시"}
    assert {row["출처파일ID"] for row in table} == {pohang, asan, kimcheon}


@requires_db
def test_merge_distributions_are_saved(db_client: TestClient) -> None:
    pohang = analyzed(db_client, "포항시.csv", POHANG)
    asan = analyzed(db_client, "아산시.csv", ASAN)
    body = {
        "sources": [
            {
                "fileId": pohang,
                "mapping": {
                    "content": "신고상담내용",
                    "receivedAt": "접수일시",
                    "region": "행정동",
                },
                "notes": ["좌표 열은 값 범위로 바꿔 매핑"],
            },
            {
                "fileId": asan,
                "mapping": {"receivedAt": "접수일시", "sourceCategory": "민원유형-중"},
                "constants": {"region": "아산시"},
            },
        ]
    }
    merged_id = db_client.post("/api/v1/files/merge", json=body).json()["data"]["fileId"]
    run_worker(db_client)

    assert TEST_URI is not None
    app = db_client.app
    assert isinstance(app, FastAPI)
    db_name: str = app.state.settings.mongodb_db_name
    raw: MongoClient[Document]
    with MongoClient(TEST_URI) as raw:
        result = raw[db_name]["processing_results"].find_one(
            {"resultType": "analysis"}, sort=[("_id", -1)]
        )
    assert result is not None
    analysis = result["analysis"]
    assert analysis["merged"] is True
    assert [s["rows"] for s in analysis["sources"]] == [2, 3]
    assert analysis["sources"][0]["notes"] == ["좌표 열은 값 범위로 바꿔 매핑"]
    # 아산은 본문 열이 없으므로 본문 빈칸 3개는 결측이 아니라 해당 없음
    body = next(c for c in analysis["columns"] if c["name"] == "본문")
    assert (body["missingCount"], body["notApplicableCount"]) == (0, 3)
    issues = result["quality"]["issues"]
    assert not any(i["type"] == "missing_values" and i["location"] == "본문" for i in issues)
    dist = analysis["distributions"]
    assert dist["월별"] == {"2023-05": 1, "2023-06": 1, "2024-07": 2, "2024-08": 1}
    regions = {e["value"]: e["count"] for e in dist["지역"]["top"]}
    assert regions == {"아산시": 3, "양덕동": 1, "대잠동": 1}
    sources = {e["name"]: e["count"] for e in dist["출처파일ID"]["top"]}
    assert sources == {"포항시.csv": 2, "아산시.csv": 3}
    assert str(result["field"]) == merged_id


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"mapping": {"content": "없는열"}}, "UNKNOWN_COLUMN"),
        ({"mapping": {}}, "EMPTY_MAPPING"),
    ],
)
@requires_db
def test_merge_rejects_bad_mapping(
    db_client: TestClient, change: dict[str, object], code: str
) -> None:
    pohang = analyzed(db_client, "포항시.csv", POHANG)

    res = db_client.post("/api/v1/files/merge", json={"sources": [{"fileId": pohang, **change}]})

    assert res.status_code == 422
    assert res.json()["error"]["code"] == code


@requires_db
def test_merge_rejects_duplicate_and_non_tabular(db_client: TestClient) -> None:
    pohang = analyzed(db_client, "포항시.csv", POHANG)
    log = upload(db_client, "app.log", b"INFO start\n")
    mapping = {"content": "신고상담내용"}

    dup = db_client.post(
        "/api/v1/files/merge",
        json={"sources": [{"fileId": pohang, "mapping": mapping}] * 2},
    )
    assert dup.json()["error"]["code"] == "DUPLICATE_SOURCE"

    text = db_client.post(
        "/api/v1/files/merge", json={"sources": [{"fileId": log, "mapping": mapping}]}
    )
    assert text.json()["error"]["code"] == "NOT_TABULAR"


@requires_db
def test_mapping_suggestion_requires_analysis(db_client: TestClient) -> None:
    file_id = upload(db_client, "포항시.csv", POHANG)

    res = db_client.post(f"/api/v1/files/{file_id}/mapping-suggestion")

    assert res.status_code == 404
    assert res.json()["error"]["code"] == "RESULT_NOT_FOUND"
