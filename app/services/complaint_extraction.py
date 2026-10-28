"""원본 파일에서 민원 단위 레코드를 뽑는다(PRD 4장 1단계 입력·정규화, 7.6 민원 단위 나누기).

- 표(CSV·XLSX 등): 1행 = 1건. 열 매핑(FieldMapping)으로 본문·접수일·지역·좌표·기존 분류를 읽는다
- 문서·스캔(PDF·DOCX·이미지): 분리 규칙이 있으면 반복 서식으로, 없으면 파일 1개 = 1건
- TXT: 빈 줄로 나눈다
빈 본문과 완전 중복은 빼고 품질 문제(quality.issues)로 알린다. 마스킹은 다음 단계에서 한다.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from app.core.errors import AppError
from app.processors.document import DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS, OcrOptions, open_document
from app.processors.splitter import FormRule, split_by_form
from app.processors.tabular import TABULAR_EXTENSIONS, Cell, open_table
from app.processors.text import TEXT_EXTENSIONS, open_text
from app.schemas.complaint import ContentType
from app.schemas.complaint_api import ComplaintAnalysisRequest
from app.schemas.result import QualityIssue
from app.services.profiling import parse_datetime, parse_number

# 좌표로 인정할 범위(대한민국 부근). 벗어나면 좌표를 버리고 품질 문제로 알린다
LATITUDE_RANGE = (33.0, 39.0)
LONGITUDE_RANGE = (124.0, 132.0)


@dataclass
class RawComplaint:
    """마스킹 전 민원 한 건과 원문 위치."""

    text: str
    content_type: ContentType
    locator: dict[str, int]
    confidence: float | None = None
    received_at: datetime | None = None
    region: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    source_category: str | None = None


@dataclass
class Extraction:
    """추출 결과: 민원 목록, 품질 문제, 추출 정보(processing_results.analysis에 들어갈 값)."""

    records: list[RawComplaint] = field(default_factory=list)
    issues: list[QualityIssue] = field(default_factory=list)
    info: dict[str, str | int | None] = field(default_factory=dict)


def _text(value: Cell) -> str:
    """칸 값을 앞뒤 공백 없는 문자열로.

    파라미터:
        value: 칸 값
    """
    return "" if value is None else str(value).strip()


def _date(value: Cell | str) -> datetime | None:
    """칸 값을 날짜로(날짜가 아니면 None). 시간대가 없으면 적힌 값 그대로 UTC로 둔다.

    원본 접수일에는 시간대가 없고, MongoDB도 시간대 없는 값을 UTC로 저장하므로 DB에서 읽은
    날짜(시간대 있음)와 비교할 수 있게 맞춘다.

    파라미터:
        value: 칸 값 또는 문자열
    """
    if isinstance(value, datetime):
        parsed: datetime | None = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        parsed = parse_datetime(_text(value)) if value is not None else None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _number(value: Cell) -> float | None:
    """칸 값을 숫자로(숫자가 아니면 None).

    파라미터:
        value: 칸 값
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return parse_number(_text(value)) if value is not None else None


def _issue(
    kind: str, severity: str, count: int, location: str | None, message: str
) -> QualityIssue:
    """품질 문제 하나(기준 형식).

    파라미터:
        kind: 문제 종류
        severity: low, medium, high
        count: 건수
        location: 위치(열 이름 등)
        message: 설명
    """
    return QualityIssue.model_validate(
        {
            "type": kind,
            "severity": severity,
            "count": count,
            "location": location,
            "message": message,
        }
    )


