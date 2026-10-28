"""민원 분석 전체 흐름: 업로드 → 분석 작업 → 후보 → 승인·보류·합치기 → 다음 파일 배정 → 검토.

실제 MongoDB 필요(임시 DB 자동). 임베딩은 주제 낱말로 벡터를 만드는 가짜 임베더를 쓴다.
"""

import hashlib
from collections.abc import Iterator

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.services.embedding import Vectors, normalize, set_embedder_override
from tests.conftest import requires_db
from tests.test_analyze_flow import run_worker, upload

pytestmark = requires_db

TOPICS = ["가로등", "쓰레기", "주차"]


class TopicEmbedder:
    """본문의 주제 낱말로 축을 정하고 본문 해시로 작은 흔들림을 더한 벡터를 만든다."""

    model_name = "fake-topic"

    def embed(self, texts: list[str]) -> Vectors:
        """본문마다 16차원 정규화 벡터를 만든다.

        파라미터:
            texts: 본문 목록
        """
        rows = []
        for text in texts:
            vector = np.zeros(16)
            for index, topic in enumerate(TOPICS):
                if topic in text:
                    vector[index] = 1.0
            seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
            rows.append(vector + np.random.default_rng(seed).normal(0, 0.02, 16))
        return normalize(np.asarray(rows))


@pytest.fixture(autouse=True)
def fake_embedder() -> Iterator[None]:
    """이 모듈의 테스트는 가짜 임베더를 쓴다."""
    set_embedder_override(TopicEmbedder())
    yield
    set_embedder_override(None)


def csv_bytes(rows: list[tuple[str, str, str]]) -> bytes:
    """(본문, 접수일, 행정동) 행으로 CSV를 만든다.

    파라미터:
        rows: 행 목록
    """
    lines = ["번호,내용,접수일,행정동"]
    lines += [f"{i},{text},{day},{dong}" for i, (text, day, dong) in enumerate(rows, 1)]
    return "\n".join(lines).encode("utf-8")


MAPPING = {"content": "내용", "receivedAt": "접수일", "region": "행정동"}


def analyze(client: TestClient, file_id: str) -> dict[str, object]:
    """규칙 방식 민원 분석을 실행하고 민원 분석 요약(analysis.complaint)을 돌려준다.

    파라미터:
        client: 테스트 클라이언트
        file_id: 파일 ID
    """
    run_worker(client)  # 업로드 때 만든 추출·진단 작업을 먼저 끝낸다
    res = client.post(
        f"/api/v1/files/{file_id}/analysis",
        json={"type": "complaint", "mapping": MAPPING, "attributeMethod": "rule"},
    )
    assert res.status_code == 202, res.text
    run_worker(client)
    job = client.get(f"/api/v1/jobs/{res.json()['data']['jobId']}").json()["data"]
    assert job["status"] == "completed", job
    result = client.get(f"/api/v1/files/{file_id}/analysis").json()["data"]
    summary: dict[str, object] = result["analysis"]["complaint"]
    return summary


def first_month() -> list[tuple[str, str, str]]:
    """1월 민원: 주제마다 8건."""
    rows = []
    for i in range(8):
        rows.append((f"골목 가로등이 꺼져 어둡습니다 {i}", "2026-01-0" + str(i + 1), "수청동"))
        rows.append((f"공터에 쓰레기가 쌓여 있습니다 {i}", "2026-01-1" + str(i), "중앙동"))
        rows.append((f"상가 앞 불법 주차가 많습니다 {i}", "2026-01-2" + str(i), "채운동"))
    return rows


def candidate_for(client: TestClient, topic: str) -> dict[str, object]:
    """핵심어에 topic이 든 결정 전 후보.

    파라미터:
        client: 테스트 클라이언트
        topic: 주제 낱말
    """
    items = client.get("/api/v1/complaints/categories/candidates").json()["data"]["items"]
    found: dict[str, object] = next(c for c in items if any(topic in k for k in c["keywords"]))
    return found


