"""문서형 파일의 민원 단위 분리(PRD 7.6 2단계: 소스별 반복 서식 규칙).

규칙은 "시작 표지 + 필드 표지"다(예: 국민신문고 목록 — 번호로 시작, 제목·처리기관·등록일·조회수).
OCR 줄은 띄어쓰기·구분 기호가 흔들리므로 표지는 띄어쓰기를 빼고 줄 앞부분과 비교한다.
페이지가 바뀌어도 한 건이 이어지면 그대로 붙인다(페이지 경계에서 잘린 건 이어 붙이기).
표가 있는 문서(1단계)와 파일 1개 = 1건(3단계)은 호출하는 쪽에서 고른다.
"""

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.errors import AppError
from app.processors.document import DocLine

DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "split_rules.toml"
# 표지 뒤 값 앞에 OCR이 붙이는 구분 기호(예: "처리 기관 _ 국토교통부", "등록일 : 2026-10-24")
VALUE_PREFIX = re.compile(r"^[\s_:：ㆍ·^|.\-]+")
# 날짜: 연(4~5자리) 구분 월 구분 일. 5자리는 OCR이 구분 기호를 숫자로 읽은 경우,
# 구분 1~2글자는 "2026. 10. 24" 같은 점+공백 표기까지 받기 위함
DATE_PARTS = re.compile(r"(\d{4,5})\D{1,2}(\d{1,2})\D{1,2}(\d{1,2})")


class FormRule(BaseModel):
    """반복 서식 분리 규칙 하나."""

    # 규칙 파일의 오타 키(예: fileds)를 조용히 무시하지 않고 읽을 때 바로 오류로 알린다
    model_config = ConfigDict(extra="forbid")

    start_label: str
    fields: dict[str, str]
    content_field: str
    content_type: Literal["body", "title"] = "body"
    continuation_fields: list[str] = []
    ignore_patterns: list[str] = []
    # 접수일로 쓸 필드(complaints.receivedAt). 없으면 접수일 없음
    received_field: str | None = None
    # 필드 형식: date(YYYY-MM-DD로 맞춤), digits(숫자만 남김). OCR 오인식을 바로잡는다
    field_types: dict[str, Literal["date", "digits"]] = {}


@dataclass
class FormRecord:
    """분리한 민원 한 건: 필드 값, 시작 페이지, 줄 신뢰도(OCR일 때만)."""

    fields: dict[str, str] = field(default_factory=dict)
    page: int | None = None
    confidences: list[float] = field(default_factory=list)

    @property
    def confidence(self) -> float | None:
        """줄 신뢰도 평균(OCR·분리 신뢰도, complaints.extractionConfidence). 텍스트 추출이면 None.

        파라미터: 없음
        """
        return round(sum(self.confidences) / len(self.confidences), 4) if self.confidences else None

    @property
    def min_confidence(self) -> float | None:
        """가장 낮은 줄 신뢰도(검수 판단용).

        파라미터: 없음
        """
        return min(self.confidences) if self.confidences else None


def load_split_rules(path: Path = DEFAULT_RULES_PATH) -> dict[str, FormRule]:
    """분리 규칙 파일을 읽는다. 형식이 틀리면 AppError(INVALID_SPLIT_RULES).

    파라미터:
        path: TOML 파일(기본: config/split_rules.toml)
    """
    # 파일 없음·문법 오류·규칙 형식 오류(pydantic ValidationError는 ValueError)를 한 코드로
    # 묶는다. 사용자 입력이 아니라 서버 설정 문제이므로 500
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return {name: FormRule.model_validate(rule) for name, rule in data.items()}
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise AppError(
            "INVALID_SPLIT_RULES", f"민원 분리 규칙 파일을 읽을 수 없습니다: {exc}", 500
        ) from exc


def normalize_field(value: str, kind: Literal["date", "digits"]) -> str:
    """OCR로 읽은 필드 값을 형식에 맞춘다. 맞출 수 없으면 원래 값을 둔다.

    - digits: 숫자만 남긴다("4051.3" → "40513", OCR이 숫자 사이에 끼운 기호 제거)
    - date: 연·월·일 숫자를 찾아 YYYY-MM-DD로. 연도가 다섯 자리면 앞 네 자리를 쓴다
      ("20264-10-24": 하이픈을 숫자로 읽은 경우 → 2026-10-24)

    파라미터:
        value: 필드 값
        kind: date 또는 digits
    """
    if kind == "digits":
        digits = re.sub(r"\D", "", value)
        # 숫자가 하나도 없으면 비우지 않고 원래 값을 둬 검수 때 무엇을 읽었는지 보이게 한다
        return digits or value
    # OCR이 날짜 사이에 넣은 공백을 먼저 지워 "2026 - 10 - 24"도 같은 패턴으로 잡는다
    match = DATE_PARTS.search(value.replace(" ", ""))
    if not match:
        return value
    year, month, day = match.group(1)[:4], int(match.group(2)), int(match.group(3))
    # 범위를 벗어나면 잘못 읽은 것이므로 억지로 고치지 않는다(틀린 날짜보다 원문이 낫다)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return value
    return f"{year}-{month:02d}-{day:02d}"


