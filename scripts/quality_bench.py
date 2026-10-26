"""품질 측정 기준선: 규칙 방식 개선 전후를 같은 지표로 비교한다(외부 호출 없음).

실행: python scripts/quality_bench.py --label "설명" [--labels 라벨링.csv] [--samples DIR]
지표
- ocr: 국민신문고 PDF 9개 OCR·분리를 교정본 450건과 대조(분리 재현율, 제목 교정 필요 비율 등)
- attr.synthetic: 가상민원 정답(긴급도·동·지물)과 규칙 속성 비교
- attr.llmAgreement: 규칙 속성과 LLM 속성(M2에서 받아 둔 응답 캐시, 새 호출 없음)의 일치율
  — 실제 정답이 생기기 전까지의 대리 지표(포항·가상민원)
- attr.coverage: 규칙 속성이 값을 채운 비율(포항)
- sim: 가상민원 월별 운영 시뮬레이션(config/analysis.toml, 첫 체계 5개월)
- names: 포항·국민신문고 교정본 군집의 핵심어 이름 표본(눈으로 확인)
- labels: 라벨링 양식이 채워져 있으면 그 정답으로 규칙 속성 정확도와 라벨러 간 일치도
산출물(reports/, git 제외): reports/quality/history.jsonl(누적), latest.md(직전 실행과 비교)
"""

import argparse
import asyncio
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from m2_simulation import (  # noqa: E402
    ATTRIBUTE_FIELDS,
    agreement,
    attribute_value,
    compare_with_gold,
    coverage,
    load_pohang,
    load_sinmungo,
    load_synthetic,
)
from ocr_eval import evaluate as evaluate_ocr  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.core.analysis_config import load_analysis_config  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.schemas.complaint_analysis import Attributes, ComplaintRecord  # noqa: E402
from app.services.attributes import (  # noqa: E402
    extract_by_llm,
    extract_by_rules,
    refine_with_prototypes,
)
from app.services.discovery import discover  # noqa: E402
from app.services.embedding import (  # noqa: E402
    CachedEmbedder,
    SentenceTransformerEmbedder,
    Vectors,
)
from app.services.llm import ChatReply, LlmClient  # noqa: E402
from app.services.naming import keyword_name  # noqa: E402
from app.services.ocr_setup import ocr_options  # noqa: E402
from app.services.simulation import run_simulation  # noqa: E402
from app.services.storage import LocalStorage  # noqa: E402

CACHE = Path.home() / ".cache" / "datalens"
# 라벨링 양식 열 → 속성 이름
LABEL_COLUMNS = {
    "긴급도": "urgency",
    "대상시설": "facility",
    "장소": "landmark",
    "요청유형": "request_type",
    "영향대상": "affected",
}


class CacheOnlyBackend:
    """LLM 응답 캐시만 쓰는 가짜 백엔드. 캐시에 없으면 호출하지 않고 실패한다."""

    def __init__(self, deployment: str) -> None:
        """백엔드를 만든다.

        파라미터:
            deployment: 캐시를 만든 배포 이름(캐시 위치에 들어간다)
        """
        self.deployment = deployment

    async def complete(self, system: str, user: str, response_model: type[BaseModel]) -> ChatReply:
        """항상 실패한다(외부 호출 금지).

        파라미터:
            system: 시스템 지시문
            user: 사용자 메시지
            response_model: 응답 모델
        """
        raise AppError("CACHE_MISS", "캐시에 없는 요청입니다.", 500)


def llm_reference(records: list[ComplaintRecord]) -> dict[str, Attributes]:
    """M2 실행 때 받아 둔 LLM 속성(캐시)을 읽는다. 캐시에 없는 묶음은 빠진다.

    파라미터:
        records: M2와 같은 순서·같은 마스킹본의 민원(가상민원 + 포항)
    """
    settings = get_settings()
    deployment = settings.azure_openai_deployment_fast or "gpt-5.6-luna-1"
    client = LlmClient(CacheOnlyBackend(deployment), LocalStorage(ROOT / "storage"))
    extraction = asyncio.run(extract_by_llm(records, client))
    return {a.record_id: a for a in extraction.attributes}


def flatten(prefix: str, value: object, out: dict[str, float]) -> None:
    """중첩 지표를 'a.b.c' 키의 숫자로 편다.

    파라미터:
        prefix: 앞 키
        value: 값
        out: 결과를 쌓을 dict
    """
    if isinstance(value, dict):
        for key, item in value.items():
            flatten(f"{prefix}.{key}" if prefix else str(key), item, out)
    elif isinstance(value, int | float) and not isinstance(value, bool):
        out[prefix] = float(value)


def label_scores(path: Path, rule_attrs: dict[str, Attributes]) -> dict[str, float]:
    """채워진 라벨링 양식으로 규칙 속성 정확도와 라벨러 간 카테고리 일치율을 계산한다.

    파라미터:
        path: 라벨링 CSV(scripts/m2_simulation.py가 만든 양식)
        rule_attrs: 원본ID → 규칙 속성
    """
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    scores: dict[str, float] = {}
    both = [r for r in rows if r.get("라벨A_카테고리") and r.get("라벨B_카테고리")]
    if both:
        same = sum(r["라벨A_카테고리"].strip() == r["라벨B_카테고리"].strip() for r in both)
        scores["labels.annotatorAgreement"] = round(same / len(both), 4)
        scores["labels.doubleLabeled"] = float(len(both))
    for column, name in LABEL_COLUMNS.items():
        labeled = [r for r in rows if r.get(column, "").strip() and r["원본ID"] in rule_attrs]
        if not labeled:
            continue
        correct = 0
        for row in labeled:
            found = (attribute_value(rule_attrs[row["원본ID"]], name) or "").replace(" ", "")
            correct += found == row[column].strip().replace(" ", "")
        scores[f"labels.rule.{name}"] = round(correct / len(labeled), 4)
        scores[f"labels.count.{name}"] = float(len(labeled))
    return scores


