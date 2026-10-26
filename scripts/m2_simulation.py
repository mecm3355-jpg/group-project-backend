"""M2 시뮬레이션 리포트(PRD 11장 M2, 9장 지표)를 만든다. DB 없이 샘플 파일만 읽는다.

실행: python scripts/m2_simulation.py [--samples 샘플데이타] [--out reports/m2] [--sweep] [--llm]
- 가상민원 월별 운영 시뮬레이션: 발견 품질·배정 정확도·신규 탐지율·미배정/애매 비율
- --sweep: 첫 체계 기간·응집도 하한·배정 기준 유사도·애매 구간을 바꿔 가며 추천값을 고른다
- 속성 추출: 규칙 방식을 가상민원 정답과 비교. --llm이면 fast 배포 결과와도 비교
  (가상민원 + 포항 마스킹본을 외부로 보낸다)
- 실데이터: 아산 월별 통계·급증(전년 같은 달 비교), 김천 정형 문구·10월 급증,
  포항·국민신문고 군집
- 평가셋 라벨링 양식(포항 116건 + 국민신문고 100건, 마스킹본)
산출물(reports/m2, git 제외): report.md, results.json, recommended-analysis.toml,
labeling-template.csv
모든 본문은 마스킹한 뒤에만 임베딩·LLM·산출물에 쓴다. 임베딩 캐시는 ~/.cache/datalens/embeddings.
"""

import argparse
import asyncio
import csv
import itertools
import json
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pydantic import JsonValue  # noqa: E402

from app.core.analysis_config import (  # noqa: E402
    AnalysisConfig,
    AssignmentConfig,
    SpikeConfig,
    load_analysis_config,
)
from app.processors.tabular import Cell, open_table  # noqa: E402
from app.schemas.complaint_analysis import Attributes, ComplaintRecord  # noqa: E402
from app.services.attributes import extract_by_llm, extract_by_rules  # noqa: E402
from app.services.discovery import discover  # noqa: E402
from app.services.embedding import (  # noqa: E402
    CachedEmbedder,
    Embedder,
    SentenceTransformerEmbedder,
)
from app.services.masking import mask_text  # noqa: E402
from app.services.profiling import parse_datetime  # noqa: E402
from app.services.simulation import (  # noqa: E402
    TARGETS,
    SimulationResult,
    meets_targets,
    run_simulation,
)
from app.services.source_rules import extract_violation_place, normalize_result  # noqa: E402
from app.services.spikes import MonthlyCounts, detect_spikes, monthly_counts  # noqa: E402

URGENCY_ORDER = ["낮음", "보통", "높음", "긴급"]
# 첫 체계(v1)를 만들 때 모을 개월 수(PRD 12장 "첫 달 데이터 규모")
SWEEP_WARMUPS = [1, 3, 5]
SWEEP_COHESIONS = [0.0, 0.86, 0.88]
SWEEP_THRESHOLDS = [0.70, 0.75, 0.80]
SWEEP_MARGINS = [0.02, 0.05]
KIMCHEON_RATIOS = [1.3, 1.5, 1.75, 2.0]


@dataclass
class Labeled:
    """정답이 있는 가상민원 1건."""

    record: ComplaintRecord
    category: str
    urgency: str
    dong: str
    landmark: str


def read_rows(path: Path) -> tuple[list[str], list[list[Cell]]]:
    """표 파일 하나를 (머리글, 행 목록)으로 읽는다.

    파라미터:
        path: CSV 또는 XLSX 경로
    """
    with open_table(path, path.suffix[1:].lower()) as table:
        return table.columns, list(table.rows)


def text_of(value: Cell) -> str:
    """칸 값을 문자열로(None은 빈 문자열).

    파라미터:
        value: 칸 값
    """
    return "" if value is None else str(value).strip()


def month_of(value: Cell) -> str | None:
    """칸 값의 접수월(YYYY-MM). 날짜가 아니면 None.

    파라미터:
        value: 날짜 칸 값
    """
    parsed = parse_datetime(text_of(value))
    return f"{parsed.year:04d}-{parsed.month:02d}" if parsed else None