def _strip_label(text: str, label: str) -> str | None:
    """줄이 표지로 시작하면(띄어쓰기 무시) 표지 뒤 값을, 아니면 None.

    파라미터:
        text: 줄
        label: 표지(띄어쓰기 없이 비교)
    """
    target = label.replace(" ", "")
    matched = 0
    # 줄 글자를 한 글자씩 보며 공백은 건너뛰고 표지 글자와 맞춘다. 정규식으로 표지마다
    # 글자 사이 \s*를 끼우는 것보다 단순하고, 값이 시작하는 원래 위치를 그대로 알 수 있다
    for index, char in enumerate(text):
        if char.isspace():
            continue
        # 표지를 다 맞춘 뒤 첫 글자부터가 값이다. 앞에 붙은 구분 기호는 떼어 낸다
        if matched == len(target):
            return VALUE_PREFIX.sub("", text[index:]).strip()
        if char != target[matched]:
            return None
        matched += 1
    # 줄이 표지로 끝나면 값이 빈 필드다(다음 줄이 이어 붙을 수 있음)
    return "" if matched == len(target) else None


def split_by_form(lines: Iterable[DocLine], rule: FormRule) -> list[FormRecord]:
    """문서 줄을 규칙대로 민원 단위로 나눈다. 첫 시작 표지 앞의 줄(머리말)은 버린다.

    ignore_patterns에 맞는 줄(머리말·바닥글·쪽번호)은 버린다. 표지 없는 줄은 직전 필드가
    continuation_fields일 때만 이어 붙이고, 아니면 버린다.

    파라미터:
        lines: open_document의 entries
        rule: 분리 규칙
    """
    # 긴 표지부터 비교한다. "처리기관"과 "처리기관 담당자"처럼 앞부분이 겹치면 짧은 표지가
    # 먼저 맞아 값이 잘못 잘리기 때문이다
    labels = sorted(rule.fields.items(), key=lambda item: -len(item[1]))
    ignored = [re.compile(pattern) for pattern in rule.ignore_patterns]
    # 시작 표지에 해당하는 필드 키. 이 필드를 만나면 새 민원 한 건이 시작된다
    start_key = next((key for key, label in labels if label == rule.start_label), None)
    records: list[FormRecord] = []
    current: FormRecord | None = None
    # 직전에 값을 넣은 필드. 표지 없는 줄을 어디에 이어 붙일지 정하는 데 쓴다
    last_key: str | None = None
    for line in lines:
        if any(pattern.search(line.text) for pattern in ignored):
            last_key = None  # 머리말·바닥글 뒤 줄은 앞 필드에 이어 붙이지 않는다
            continue
        matched: tuple[str, str] | None = None
        for key, label in labels:
            value = _strip_label(line.text, label)
            if value is not None:
                matched = (key, value)
                break
        if matched is None:
            # 표지 없는 줄: 본문처럼 여러 줄로 이어지는 필드일 때만 붙인다. 페이지가 바뀌어도
            # current를 유지하므로 페이지 경계에서 잘린 건이 자연스럽게 이어진다
            if current is not None and last_key in rule.continuation_fields:
                current.fields[last_key] = f"{current.fields[last_key]} {line.text.strip()}".strip()
                if line.confidence is not None:
                    current.confidences.append(line.confidence)
            else:
                last_key = None  # 머리말·바닥글 뒤의 줄은 이어 붙이지 않는다
            continue
        key, value = matched
        # 시작 표지를 만나면 새 건을 연다. OCR이 시작 표지를 놓쳐 첫 건이 다른 필드로
        # 시작해도 버리지 않도록, 열린 건이 없으면 그 줄에서 연다
        if key == start_key or current is None:
            current = FormRecord(page=line.page)
            records.append(current)
        current.fields[key] = value
        if line.confidence is not None:
            current.confidences.append(line.confidence)
        last_key = key
    # 형식 맞춤은 이어 붙이기가 끝난 뒤에 한다(줄이 나뉜 날짜·번호도 합친 값으로 맞추려고)
    for record in records:
        for key, kind in rule.field_types.items():
            if key in record.fields:
                record.fields[key] = normalize_field(record.fields[key], kind)
    # 내용 필드가 비면 분석할 글이 없으므로 민원으로 내보내지 않는다(목차·빈 서식 등)
    return [r for r in records if r.fields.get(rule.content_field)]
