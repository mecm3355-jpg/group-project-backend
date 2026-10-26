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


class FormRule(BaseModel):
    """반복 서식 분리 규칙 하나."""

    model_config = ConfigDict(extra="forbid")

    start_label: str
    fields: dict[str, str]
    content_field: str
    content_type: Literal["body", "title"] = "body"
    continuation_fields: list[str] = []
    ignore_patterns: list[str] = []
    # 접수일로 쓸 필드(complaints.receivedAt). 없으면 접수일 없음
    received_field: str | None = None


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
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return {name: FormRule.model_validate(rule) for name, rule in data.items()}
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise AppError(
            "INVALID_SPLIT_RULES", f"민원 분리 규칙 파일을 읽을 수 없습니다: {exc}", 500
        ) from exc


def _strip_label(text: str, label: str) -> str | None:
    """줄이 표지로 시작하면(띄어쓰기 무시) 표지 뒤 값을, 아니면 None.

    파라미터:
        text: 줄
        label: 표지(띄어쓰기 없이 비교)
    """
    target = label.replace(" ", "")
    matched = 0
    for index, char in enumerate(text):
        if char.isspace():
            continue
        if matched == len(target):
            return VALUE_PREFIX.sub("", text[index:]).strip()
        if char != target[matched]:
            return None
        matched += 1
    return "" if matched == len(target) else None


def split_by_form(lines: Iterable[DocLine], rule: FormRule) -> list[FormRecord]:
    """문서 줄을 규칙대로 민원 단위로 나눈다. 첫 시작 표지 앞의 줄(머리말)은 버린다.

    ignore_patterns에 맞는 줄(머리말·바닥글·쪽번호)은 버린다. 표지 없는 줄은 직전 필드가
    continuation_fields일 때만 이어 붙이고, 아니면 버린다.

    파라미터:
        lines: open_document의 entries
        rule: 분리 규칙
    """
    labels = sorted(rule.fields.items(), key=lambda item: -len(item[1]))
    ignored = [re.compile(pattern) for pattern in rule.ignore_patterns]
    start_key = next((key for key, label in labels if label == rule.start_label), None)
    records: list[FormRecord] = []
    current: FormRecord | None = None
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
            if current is not None and last_key in rule.continuation_fields:
                current.fields[last_key] = f"{current.fields[last_key]} {line.text.strip()}".strip()
                if line.confidence is not None:
                    current.confidences.append(line.confidence)
            else:
                last_key = None  # 머리말·바닥글 뒤의 줄은 이어 붙이지 않는다
            continue
        key, value = matched
        if key == start_key or current is None:
            current = FormRecord(page=line.page)
            records.append(current)
        current.fields[key] = value
        if line.confidence is not None:
            current.confidences.append(line.confidence)
        last_key = key
    return [r for r in records if r.fields.get(rule.content_field)]