def find(samples: Path, pattern: str) -> Path:
    """샘플 폴더에서 이름 패턴에 맞는 파일 하나를 찾는다.

    파라미터:
        samples: 샘플 폴더
        pattern: glob 패턴
    """
    matches = sorted(samples.glob(pattern))
    if not matches:
        raise SystemExit(f"샘플 파일이 없습니다: {samples / pattern}")
    return matches[0]


def load_synthetic(samples: Path) -> list[Labeled]:
    """가상민원 1,000건을 마스킹해 읽는다.

    파라미터:
        samples: 샘플 폴더
    """
    columns, rows = read_rows(find(samples, "생활불편_가상민원*.xlsx"))
    col = {name: i for i, name in enumerate(columns)}
    items: list[Labeled] = []
    for row in rows:
        items.append(
            Labeled(
                record=ComplaintRecord(
                    record_id=text_of(row[col["민원ID"]]),
                    text=mask_text(text_of(row[col["민원내용"]])).text,
                    month=month_of(row[col["가상접수일시"]]),
                ),
                category=text_of(row[col["대분류(정답)"]]),
                urgency=text_of(row[col["긴급도(정답)"]]),
                dong=text_of(row[col["가상지역"]]),
                landmark=text_of(row[col["발생장소"]]),
            )
        )
    return items


def load_pohang(samples: Path) -> list[ComplaintRecord]:
    """포항 불편신고 중 본문이 있는 행을 마스킹해 읽는다.

    파라미터:
        samples: 샘플 폴더
    """
    columns, rows = read_rows(find(samples, "*포항시_불편신고접수*.csv"))
    col = {name: i for i, name in enumerate(columns)}
    records: list[ComplaintRecord] = []
    for line, row in enumerate(rows, start=2):
        body = text_of(row[col["신고상담내용"]])
        if not body:
            continue
        number = text_of(row[col["신고번호"]])
        records.append(
            ComplaintRecord(
                record_id=f"POH-{number or f'L{line}'}",
                text=mask_text(body).text,
                month=month_of(row[col["접수일시"]]),
            )
        )
    return records


def load_sinmungo(samples: Path) -> list[ComplaintRecord]:
    """국민신문고 OCR 교정본 500건의 제목을 마스킹해 읽는다.

    파라미터:
        samples: 샘플 폴더
    """
    columns, rows = read_rows(find(samples, "국민신문고_OCR_민원제목.csv"))
    col = {name: i for i, name in enumerate(columns)}
    return [
        ComplaintRecord(
            record_id=f"SMG-{text_of(row[col['번호']])}",
            text=mask_text(text_of(row[col["제목"]])).text,
            month=month_of(row[col["등록일"]]),
        )
        for row in rows
        if text_of(row[col["제목"]])
    ]


# ---------------------------------------------------------------- 시뮬레이션


def config_with(
    base: AnalysisConfig, threshold: float, margin: float, cohesion: float
) -> AnalysisConfig:
    """배정 기준·애매 구간·응집도 하한만 바꾼 설정.

    파라미터:
        base: 기본 설정
        threshold: 배정 기준 유사도
        margin: 애매 구간
        cohesion: 후보 응집도 하한(0이면 끔)
    """
    return base.model_copy(
        update={
            "assignment": AssignmentConfig(threshold=threshold, ambiguity_margin=margin),
            "discovery": base.discovery.model_copy(update={"min_cohesion": cohesion}),
        }
    )


def score(metrics: dict[str, float | None]) -> tuple[int, float]:
    """추천 순위: (달성한 목표 수, 주요 지표 합). 둘 다 클수록 좋다.

    파라미터:
        metrics: 시뮬레이션 지표
    """
    passed = sum(meets_targets(metrics).values())
    total = sum(metrics.get(k) or 0.0 for k in ("purity", "assignmentAccuracy", "newDetectionRate"))
    total += 1 - (metrics.get("reviewRatio") or 1.0)
    return passed, round(total, 4)


