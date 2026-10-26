"""월별 집계와 급증 판정(PRD 8.2 급증 카테고리, 9장 통계·급증).

기본 규칙: 이번 달 건수 ≥ 직전 N개월 평균 × ratio, 그리고 min_count건 이상.
계절 정점(예: 아산 7월) 오탐을 줄이려고, 전년 같은 달이 있으면 그 달 × yoy_ratio 이상일 때만
급증으로 본다. 자료의 처음·마지막 달이 일부만 들어 있으면 판정과 기준선에서 뺀다.
"""

import calendar
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from app.core.analysis_config import SpikeConfig


def month_key(day: date) -> str:
    """날짜의 접수월(YYYY-MM).

    파라미터:
        day: 날짜
    """
    return f"{day.year:04d}-{day.month:02d}"


def month_range(first: str, last: str) -> list[str]:
    """first부터 last까지 빠짐없는 달 목록(건수가 0인 달도 넣기 위해).

    파라미터:
        first: 시작 월(YYYY-MM)
        last: 끝 월(YYYY-MM)
    """
    months: list[str] = []
    year, month = int(first[:4]), int(first[5:7])
    while f"{year:04d}-{month:02d}" <= last:
        months.append(f"{year:04d}-{month:02d}")
        year, month = year + month // 12, month % 12 + 1
    return months


@dataclass(frozen=True)
class MonthlyCounts:
    """빠짐없는 달별 건수와 일부만 들어 있는 달."""

    months: list[str]
    counts: dict[str, int]
    partial: set[str]


def monthly_counts(days: Iterable[date], min_coverage: float) -> MonthlyCounts:
    """날짜 목록을 달별로 센다. 처음·마지막 달이 그 달의 min_coverage 미만만 덮으면 일부 월.

    파라미터:
        days: 접수일 목록
        min_coverage: 일부 월 판정 비율(0~1)
    """
    day_list = sorted(days)
    if not day_list:
        return MonthlyCounts([], {}, set())
    counts = Counter(month_key(day) for day in day_list)
    months = month_range(month_key(day_list[0]), month_key(day_list[-1]))
    first, last = day_list[0], day_list[-1]
    first_days = calendar.monthrange(first.year, first.month)[1]
    last_days = calendar.monthrange(last.year, last.month)[1]
    partial: set[str] = set()
    if (first_days - first.day + 1) / first_days < min_coverage:
        partial.add(month_key(first))
    if last.day / last_days < min_coverage:
        partial.add(month_key(last))
    return MonthlyCounts(months, {m: counts.get(m, 0) for m in months}, partial)


def same_month_last_year(month: str) -> str:
    """전년 같은 달(YYYY-MM).

    파라미터:
        month: YYYY-MM
    """
    return f"{int(month[:4]) - 1:04d}{month[4:]}"


@dataclass(frozen=True)
class SpikeCheck:
    """한 달의 급증 판정 결과."""

    month: str
    count: int
    baseline: float
    ratio: float | None
    last_year: int | None
    # 기본 규칙(직전 평균 대비)만 본 판정, 전년 같은 달 비교까지 거친 최종 판정
    basic_spike: bool
    spike: bool


def detect_spikes(series: MonthlyCounts, config: SpikeConfig) -> list[SpikeCheck]:
    """기준선(직전 N개월, 일부 월 제외)이 모두 있는 달마다 급증 여부를 판정한다.

    파라미터:
        series: 달별 건수
        config: 급증 기준값
    """
    full = [m for m in series.months if m not in series.partial]
    checks: list[SpikeCheck] = []
    for index, month in enumerate(full):
        if index < config.baseline_months:
            continue
        previous = full[index - config.baseline_months : index]
        baseline = sum(series.counts[m] for m in previous) / config.baseline_months
        count = series.counts[month]
        ratio = count / baseline if baseline else None
        basic = count >= config.min_count and (
            baseline == 0 if ratio is None else ratio >= config.ratio
        )
        last_year_month = same_month_last_year(month)
        last_year = (
            series.counts[last_year_month]
            if last_year_month in series.counts and last_year_month not in series.partial
            else None
        )
        spike = basic
        if basic and config.yoy_ratio is not None and last_year is not None:
            spike = count >= last_year * config.yoy_ratio
        checks.append(
            SpikeCheck(
                month=month,
                count=count,
                baseline=round(baseline, 1),
                ratio=round(ratio, 2) if ratio is not None else None,
                last_year=last_year,
                basic_spike=basic,
                spike=spike,
            )
        )
    return checks
