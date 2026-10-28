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
    # YYYY-MM은 자릿수가 고정이라 문자열 비교가 곧 날짜 비교다
    while f"{year:04d}-{month:02d}" <= last:
        months.append(f"{year:04d}-{month:02d}")
        # 12월 다음은 다음 해 1월
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
    # 중간에 민원이 0건인 달도 시계열에 넣어야 직전 N개월 평균이 실제보다 높게 잡히지 않는다
    months = month_range(month_key(day_list[0]), month_key(day_list[-1]))
    # 자료가 달 중간에 시작·끝나면 그 달 건수가 적게 잡혀, 기준선을 끌어내리거나(다음 달 급증
    # 오탐) 그 달 자체가 급감처럼 보인다. 첫 접수일~월말, 월초~마지막 접수일이 그 달 일수의
    # min_coverage(초기 0.9)에 못 미치면 일부 월로 표시해 판정에서 뺀다
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
        # 기준선 달 수(초기 3개월)가 다 차지 않은 앞쪽 달은 비교 대상이 없어 판정하지 않는다
        if index < config.baseline_months:
            continue
        previous = full[index - config.baseline_months : index]
        baseline = sum(series.counts[m] for m in previous) / config.baseline_months
        count = series.counts[month]
        # 기준선이 0이면 비율을 낼 수 없다. 그때는 0건에서 min_count건 이상으로 늘어난 것 자체를
        # 급증으로 본다
        ratio = count / baseline if baseline else None
        # min_count(초기 5건)는 1→2건처럼 작은 수의 2배를 급증으로 잡지 않기 위한 하한(PRD 8.2)
        basic = count >= config.min_count and (
            baseline == 0 if ratio is None else ratio >= config.ratio
        )
        # 전년 같은 달은 자료에 있고 일부 월이 아닐 때만 쓴다. 없으면 기본 규칙만으로 판정한다
        last_year_month = same_month_last_year(month)
        last_year = (
            series.counts[last_year_month]
            if last_year_month in series.counts and last_year_month not in series.partial
            else None
        )
        # 매년 같은 달에 오르는 계절 정점(예: 아산 7월)은 직전 3개월보다 크게 늘어도 전년 같은
        # 달과 비슷하다. 전년 대비 yoy_ratio(초기 1.5배)도 넘어야 최종 급증으로 남긴다(PRD 9장)
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