# ---------------------------------------------------------------- 속성 비교


def compare_with_gold(items: list[Labeled], attrs: dict[str, Attributes]) -> dict[str, JsonValue]:
    """가상민원 정답(긴급도·지역·발생장소)과 비교한 정확도.

    파라미터:
        items: 정답이 있는 민원
        attrs: record_id → 추출 결과
    """
    exact = near = dong = landmark = total = 0
    for item in items:
        attr = attrs.get(item.record.record_id)
        if attr is None:
            continue
        total += 1
        if attr.urgency == item.urgency:
            exact += 1
        if attr.urgency in URGENCY_ORDER and item.urgency in URGENCY_ORDER:
            gap = abs(URGENCY_ORDER.index(attr.urgency) - URGENCY_ORDER.index(item.urgency))
            near += gap <= 1
        dong += attr.place.dong == item.dong
        found = (attr.place.landmark or "").replace(" ", "")
        landmark += bool(found) and item.landmark.replace(" ", "") in found
    return {
        "evaluated": total,
        "urgencyExact": round(exact / total, 4) if total else None,
        "urgencyWithinOne": round(near / total, 4) if total else None,
        "dong": round(dong / total, 4) if total else None,
        "landmark": round(landmark / total, 4) if total else None,
    }


ATTRIBUTE_FIELDS = ("facility", "dong", "landmark", "urgency", "repeat", "request_type", "affected")


def attribute_value(attr: Attributes, name: str) -> str | None:
    """속성 하나의 값(장소는 dong·landmark로 나눠 본다).

    파라미터:
        attr: 추출 결과
        name: ATTRIBUTE_FIELDS 중 하나
    """
    if name == "dong":
        return attr.place.dong
    if name == "landmark":
        return attr.place.landmark
    value = getattr(attr, name)
    return value if isinstance(value, str) else None


def coverage(attrs: list[Attributes]) -> dict[str, JsonValue]:
    """속성별로 값이 채워진 비율.

    파라미터:
        attrs: 추출 결과
    """
    if not attrs:
        return {}
    return {
        name: round(sum(attribute_value(a, name) is not None for a in attrs) / len(attrs), 4)
        for name in ATTRIBUTE_FIELDS
    }


def agreement(left: dict[str, Attributes], right: dict[str, Attributes]) -> dict[str, JsonValue]:
    """두 방식의 속성별 일치율(둘 다 결과가 있는 민원 기준, 공백 무시).

    파라미터:
        left: record_id → 규칙 방식 결과
        right: record_id → LLM 방식 결과
    """
    common = sorted(set(left) & set(right))
    if not common:
        return {}
    result: dict[str, JsonValue] = {"compared": len(common)}
    for name in ATTRIBUTE_FIELDS:
        same = 0
        for record_id in common:
            a = (attribute_value(left[record_id], name) or "").replace(" ", "")
            b = (attribute_value(right[record_id], name) or "").replace(" ", "")
            same += a == b
        result[name] = round(same / len(common), 4)
    return result


async def run_llm(records: list[ComplaintRecord]) -> tuple[list[Attributes], list[str], JsonValue]:
    """fast 배포로 속성을 뽑는다. 응답 캐시(storage/llm-cache)를 쓴다.

    파라미터:
        records: 마스킹된 민원
    """
    from app.core.config import get_settings
    from app.services.llm import create_llm_client

    client = create_llm_client(get_settings(), "fast")
    extraction = await extract_by_llm(records, client)
    return extraction.attributes, extraction.errors, client.usage.to_record()


# ---------------------------------------------------------------- 실데이터 통계


def series_json(series: MonthlyCounts) -> dict[str, JsonValue]:
    """달별 건수를 JSON으로.

    파라미터:
        series: 달별 건수
    """
    return {
        "counts": dict(series.counts),
        "partialMonths": list[JsonValue](sorted(series.partial)),
    }


