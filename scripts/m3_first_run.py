"""M3 첫 분석: 실제 샘플을 API·워커 그대로 돌려 카테고리 후보와 분석 결과 리포트를 만든다.

실행: python scripts/m3_first_run.py [--samples DIR] [--out DIR] [--attribute-method rule]
- 임시 MongoDB(종료하면 삭제)와 ~/.cache/datalens/m3-storage 저장소를 쓴다. Atlas에는 안 붙는다.
- 포항 불편신고(CSV, 경도·위도 열 이름이 뒤바뀌어 있어 매핑에서 바로잡음)와
  국민신문고 스캔 PDF(OCR → sinmungo 분리 규칙)를 올려 민원 분석을 실행한다.
- 승인은 하지 않는다. 후보 목록을 리포트로 남겨 담당자가 승인한다(PRD 5.1 1개월차).
- 속성 추출 기본값은 LLM(마스킹본을 Azure fast 배포로 전송), 이름 초안은 deep 배포.
산출물(reports/, git 제외): first-run.md, first-run.json
"""

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from pymongo_inmemory import MongoClient  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.database import Mongo  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.job_handlers import HANDLERS  # noqa: E402
from app.services.job_runner import JobRunner  # noqa: E402

POHANG_MAPPING = {
    "sourceId": "신고번호",
    "content": "신고상담내용",
    "receivedAt": "접수일시",
    "region": "행정동",
    "address": "소재지주소",
    # 원본의 "경도" 열에 위도 값이, "위도" 열에 경도 값이 들어 있다(PRD 3장 포항 주의점)
    "latitude": "경도",
    "longitude": "위도",
    "sourceCategory": "분류번호",
}


def drain(settings: Settings) -> int:
    """대기 작업을 모두 처리한다. 처리한 작업 수를 돌려준다.

    파라미터:
        settings: 앱 설정
    """

    async def run() -> int:
        """워커 한 개로 대기 작업이 없을 때까지 실행한다."""
        mongo = Mongo(settings.mongodb_uri, settings.mongodb_db_name)
        try:
            runner = JobRunner(mongo.db, settings, HANDLERS)
            count = 0
            while await runner.run_once():
                count += 1
            return count
        finally:
            await mongo.close()

    return asyncio.run(run())


def upload(client: TestClient, path: Path) -> str:
    """파일을 올리고 fileId를 돌려준다.

    파라미터:
        client: API 클라이언트
        path: 올릴 파일
    """
    with path.open("rb") as f:
        res = client.post("/api/v1/files", files={"file": (path.name, f)})
    res.raise_for_status()
    file_id: str = res.json()["data"]["fileId"]
    return file_id


