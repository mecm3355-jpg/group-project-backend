"""국민신문고 스캔 PDF OCR 정확도 측정(PRD 9장 추출·분리, M3).

실행: python scripts/ocr_eval.py [--samples 샘플데이타] [--out reports/m3]
- 국민신문고*.pdf를 로컬 Tesseract로 OCR하고 split_rules.toml의 sinmungo 규칙으로 민원을 나눈다.
- 사람이 교정한 국민신문고_OCR_민원제목.csv(500건)와 파일별 순서대로 맞춰 본다.
- 지표: 분리 정확도(건수·순서 일치), 제목 교정 필요 비율(목표 10% 이하), 글자 오류율(CER),
  처리기관·등록일 일치율, 신뢰도 낮은 건 중 실제 오류 비율(검수 기준값 점검)
산출물(reports/, git 제외): ocr-eval.json, ocr-errors.csv(틀린 제목 목록)
"""

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.processors.document import open_document  # noqa: E402
from app.processors.splitter import FormRecord, load_split_rules, split_by_form  # noqa: E402
from app.services.ocr_setup import ocr_options  # noqa: E402

# 신뢰도 기준값 후보(이 값 미만이면 검수)
THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.85, 0.9]


def compact(text: str) -> str:
    """비교용으로 띄어쓰기를 지운다.

    파라미터:
        text: 글자
    """
    return "".join(text.split())


def letters(text: str) -> str:
    """문장부호·기호를 뺀 글자(한글·영문·숫자)만 남긴다. 내용 오류만 볼 때 쓴다.

    파라미터:
        text: 글자
    """
    return "".join(c for c in text if c.isalnum())


def edit_distance(a: str, b: str) -> int:
    """두 문자열의 편집 거리(레벤슈타인).

    파라미터:
        a: 문자열
        b: 문자열
    """
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def align(truth: list[dict[str, str]], found: list[FormRecord]) -> list[tuple[int, int]]:
    """교정본과 OCR 결과를 제목 유사도로 순서를 지키며 맞춘다(빠지거나 더 생긴 건은 건너뛴다).

    파라미터:
        truth: 한 파일의 교정본 행(순서대로)
        found: 한 파일의 OCR 분리 결과(순서대로)
    """
    matcher = SequenceMatcher(
        a=[compact(t["제목"])[:6] for t in truth],
        b=[compact(f.fields.get("title", ""))[:6] for f in found],
        autojunk=False,
    )
    pairs: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal", "replace"):
            pairs.extend(zip(range(i1, i2), range(j1, j2), strict=False))
    return pairs


def main() -> None:
    """국민신문고 PDF를 OCR해 교정본과 비교한다.

    파라미터: 없음(명령줄 인자는 --help 참고)
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", type=Path, default=ROOT / "샘플데이타")
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "m3")
    parser.add_argument("--languages", help="OCR 언어 모델(기본: OCR_LANGUAGES 설정, 예: kor+eng)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    if args.languages:
        settings = settings.model_copy(update={"ocr_languages": args.languages})
    options = ocr_options(settings)
    if options is None:
        raise SystemExit("Tesseract 또는 한국어 모델이 없습니다(TESSERACT_* 설정 확인).")
    rule = load_split_rules()["sinmungo"]

    truth_by_file: dict[str, list[dict[str, str]]] = defaultdict(list)
    with (args.samples / "국민신문고_OCR_민원제목.csv").open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            truth_by_file[row["원본파일"]].append(row)

    started = time.time()
    totals: dict[str, float] = defaultdict(float)
    confidence_rows: list[tuple[float, bool]] = []
    errors: list[list[str]] = []
    per_file: dict[str, dict[str, int]] = {}
    for pdf in sorted(args.samples.glob("국민신문고*.pdf")):
        with open_document(pdf, "pdf", options) as document:
            found = split_by_form(document.entries, rule)
        # 교정본은 번호 오름차순이고, PDF 목록은 1쪽부터 최신(번호가 큰) 순이다
        truth = sorted(
            truth_by_file.get(pdf.name, []), key=lambda t: (int(t["페이지"]), -int(t["번호"]))
        )
        pairs = align(truth, found)
        per_file[pdf.name] = {"truth": len(truth), "found": len(found), "matched": len(pairs)}
        totals["truth"] += len(truth)
        totals["found"] += len(found)
        totals["matched"] += len(pairs)
        totals["pages"] += document.page_count or 0
        for ti, fi in pairs:
            t, r = truth[ti], found[fi]
            title_ok = compact(r.fields.get("title", "")) == compact(t["제목"])
            totals["titleExact"] += title_ok
            totals["titleLettersExact"] += letters(r.fields.get("title", "")) == letters(t["제목"])
            totals["chars"] += len(compact(t["제목"]))
            totals["charErrors"] += edit_distance(
                compact(r.fields.get("title", "")), compact(t["제목"])
            )
            totals["agencyExact"] += compact(r.fields.get("agency", "")) == compact(t["처리기관"])
            totals["dateExact"] += compact(r.fields.get("registered", "")) == t["등록일"]
            totals["numberExact"] += compact(r.fields.get("number", "")) == t["번호"]
            if r.min_confidence is not None:
                confidence_rows.append((r.min_confidence, title_ok))
            if not title_ok:
                errors.append([pdf.name, str(r.page), t["제목"], r.fields.get("title", "")])
        print(f"[{time.time() - started:5.0f}s] {pdf.name}: {per_file[pdf.name]}", flush=True)

    matched = totals["matched"] or 1
    review: dict[str, dict[str, float]] = {}
    for threshold in THRESHOLDS:
        flagged = [ok for conf, ok in confidence_rows if conf < threshold]
        missed = [ok for conf, ok in confidence_rows if conf >= threshold and not ok]
        review[str(threshold)] = {
            "flaggedRate": round(len(flagged) / matched, 4),
            # 검수 대상 중 실제로 틀린 비율, 검수에서 빠진 오류 수
            "errorRateInFlagged": round(flagged.count(False) / len(flagged), 4) if flagged else 0,
            "missedErrors": len(missed),
        }
    result = {
        "engine": options.engine.name,
        "pages": totals["pages"],
        "seconds": round(time.time() - started, 1),
        "truthRecords": totals["truth"],
        "foundRecords": totals["found"],
        "splitRecall": round(totals["matched"] / (totals["truth"] or 1), 4),
        "titleExact": round(totals["titleExact"] / matched, 4),
        "titleNeedsFix": round(1 - totals["titleExact"] / matched, 4),
        "titleCer": round(totals["charErrors"] / (totals["chars"] or 1), 4),
        # 문장부호를 뺀 내용 글자만 비교했을 때 고쳐야 하는 비율
        "titleLettersNeedsFix": round(1 - totals["titleLettersExact"] / matched, 4),
        "agencyExact": round(totals["agencyExact"] / matched, 4),
        "dateExact": round(totals["dateExact"] / matched, 4),
        "numberExact": round(totals["numberExact"] / matched, 4),
        "reviewThresholds": review,
        "files": per_file,
    }
    suffix = f"-{options.engine.name.split(':')[1]}"
    (args.out / f"ocr-eval{suffix}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (args.out / f"ocr-errors{suffix}.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["원본파일", "페이지", "교정본 제목", "OCR 제목"])
        writer.writerows(errors)
    print(json.dumps({k: v for k, v in result.items() if k != "files"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