def spike_rows(series: MonthlyCounts, config: SpikeConfig) -> list[dict[str, JsonValue]]:
    """급증 판정 결과를 JSON 행으로.

    파라미터:
        series: 달별 건수
        config: 급증 기준값
    """
    return [
        {
            "month": c.month,
            "count": c.count,
            "baseline": c.baseline,
            "ratio": c.ratio,
            "lastYear": c.last_year,
            "basicSpike": c.basic_spike,
            "spike": c.spike,
        }
        for c in detect_spikes(series, config)
    ]


def asan_stats(samples: Path, config: SpikeConfig) -> dict[str, JsonValue]:
    """아산 생활민원: 건수 일치, 중복·날짜 오류, 월별 추이, 유형별 급증(전년 비교 전후).

    파라미터:
        samples: 샘플 폴더
        config: 급증 기준값
    """
    columns, rows = read_rows(find(samples, "*아산시_생활민원처리현황*.csv"))
    col = {name: i for i, name in enumerate(columns)}
    duplicates = len(rows) - len({tuple(text_of(c) for c in row) for row in rows})
    dated: list[tuple[date, str]] = []
    bad_dates = 0
    for row in rows:
        parsed = parse_datetime(text_of(row[col["접수일시"]]))
        if parsed is None:
            bad_dates += 1
            continue
        dated.append((parsed.date(), text_of(row[col["민원유형-중"]])))
    # 날짜 오류: 자료 기준일 뒤의 접수일(미래 날짜)
    reference = max(
        (p.date() for row in rows if (p := parse_datetime(text_of(row[col["데이터 기준일"]])))),
        default=None,
    )
    future = [d for d, _ in dated if reference and d > reference]
    total = monthly_counts([d for d, _ in dated], config.min_month_coverage)
    by_type: dict[str, list[date]] = {}
    for day, kind in dated:
        by_type.setdefault(kind, []).append(day)

    type_spikes: dict[str, JsonValue] = {}
    seasonal_removed = 0
    basic_count = final_count = 0
    for kind, days in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        series = monthly_counts(days, config.min_month_coverage)
        # 유형별 시계열도 전체 자료 기간을 기준으로 일부 월을 정한다
        series = MonthlyCounts(
            total.months, {m: series.counts.get(m, 0) for m in total.months}, total.partial
        )
        checks = detect_spikes(series, config)
        basic = [c.month for c in checks if c.basic_spike]
        final = [c.month for c in checks if c.spike]
        basic_count += len(basic)
        final_count += len(final)
        seasonal_removed += len(set(basic) - set(final))
        type_spikes[kind] = {
            "rows": len(days),
            "basicSpikes": list[JsonValue](basic),
            "spikes": list[JsonValue](final),
        }
    return {
        "rows": len(rows),
        "countedRows": sum(total.counts.values()),
        "countMatches": sum(total.counts.values()) + bad_dates == len(rows),
        "duplicateRows": duplicates,
        "unparsedDates": bad_dates,
        "futureDates": len(future),
        "monthly": series_json(total),
        "totalSpikes": list[JsonValue](spike_rows(total, config)),
        "typeSpikes": type_spikes,
        "basicSpikeCount": basic_count,
        "spikeCount": final_count,
        "removedByLastYear": seasonal_removed,
    }