def test_complaint_analysis_flow(db_client: TestClient) -> None:
    first = upload(db_client, "jan.csv", csv_bytes(first_month()))

    summary = analyze(db_client, first)
    assert summary["total"] == 24
    assert summary["unassignedRate"] == 1.0  # 첫 달은 체계가 없다
    assert summary["candidateCount"] == 3
    assert summary["taxonomyVersion"] == 0

    lights = candidate_for(db_client, "가로등")
    assert lights["size"] == 8
    exemplars = lights["exemplars"]
    assert isinstance(exemplars, list) and len(exemplars) == 5

    # 승인: 체계 v1, 소속 민원 8건 배정
    res = db_client.post(
        f"/api/v1/complaints/categories/candidates/{lights['candidateId']}/decision",
        json={
            "decision": "approved",
            "decidedBy": "analyst01",
            "name": "가로등 고장",
            "group": "안전·조명",
        },
    )
    assert res.status_code == 200, res.text
    decided = res.json()["data"]
    assert (decided["categoryId"], decided["taxonomyVersion"], decided["memberCount"]) == (
        "CAT-0001",
        1,
        8,
    )
    again = db_client.post(
        f"/api/v1/complaints/categories/candidates/{lights['candidateId']}/decision",
        json={"decision": "held", "decidedBy": "analyst01"},
    )
    assert again.status_code == 409

    taxonomy = db_client.get("/api/v1/complaints/categories").json()["data"]
    assert taxonomy["version"] == 1
    assert [(c["categoryId"], c["name"], c["group"]) for c in taxonomy["items"]] == [
        ("CAT-0001", "가로등 고장", "안전·조명")
    ]
    assigned = db_client.get("/api/v1/complaints", params={"categoryId": "CAT-0001"}).json()
    assert assigned["data"]["total"] == 8

    # 보류: 미배정으로 남는다. 합치기: 기존 카테고리로 들어간다(버전은 그대로)
    trash = candidate_for(db_client, "쓰레기")
    held = db_client.post(
        f"/api/v1/complaints/categories/candidates/{trash['candidateId']}/decision",
        json={"decision": "held", "decidedBy": "analyst01"},
    )
    assert held.json()["data"]["decision"] == "held"
    parking = candidate_for(db_client, "주차")
    merged = db_client.post(
        f"/api/v1/complaints/categories/candidates/{parking['candidateId']}/decision",
        json={"decision": "merged", "decidedBy": "analyst01", "mergedIntoCategoryId": "CAT-0001"},
    ).json()["data"]
    assert (merged["decision"], merged["taxonomyVersion"]) == ("merged", 1)
    unassigned = db_client.get("/api/v1/complaints", params={"assignStatus": "unassigned"})
    assert unassigned.json()["data"]["total"] == 8  # 보류한 쓰레기 민원만 남는다

    # 다음 달 파일: 가로등 민원은 자동 배정, 같은 장소 반복은 이슈로 묶임, 위험 언급은 긴급 검토
    second = upload(
        db_client,
        "feb.csv",
        csv_bytes(
            [
                ("수청동 놀이터 옆에서 가로등이 깜빡입니다 A", "2026-02-01", "수청동"),
                ("수청동 놀이터 옆에서 가로등이 깜빡입니다 B", "2026-02-03", "수청동"),
                ("가로등이 넘어질 것 같아 위험합니다", "2026-02-05", "수청동"),
            ]
        ),
    )
    summary = analyze(db_client, second)
    assert summary["total"] == 3
    assert summary["assignedRate"] == 1.0
    assert summary["taxonomyVersion"] == 1
    assert summary["issuesCreated"] == 1
    assert summary["reviewReasons"] == {"urgent": 1}

    reviews = db_client.get("/api/v1/complaints/reviews").json()["data"]
    assert reviews["total"] == 1
    [review] = reviews["items"]
    assert (review["reason"], review["suggestedCategoryId"]) == ("urgent", "CAT-0001")
    done = db_client.post(
        f"/api/v1/complaints/reviews/{review['reviewId']}",
        json={"categoryId": "CAT-0001", "reviewedBy": "analyst01"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["data"]["status"] == "done"
    confirmed = db_client.get("/api/v1/complaints", params={"assignStatus": "confirmed"})
    assert confirmed.json()["data"]["total"] == 1

    # 같은 파일을 다시 분석하면 이전 민원은 지워지고(deleted) 새 민원으로 바뀐다
    analyze(db_client, second)
    listed = db_client.get("/api/v1/complaints", params={"fileId": second}).json()["data"]
    assert listed["total"] == 3

    # 파일을 지우면 그 파일의 민원과 검토도 지워진다
    assert db_client.delete(f"/api/v1/files/{second}").status_code == 200
    gone = db_client.get("/api/v1/complaints", params={"fileId": second}).json()["data"]
    assert gone["total"] == 0


def test_analysis_request_is_validated(db_client: TestClient) -> None:
    file_id = upload(db_client, "jan.csv", csv_bytes(first_month()))
    run_worker(db_client)

    no_mapping = db_client.post(
        f"/api/v1/files/{file_id}/analysis", json={"type": "complaint", "attributeMethod": "rule"}
    )
    assert no_mapping.json()["error"]["code"] == "MAPPING_REQUIRED"
    # 테스트 설정에는 LLM 연결 정보가 없다: llm을 명시하면 거절, 기본(auto)은 규칙으로 접수
    llm = db_client.post(
        f"/api/v1/files/{file_id}/analysis", json={"mapping": MAPPING, "attributeMethod": "llm"}
    )
    assert (llm.status_code, llm.json()["error"]["code"]) == (503, "LLM_NOT_CONFIGURED")
    auto = db_client.post(f"/api/v1/files/{file_id}/analysis", json={"mapping": MAPPING})
    assert auto.status_code == 202, auto.text
    run_worker(db_client)
    summary = db_client.get(f"/api/v1/files/{file_id}/analysis").json()["data"]["analysis"]
    assert summary["complaint"]["attributeMethod"] == "rule"
    bad_rule = db_client.post(
        f"/api/v1/files/{file_id}/analysis",
        json={"mapping": MAPPING, "splitRule": "none", "attributeMethod": "rule"},
    )
    assert bad_rule.json()["error"]["code"] == "SPLIT_RULE_NOT_FOUND"
    missing = db_client.post(
        "/api/v1/complaints/categories/candidates/68fb12a1aaaaaaaaaaaaaaaa/decision",
        json={"decision": "held", "decidedBy": "x"},
    )
    assert missing.status_code == 404