def _from_table(path: Path, extension: str, request: ComplaintAnalysisRequest) -> Extraction:
    """표 파일을 1행 = 1건으로 읽는다.

    파라미터:
        path: 원본 경로
        extension: 확장자
        request: 열 매핑·고정값
    """
    mapping = request.mapping.mapped()
    # 분석할 글이 없으면 임베딩·군집화를 할 수 없다. 본문 열이 없는 자료(예: 제목만 있는 단속
    # 기록)는 제목을 본문 대신 쓴다
    body_column = mapping.get("content") or mapping.get("title")
    if body_column is None:
        raise AppError(
            "MAPPING_REQUIRED",
            "표 파일은 본문(content) 또는 제목(title) 열 매핑이 필요합니다.",
            422,
        )
    result = Extraction()
    with open_table(path, extension) as table:
        # 매핑 오타는 행마다 None으로 조용히 넘어가면 원인을 찾기 어려우므로 처음에 거부한다
        missing = [c for c in mapping.values() if c not in table.columns]
        if missing:
            raise AppError("MAPPING_INVALID", f"파일에 없는 열입니다: {', '.join(missing)}", 422)
        # 열 이름 → 칸 위치를 한 번만 계산해 행마다 찾지 않게 한다
        index = {name: table.columns.index(column) for name, column in mapping.items()}
        # 본문 열이 없어 제목을 대신 읽었는지 민원마다 기록해 둔다(contentType)
        content_type: ContentType = "body" if "content" in mapping else "title"
        # 1행은 머리글이라 데이터는 2행부터다. 원본 파일의 행 번호와 맞춰 근거를 찾아가게 한다
        # (PRD 10장 추적성)
        for row_number, row in enumerate(table.rows, start=2):
            if not row:
                continue

            # row를 기본값으로 묶어 두어, 반복마다 그 행의 값을 읽게 한다(늦은 바인딩 방지)
            def cell(name: str, row: list[Cell] = row) -> Cell:
                """매핑된 열의 값(매핑이 없으면 None).

                파라미터:
                    name: 매핑 항목 이름(content, title, received_at, region 등)
                    row: 읽을 행(기본값으로 현재 행이 묶인다)
                """
                return row[index[name]] if name in index else None

            # 지역·기존 분류 열이 없거나 비어 있으면 요청의 고정값(예: 파일 전체가 한 지역)을 쓴다
            result.records.append(
                RawComplaint(
                    text=_text(cell("content" if content_type == "body" else "title")),
                    content_type=content_type,
                    locator={"row": row_number},
                    received_at=_date(cell("received_at")),
                    region=_text(cell("region")) or request.constants.region,
                    latitude=_number(cell("latitude")),
                    longitude=_number(cell("longitude")),
                    source_category=_text(cell("source_category"))
                    or request.constants.source_category,
                )
            )
        result.info = {"format": table.format, "encoding": table.encoding, "sheet": table.sheet}
    result.info["rowCount"] = len(result.records)
    return result


def _from_document(
    path: Path,
    extension: str,
    request: ComplaintAnalysisRequest,
    ocr: OcrOptions | None,
    rules: dict[str, FormRule],
) -> Extraction:
    """문서·스캔 파일을 분리 규칙(있으면)으로 나누거나 파일 하나를 한 건으로 읽는다.

    파라미터:
        path: 원본 경로
        extension: 확장자
        request: 분리 규칙 이름
        ocr: OCR 설정
        rules: 분리 규칙 목록
    """
    # 요청에 분리 규칙 이름이 있으면 설정 파일에 있는지 먼저 확인한다. 없는 이름으로 파일 전체를
    # 한 건으로 처리해 버리면 결과가 크게 틀어지므로 오류로 알린다
    rule: FormRule | None = None
    if request.split_rule is not None:
        rule = rules.get(request.split_rule)
        if rule is None:
            raise AppError(
                "SPLIT_RULE_NOT_FOUND", f"분리 규칙이 없습니다: {request.split_rule}", 422
            )
    result = Extraction()
    with open_document(path, extension, ocr) as document:
        # 한 파일에 같은 서식의 민원이 반복되면(예: 국민신문고 PDF) 분리 규칙으로 한 건씩 나눈다.
        # 페이지·OCR 신뢰도를 함께 남겨 사람 검수 때 원문 위치로 돌아갈 수 있게 한다
        if rule is not None:
            for form in split_by_form(document.entries, rule):
                received = form.fields.get(rule.received_field) if rule.received_field else None
                result.records.append(
                    RawComplaint(
                        text=form.fields[rule.content_field],
                        content_type=rule.content_type,
                        locator={"page": form.page} if form.page is not None else {},
                        confidence=form.confidence,
                        received_at=_date(received) if received else None,
                        region=request.constants.region,
                        source_category=request.constants.source_category,
                    )
                )
        else:
            # 규칙이 없으면 파일 하나를 민원 한 건으로 본다. OCR 신뢰도는 OCR한 조각만 평균 낸다
            # (텍스트 레이어에서 읽은 조각은 신뢰도가 없다)
            entries = list(document.entries)
            confidences = [e.confidence for e in entries if e.confidence is not None]
            result.records.append(
                RawComplaint(
                    text="\n".join(e.text for e in entries),
                    content_type="body",
                    locator={"page": 1} if document.page_count else {"paragraph": 0},
                    confidence=round(sum(confidences) / len(confidences), 4)
                    if confidences
                    else None,
                    region=request.constants.region,
                    source_category=request.constants.source_category,
                )
            )
        result.info = {
            "format": document.format,
            "pageCount": document.page_count,
            "method": "ocr" if document.ocr_pages else "text",
        }
        # OCR을 못 쓰는 환경에서 스캔 페이지는 빈 글로 빠진다. 조용히 넘기지 않고 높은 심각도로
        # 알려 담당자가 OCR을 켜고 다시 처리하게 한다
        if document.pages_without_text:
            result.issues.append(
                _issue(
                    "no_text_layer",
                    "high",
                    document.pages_without_text,
                    None,
                    f"글자를 못 읽은 페이지 {document.pages_without_text}개(OCR 필요).",
                )
            )
    return result