def kimcheon_stats(samples: Path, config: SpikeConfig) -> dict[str, JsonValue]:
    """김천 불법주정차: 위반 장소 추출률, 처리결과 정규화표, 월별 추이와 10월 급증.

    파라미터:
        samples: 샘플 폴더
        config: 급증 기준값
    """
    columns, rows = read_rows(find(samples, "*김천시_안전신문고*.csv"))
    col = {name: i for i, name in enumerate(columns)}
    places = Counter(extract_violation_place(text_of(row[col["민원제목"]])) for row in rows)
    raw_results = Counter(text_of(row[col["민원요지"]]) for row in rows)
    mapping = {raw: normalize_result(raw) for raw in raw_results}
    normalized: Counter[str] = Counter()
    for raw, count in raw_results.items():
        normalized[mapping[raw]] += count
    days = [
        p.date() for row in rows if (p := parse_datetime(text_of(row[col["접수일"]]))) is not None
    ]
    series = monthly_counts(days, config.min_month_coverage)
    sweep: dict[str, JsonValue] = {}
    for ratio in KIMCHEON_RATIOS:
        checks = detect_spikes(series, config.model_copy(update={"ratio": ratio}))
        sweep[str(ratio)] = [c.month for c in checks if c.spike]
    extracted = len(rows) - places.get("미추출", 0)
    return {
        "rows": len(rows),
        "countMatches": sum(series.counts.values()) == len(rows),
        "placeExtractedRate": round(extracted / len(rows), 4),
        "placeStandardRate": round((extracted - places.get("기타", 0)) / len(rows), 4),
        "places": dict(places.most_common()),
        "resultMapping": [
            {"raw": raw, "normalized": mapping[raw], "count": count}
            for raw, count in raw_results.most_common()
        ],
        "results": dict(normalized.most_common()),
        "monthly": series_json(series),
        "spikes": list[JsonValue](spike_rows(series, config)),
        "spikeMonthsByRatio": sweep,
    }


def cluster_summary(
    records: list[ComplaintRecord], embedder: Embedder, config: AnalysisConfig
) -> list[dict[str, JsonValue]]:
    """정답 없는 자료의 첫 달 발견 결과(군집 크기·핵심어·대표 민원 마스킹본 2건).

    파라미터:
        records: 마스킹된 민원
        embedder: 임베더
        config: 분석 기준값
    """
    vectors = embedder.embed([r.text for r in records])
    by_id = {r.record_id: r for r in records}
    candidates = discover(records, vectors, config.discovery, embedding_model=embedder.model_name)
    return [
        {
            "size": len(c.member_ids),
            "reason": c.reason,
            "keywords": list[JsonValue](c.keywords),
            "exemplars": [by_id[i].text[:80] for i in c.exemplar_ids[:2]],
        }
        for c in candidates
    ]


def labeling_template(
    pohang: list[ComplaintRecord], sinmungo: list[ComplaintRecord], path: Path
) -> int:
    """평가셋 라벨링 양식(PRD 9장: 포항 + 국민신문고 표본 100건, 2명 독립 라벨링).

    파라미터:
        pohang: 포항 민원(마스킹본)
        sinmungo: 국민신문고 제목(마스킹본)
        path: 저장할 CSV 경로(엑셀에서 열리도록 UTF-8 BOM)
    """
    sample = random.Random(42).sample(sinmungo, min(100, len(sinmungo)))
    header = [
        "평가ID", "소스", "원본ID", "본문(마스킹)", "라벨A_카테고리", "라벨B_카테고리",
        "확정_카테고리", "긴급도", "대상시설", "장소", "요청유형", "영향대상", "메모",
    ]  # fmt: skip
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for number, record in enumerate([*pohang, *sample], start=1):
            source = "포항" if record.record_id.startswith("POH") else "국민신문고"
            writer.writerow([f"EV-{number:03d}", source, record.record_id, record.text])
    return len(pohang) + len(sample)


# ---------------------------------------------------------------- 리포트


def fmt(value: JsonValue) -> str:
    """리포트 표의 칸 값.

    파라미터:
        value: 값
    """
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def metric_table(metrics: dict[str, float | None]) -> list[str]:
    """PRD 9장 지표 표(목표 달성 여부 포함).

    파라미터:
        metrics: 시뮬레이션 지표
    """
    passed = meets_targets(metrics)
    lines = ["| 지표 | 값 | 목표 | 달성 |", "| --- | --- | --- | --- |"]
    for name, value in metrics.items():
        target = TARGETS.get(name)
        mark = ("O" if passed[name] else "X") if name in TARGETS else ""
        goal = (
            "-" if target is None else (f"≤ {target}" if name == "reviewRatio" else f"≥ {target}")
        )
        lines.append(f"| {name} | {fmt(value)} | {goal} | {mark} |")
    return lines