def main() -> None:
    """지표를 계산해 기록하고 직전 실행과 비교한 표를 쓴다.

    파라미터: 없음(명령줄 인자는 --help 참고)
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", required=True, help="이번 측정 설명(예: 기준선, 띄어쓰기 교정)")
    parser.add_argument("--samples", type=Path, default=ROOT / "샘플데이타")
    parser.add_argument("--labels", type=Path, help="채운 라벨링 양식 CSV")
    parser.add_argument("--skip-ocr", action="store_true", help="OCR 측정을 건너뛴다")
    args = parser.parse_args()
    out = ROOT / "reports" / "quality"
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    metrics: dict[str, float] = {}

    if not args.skip_ocr:
        settings = get_settings().model_copy(update={"storage_root": CACHE / "bench"})
        options = ocr_options(settings)
        if options is None:
            raise SystemExit("Tesseract 또는 한국어 모델이 없습니다(--skip-ocr로 건너뛸 수 있음).")
        ocr, _ = evaluate_ocr(args.samples, options)
        ocr.pop("reviewThresholds", None)
        ocr.pop("files", None)
        ocr.pop("seconds", None)
        flatten("ocr", ocr, metrics)
        print(f"[{time.time() - started:5.0f}s] OCR 측정 완료", flush=True)

    synthetic = load_synthetic(args.samples)
    pohang = load_pohang(args.samples)
    records = [item.record for item in synthetic]
    embedder = CachedEmbedder(SentenceTransformerEmbedder(), CACHE / "embeddings")
    config = load_analysis_config()
    vectors = embedder.embed([r.text for r in records])

    def rules(items: list[ComplaintRecord], item_vectors: Vectors) -> dict[str, Attributes]:
        """규칙 속성(+원형 분류 보강)."""
        refined = refine_with_prototypes(
            [extract_by_rules(r) for r in items], item_vectors, embedder, config.attributes
        )
        return {a.record_id: a for a in refined}

    rule_synthetic = rules(records, vectors)
    rule_pohang = rules(pohang, embedder.embed([r.text for r in pohang]))
    reference = llm_reference([*records, *pohang])
    flatten("attr.synthetic", compare_with_gold(synthetic, rule_synthetic), metrics)
    flatten(
        "attr.llmAgreement.synthetic",
        agreement(rule_synthetic, {k: v for k, v in reference.items() if k in rule_synthetic}),
        metrics,
    )
    flatten(
        "attr.llmAgreement.pohang",
        agreement(rule_pohang, {k: v for k, v in reference.items() if k in rule_pohang}),
        metrics,
    )
    flatten("attr.coverage.pohang", coverage(list(rule_pohang.values())), metrics)

    simulation = run_simulation(
        records,
        vectors,
        [item.category for item in synthetic],
        config,
        embedding_model=embedder.model_name,
        warmup_months=5,
    )
    flatten("sim", simulation.metrics, metrics)
    print(f"[{time.time() - started:5.0f}s] 속성·시뮬레이션 측정 완료", flush=True)

    real = [*pohang, *load_sinmungo(args.samples)]
    candidates = discover(
        real, embedder.embed([r.text for r in real]), config.discovery, embedding_model="bge-m3"
    )
    names = [keyword_name(c.keywords) for c in candidates[:15]]
    metrics["names.candidates"] = float(len(candidates))

    if args.labels:
        metrics.update(label_scores(args.labels, {**rule_pohang, **rule_synthetic}))

    history = out / "history.jsonl"
    previous: dict[str, float] = {}
    previous_label = "-"
    if history.is_file():
        lines = history.read_text(encoding="utf-8").strip().splitlines()
        if lines:
            last = json.loads(lines[-1])
            previous, previous_label = last["metrics"], last["label"]
    entry = {"at": datetime.now().isoformat(timespec="seconds"), "label": args.label}
    with history.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps({**entry, "metrics": metrics, "names": names}, ensure_ascii=False) + "\n"
        )

    lines_md = [
        f"# 품질 측정: {args.label}",
        "",
        f"- 측정: {entry['at']} · 비교 대상: {previous_label}",
        "- llmAgreement는 LLM 결과와의 일치율(대리 지표)이다. 실제 정답은 라벨링 후 labels.*",
        "",
        "| 지표 | 이전 | 이번 | 변화 |",
        "| --- | --- | --- | --- |",
    ]
    for key in sorted(metrics):
        now, before = metrics[key], previous.get(key)
        delta = "" if before is None else f"{now - before:+.4f}"
        lines_md.append(
            f"| {key} | {'-' if before is None else f'{before:.4f}'} | {now:.4f} | {delta} |"
        )
    lines_md += ["", "## 후보 이름 표본(핵심어 방식)", "", *[f"- {n}" for n in names]]
    (out / "latest.md").write_text("\n".join(lines_md) + "\n", encoding="utf-8")
    print(f"[{time.time() - started:5.0f}s] 완료: {out / 'latest.md'}")
    for key in ATTRIBUTE_FIELDS:
        print(key, metrics.get(f"attr.llmAgreement.pohang.{key}"))


if __name__ == "__main__":
    main()