def main() -> None:
    """샘플을 올려 민원 분석을 실행하고 리포트를 쓴다.

    파라미터: 없음(명령줄 인자는 --help 참고)
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", type=Path, default=ROOT / "샘플데이타")
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "m3")
    parser.add_argument("--attribute-method", choices=["auto", "llm", "rule"], default="auto")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.time()

    def log(message: str) -> None:
        """진행 상황을 출력한다."""
        print(f"[{time.time() - started:6.0f}s] {message}", flush=True)

    mongo = MongoClient()
    try:
        settings = Settings(
            mongodb_uri=mongo._mongod.connection_string,
            mongodb_db_name="datalens_m3",
            # Google Drive 폴더 밖에 둔다(Drive 동기화가 확장자 없는 원본 이름을 바꾼다)
            storage_root=Path.home() / ".cache" / "datalens" / "m3-storage",
        )
        with TestClient(create_app(settings)) as client:
            pohang = upload(client, next(args.samples.glob("*포항시_불편신고접수*.csv")))
            pdfs = [upload(client, p) for p in sorted(args.samples.glob("국민신문고*.pdf"))]
            log(f"업로드: 포항 1개, 국민신문고 PDF {len(pdfs)}개 → 추출·진단")
            drain(settings)
            requests: list[tuple[str, dict[str, object]]] = [(pohang, {"mapping": POHANG_MAPPING})]
            requests += [(f, {"splitRule": "sinmungo"}) for f in pdfs]
            summaries = []
            for number, (file_id, body) in enumerate(requests, 1):
                res = client.post(
                    f"/api/v1/files/{file_id}/analysis",
                    json={"type": "complaint", "attributeMethod": args.attribute_method, **body},
                )
                res.raise_for_status()
                drain(settings)
                job = client.get(f"/api/v1/jobs/{res.json()['data']['jobId']}").json()["data"]
                if job["status"] != "completed":
                    raise SystemExit(f"분석 실패: {job}")
                result = client.get(f"/api/v1/files/{file_id}/analysis").json()["data"]
                summaries.append(
                    {
                        "file": client.get(f"/api/v1/files/{file_id}").json()["data"][
                            "originalName"
                        ],
                        "analysis": result["analysis"],
                        "quality": result["quality"],
                        "ai": result["ai"],
                    }
                )
                log(f"분석 {number}/{len(requests)}: {summaries[-1]['analysis']['complaint']}")
            candidates = client.get("/api/v1/complaints/categories/candidates").json()["data"]
            reviews = client.get("/api/v1/complaints/reviews", params={"limit": 200}).json()["data"]
            listed = client.get("/api/v1/complaints", params={"limit": 1}).json()["data"]
    finally:
        mongo.close()

    data = {
        "attributeMethod": args.attribute_method,
        "files": summaries,
        "complaints": listed["total"],
        "candidates": candidates["items"],
        "reviews": {
            "total": reviews["total"],
            "byReason": _count(r["reason"] for r in reviews["items"]),
        },
    }
    (args.out / "first-run.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_report(args.out / "first-run.md", data)
    log(f"완료: 민원 {listed['total']}건, 후보 {len(candidates['items'])}개 → {args.out}")


def _count(values: Iterable[object]) -> dict[str, int]:
    """값별 개수.

    파라미터:
        values: 값 반복자
    """
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def write_report(path: Path, data: dict[str, object]) -> None:
    """first-run.md를 쓴다.

    파라미터:
        path: 저장 위치
        data: first-run.json 내용
    """
    lines = [
        "# M3 첫 분석 리포트(카테고리 v1 승인 전)",
        "",
        f"- 속성 추출: {data['attributeMethod']} · 활성 민원 {data['complaints']}건",
        "- 승인 전 후보 목록이다. 담당자가 이름·상위 그룹을 확인해 승인·보류·합치기를 결정한다.",
        "",
        "## 파일별 결과",
        "",
        "| 파일 | 형식 | 민원 | 후보(누적 풀) | 검토 | 품질 문제 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    files = data["files"]
    assert isinstance(files, list)
    for item in files:
        analysis, quality = item["analysis"], item["quality"]
        summary = analysis["complaint"]
        issues = ", ".join(f"{i['type']} {i['count']}" for i in quality["issues"]) or "-"
        lines.append(
            f"| {item['file']} | {analysis.get('format')}/{analysis.get('method', '-')} "
            f"| {summary['total']} | {summary['candidateCount']}({summary['poolSize']}) "
            f"| {summary['reviewCount']} | {issues} |"
        )
    reviews = data["reviews"]
    assert isinstance(reviews, dict)
    lines += ["", f"검토 대기: {reviews['total']}건 {reviews['byReason']}", ""]
    lines += [
        "## 신규 카테고리 후보",
        "",
        "| 크기 | 제안 이름 | 상위 그룹 | 정의 | 핵심어 | 대표 민원 |",
    ]
    lines.append("| --- | --- | --- | --- | --- | --- |")
    candidates = data["candidates"]
    assert isinstance(candidates, list)
    for c in candidates:
        exemplar = " / ".join(e[:40].replace("|", " ") for e in c["exemplars"][:2])
        lines.append(
            f"| {c['size']} | {c['suggestedName']} | {c['suggestedGroup'] or '-'} "
            f"| {(c['suggestedDefinition'] or '-').replace('|', ' ')} "
            f"| {', '.join(c['keywords'][:5])} | {exemplar} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