def month_table(result: SimulationResult) -> list[str]:
    """월별 처리 결과 표.

    파라미터:
        result: 시뮬레이션 결과
    """
    lines = [
        "| 월 | 접수 | 배정 | 배정 정답 | 애매 | 미배정 | 미등장유형 | 흡수 "
        "| 풀 | 후보 | 체계 | 순도 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for m in result.months:
        lines.append(
            f"| {m.month} | {m.received} | {m.assigned} | {m.assigned_correct} | {m.ambiguous} "
            f"| {m.unassigned} | {m.unseen_type_records} | {m.unseen_type_absorbed} "
            f"| {m.pool_size} | {m.candidates} | {m.taxonomy_size} | {fmt(m.purity)} |"
        )
    return lines


def write_report(
    out: Path,
    data: dict[str, JsonValue],
    base: SimulationResult,
    recommended: SimulationResult | None,
) -> None:
    """report.md를 쓴다.

    파라미터:
        out: 산출물 폴더
        data: results.json 내용
        base: 기본 기준값 시뮬레이션 결과
        recommended: 추천 기준값 시뮬레이션 결과(--sweep일 때)
    """
    lines = [
        "# M2 시뮬레이션 리포트",
        "",
        f"- 생성: {data['generatedAt']} · 임베딩: {data['embeddingModel']}",
        "- 기준 문서: PRD 9장(지표), 5.3(운영 규칙), 8.2(급증). 목표치는 초기 가설이다.",
        "- 승인은 '후보를 모두 승인'으로 가정했고, 카테고리 정답은 소속 민원의 최다 정답 대분류다.",
        "",
        "## 1. 가상민원 월별 운영 시뮬레이션 (기본 기준값)",
        "",
        *metric_table(base.metrics),
        "",
        *month_table(base),
        "",
    ]
    found = base.new_type_found
    if found:
        lines.append(
            "신규 유형 발견(첫 등장 월 또는 다음 달): "
            + ", ".join(f"{k} {'O' if v else 'X'}" for k, v in sorted(found.items()))
        )
        lines.append("")
    lines += ["### 카테고리(승인 순)", "", "| ID | 월 | 크기 | 정답 | 비율 | 핵심어 |"]
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for c in base.categories:
        lines.append(
            f"| {c.category_id} | {c.month} | {c.size} | {c.gold_label} | {c.gold_share:.2f} "
            f"| {', '.join(c.keywords[:6])} |"
        )
    lines.append("")

    sweep = data.get("sweep")
    if isinstance(sweep, list) and sweep:
        lines += [
            "## 2. 기준값 탐색",
            "",
            "| 첫 체계 | 응집도 | 배정 기준 | 애매 구간 | 달성 | 순도 | 배정 정확도 | 신규 탐지 "
            "| 검토 비율 | 유형 발견 | 카테고리 수 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in sweep:
            if not isinstance(row, dict):
                continue
            m = row["metrics"]
            if not isinstance(m, dict):
                continue
            lines.append(
                f"| {row['warmupMonths']}개월 | {row['cohesion']} | {row['threshold']} "
                f"| {row['margin']} | {row['passed']}/5 "
                f"| {fmt(m['purity'])} | {fmt(m['assignmentAccuracy'])} "
                f"| {fmt(m['newDetectionRate'])} | {fmt(m['reviewRatio'])} "
                f"| {fmt(m['typeCoverage'])} | {fmt(m['categoryCount'])} |"
            )
        lines += ["", f"추천: `{json.dumps(data['recommended'], ensure_ascii=False)}`", ""]
        if recommended is not None:
            lines += ["### 추천 기준값의 월별 결과", "", *metric_table(recommended.metrics), ""]
            lines += [*month_table(recommended), ""]

    attrs = data["attributes"]
    lines += ["## 3. 속성 추출", "", "```json", json.dumps(attrs, ensure_ascii=False, indent=2)]
    lines += ["```", ""]
    for title, key in (
        ("4. 아산 실데이터 통계·급증", "asan"),
        ("5. 김천 정형 문구·급증", "kimcheon"),
    ):
        lines += [f"## {title}", "", "```json"]
        lines += [json.dumps(data[key], ensure_ascii=False, indent=2), "```", ""]
    for title, key in (
        ("6. 포항 군집(정답 없음)", "pohang"),
        ("7. 국민신문고 제목 군집", "sinmungo"),
    ):
        lines += [f"## {title}", "", "| 크기 | 기준 | 핵심어 | 대표 민원(마스킹) |"]
        lines.append("| --- | --- | --- | --- |")
        clusters = data[key]
        if isinstance(clusters, list):
            for cluster in clusters:
                if isinstance(cluster, dict):
                    keywords = cluster["keywords"] if isinstance(cluster["keywords"], list) else []
                    exemplars = (
                        cluster["exemplars"] if isinstance(cluster["exemplars"], list) else []
                    )
                    lines.append(
                        f"| {cluster['size']} | {cluster['reason']} "
                        f"| {', '.join(map(str, keywords[:6]))} "
                        f"| {' / '.join(str(e).replace('|', ' ') for e in exemplars)} |"
                    )
        lines.append("")
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    """샘플을 읽어 M2 리포트를 만든다.

    파라미터: 없음(명령줄 인자는 --help 참고)
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", type=Path, default=ROOT / "샘플데이타")
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "m2")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "analysis.toml")
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/datalens/embeddings")
    parser.add_argument("--sweep", action="store_true", help="기준값 탐색")
    parser.add_argument("--llm", action="store_true", help="LLM 속성 추출 비교(외부 전송)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    config = load_analysis_config(args.config)
    started = time.time()

    def log(message: str) -> None:
        """진행 상황을 출력한다."""
        print(f"[{time.time() - started:6.0f}s] {message}", flush=True)

    synthetic = load_synthetic(args.samples)
    pohang = load_pohang(args.samples)
    sinmungo = load_sinmungo(args.samples)
    log(f"샘플: 가상민원 {len(synthetic)} · 포항 {len(pohang)} · 국민신문고 {len(sinmungo)}")

    embedder = CachedEmbedder(SentenceTransformerEmbedder(), args.cache)
    records = [item.record for item in synthetic]
    gold = [item.category for item in synthetic]
    vectors = embedder.embed([r.text for r in records])
    log("가상민원 임베딩 완료")

    base = run_simulation(records, vectors, gold, config, embedding_model=embedder.model_name)
    log(f"기본 시뮬레이션: {base.metrics}")

    data: dict[str, JsonValue] = {
        "generatedAt": date.today().isoformat(),
        "embeddingModel": embedder.model_name,
        "config": config.model_dump(),
        "simulation": base.to_json(),
    }
    recommended: SimulationResult | None = None
    if args.sweep:
        rows: list[JsonValue] = []
        best: tuple[tuple[int, float], dict[str, JsonValue]] | None = None
        grid = list(
            itertools.product(SWEEP_WARMUPS, SWEEP_COHESIONS, SWEEP_THRESHOLDS, SWEEP_MARGINS)
        )
        for number, (warmup, cohesion, threshold, margin) in enumerate(grid, 1):
            trial = config_with(config, threshold, margin, cohesion)
            result = run_simulation(
                records,
                vectors,
                gold,
                trial,
                embedding_model=embedder.model_name,
                warmup_months=warmup,
            )
            rank = score(result.metrics)
            row: dict[str, JsonValue] = {
                "warmupMonths": warmup,
                "cohesion": cohesion,
                "threshold": threshold,
                "margin": margin,
                "passed": rank[0],
                "score": rank[1],
                "metrics": dict(result.metrics),
            }
            rows.append(row)
            if best is None or rank > best[0]:
                best = (rank, row)
            log(f"탐색 {number}/{len(grid)}: {warmup}/{cohesion}/{threshold}/{margin} → {rank}")
        data["sweep"] = rows
        if best is not None:
            recommended = run_simulation(
                records,
                vectors,
                gold,
                config_with(
                    config,
                    float(str(best[1]["threshold"])),
                    float(str(best[1]["margin"])),
                    float(str(best[1]["cohesion"])),
                ),
                embedding_model=embedder.model_name,
                warmup_months=int(str(best[1]["warmupMonths"])),
            )
            data["recommendedSimulation"] = recommended.to_json()
            data["recommended"] = best[1]
            chosen = config_with(
                config,
                float(str(best[1]["threshold"])),
                float(str(best[1]["margin"])),
                float(str(best[1]["cohesion"])),
            )
            note = f"첫 체계(v1)는 {best[1]['warmupMonths']}개월 치를 모아 만든다(설정값 아님)"
            (args.out / "recommended-analysis.toml").write_text(
                toml_text(chosen, note), encoding="utf-8"
            )

    rule_synthetic = {i.record.record_id: extract_by_rules(i.record) for i in synthetic}
    rule_pohang = {r.record_id: extract_by_rules(r) for r in pohang}
    attributes: dict[str, JsonValue] = {
        "rule": {
            "synthetic": compare_with_gold(synthetic, rule_synthetic),
            "syntheticCoverage": coverage(list(rule_synthetic.values())),
            "pohangCoverage": coverage(list(rule_pohang.values())),
        }
    }
    if args.llm:
        llm_attrs, errors, usage = asyncio.run(run_llm([*records, *pohang]))
        by_id = {a.record_id: a for a in llm_attrs}
        llm_synthetic = {k: v for k, v in by_id.items() if not k.startswith("POH")}
        llm_pohang = {k: v for k, v in by_id.items() if k.startswith("POH")}
        attributes["llm"] = {
            "synthetic": compare_with_gold(synthetic, llm_synthetic),
            "syntheticCoverage": coverage(list(llm_synthetic.values())),
            "pohangCoverage": coverage(list(llm_pohang.values())),
            "agreementSynthetic": agreement(rule_synthetic, llm_synthetic),
            "agreementPohang": agreement(rule_pohang, llm_pohang),
            "errors": list[JsonValue](errors[:20]),
            "errorCount": len(errors),
            "usage": usage,
        }
        log(f"LLM 속성 추출: {len(llm_attrs)}건, 오류 {len(errors)}건")
    data["attributes"] = attributes

    data["asan"] = asan_stats(args.samples, config.spike)
    data["kimcheon"] = kimcheon_stats(args.samples, config.spike)
    log("아산·김천 통계 완료")
    data["pohang"] = list[JsonValue](cluster_summary(pohang, embedder, config))
    data["sinmungo"] = list[JsonValue](cluster_summary(sinmungo, embedder, config))
    data["labelingRows"] = labeling_template(pohang, sinmungo, args.out / "labeling-template.csv")
    log("포항·국민신문고 군집, 라벨링 양식 완료")

    (args.out / "results.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_report(args.out, data, base, recommended)
    log(f"완료: {args.out}")


def toml_text(config: AnalysisConfig, note: str) -> str:
    """설정을 config/analysis.toml 형식으로 쓴다(값만, 머리 주석 포함).

    파라미터:
        config: 분석 기준값
        note: 머리 주석에 덧붙일 운영 메모
    """
    lines = [
        "# M2 추천값(scripts/m2_simulation.py --sweep). 검토 후 config/analysis.toml에 반영",
        f"# {note}",
    ]
    for section, values in config.model_dump().items():
        lines += ["", f"[{section}]"]
        for key, value in values.items():
            if value is not None:  # TOML에는 null이 없다. 빠진 값은 기본값을 쓴다
                lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