def _from_text(path: Path, extension: str, request: ComplaintAnalysisRequest) -> Extraction:
    """텍스트 파일을 빈 줄로 나눠 읽는다(PRD 7.6 TXT).

    파라미터:
        path: 원본 경로
        extension: 확장자
        request: 고정값
    """
    result = Extraction()
    with open_text(path, extension) as text:
        block: list[str] = []
        start = 1
        # 끝에 빈 줄 하나를 덧붙여, 파일이 빈 줄 없이 끝나도 마지막 덩어리가 저장되게 한다
        for number, line in enumerate([*text.lines, ""], start=1):
            if line.strip():
                # 덩어리의 첫 줄 번호를 원문 위치로 남긴다
                if not block:
                    start = number
                block.append(line.strip())
                continue
            # 빈 줄을 만나면 모아 둔 줄을 한 건으로 닫는다. 연속된 빈 줄은 건너뛴다
            if block:
                result.records.append(
                    RawComplaint(
                        text=" ".join(block),
                        content_type="body",
                        locator={"line": start},
                        region=request.constants.region,
                        source_category=request.constants.source_category,
                    )
                )
                block = []
        result.info = {"format": text.format, "encoding": text.encoding}
    return result


def _in_range(value: float | None, bounds: tuple[float, float]) -> bool:
    """값이 범위 안에 있는지.

    파라미터:
        value: 값
        bounds: (최소, 최대)
    """
    return value is not None and bounds[0] <= value <= bounds[1]


def _clean(result: Extraction) -> Extraction:
    """빈 본문·완전 중복을 빼고, 범위를 벗어난 좌표를 지운 뒤 각각 품질 문제로 알린다.

    파라미터:
        result: 형식별 추출 결과
    """
    # 빈 본문은 임베딩·속성 근거가 없어 분석에서 뺀다(PRD 4장 1단계: 오류 리포트로 빼고 계속)
    empty = sum(1 for r in result.records if not r.text.strip())
    seen: Counter[tuple[str, datetime | None, str | None]] = Counter()
    kept: list[RawComplaint] = []
    duplicates = bad_coordinates = 0
    for record in result.records:
        if not record.text.strip():
            continue
        # 완전 중복: 공백만 다른 본문 + 같은 접수일 + 같은 지역. 본문만 같은 서로 다른 민원
        # (다른 날 같은 내용 재신고 등)은 지우지 않도록 접수일·지역까지 키에 넣는다
        key = (" ".join(record.text.split()), record.received_at, record.region)
        seen[key] += 1
        if seen[key] > 1:
            duplicates += 1
            continue
        # 위도·경도 중 하나라도 범위를 벗어나면 둘 다 버린다. 반쪽 좌표는 거리 계산에 못 쓰고,
        # 흔한 원인은 위도·경도 열을 뒤바꿔 매핑한 경우다
        has_point = record.latitude is not None or record.longitude is not None
        if has_point and not (
            _in_range(record.latitude, LATITUDE_RANGE)
            and _in_range(record.longitude, LONGITUDE_RANGE)
        ):
            bad_coordinates += 1
            record.latitude = record.longitude = None
        kept.append(record)
    if empty:
        result.issues.append(
            _issue("empty_content", "medium", empty, None, f"본문이 빈 민원 {empty}건을 뺐습니다.")
        )
    if duplicates:
        result.issues.append(
            _issue(
                "duplicates",
                "low",
                duplicates,
                None,
                f"완전히 같은 민원 {duplicates}건을 뺐습니다.",
            )
        )
    if bad_coordinates:
        result.issues.append(
            _issue(
                "out_of_range",
                "low",
                bad_coordinates,
                "latitude, longitude",
                f"위도·경도 범위를 벗어난 좌표 {bad_coordinates}건은 좌표 없이 처리했습니다"
                "(열 이름이 뒤바뀌었는지 매핑을 확인하세요).",
            )
        )
    result.records = kept
    return result


def extract_complaints(
    path: Path,
    extension: str,
    request: ComplaintAnalysisRequest,
    ocr: OcrOptions | None,
    rules: dict[str, FormRule],
) -> Extraction:
    """파일 형식에 맞게 민원 레코드를 뽑는다. 시간이 걸리므로 스레드에서 실행한다.

    파라미터:
        path: 원본 경로
        extension: 확장자
        request: 분석 요청(매핑·고정값·분리 규칙)
        ocr: OCR 설정(스캔 PDF·이미지)
        rules: 분리 규칙 목록(config/split_rules.toml)
    """
    # 형식별 추출기로 나눈 뒤, 공통 정리(_clean)는 한 곳에서 해 형식마다 기준이 달라지지 않게 한다
    if extension in TABULAR_EXTENSIONS:
        result = _from_table(path, extension, request)
    elif extension in DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS:
        result = _from_document(path, extension, request, ocr, rules)
    elif extension in TEXT_EXTENSIONS:
        result = _from_text(path, extension, request)
    else:
        raise AppError(
            "ANALYSIS_NOT_SUPPORTED", f"{extension} 파일은 민원 분석을 지원하지 않습니다.", 422
        )
    return _clean(result)
