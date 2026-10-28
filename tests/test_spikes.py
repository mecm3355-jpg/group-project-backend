from datetime import date

from app.core.analysis_config import SpikeConfig
from app.services.spikes import MonthlyCounts, detect_spikes, month_range, monthly_counts


def series(counts: dict[str, int], partial: set[str] | None = None) -> MonthlyCounts:
    """테스트용 달별 건수.

    파라미터:
        counts: 달 → 건수(빠짐없이)
        partial: 일부 월
    """
    return MonthlyCounts(sorted(counts), counts, partial or set())


def test_month_range_crosses_year() -> None:
    assert month_range("2024-11", "2025-02") == ["2024-11", "2024-12", "2025-01", "2025-02"]


def test_monthly_counts_marks_partial_edges() -> None:
    days = [date(2024, 4, 11), date(2024, 5, 3), date(2024, 7, 1), date(2024, 8, 25)]

    result = monthly_counts(days, min_coverage=0.9)

    assert result.months == ["2024-04", "2024-05", "2024-06", "2024-07", "2024-08"]
    assert result.counts["2024-06"] == 0
    # 4월은 11일부터(20/30일), 8월은 25일까지(25/31일)
    assert result.partial == {"2024-04", "2024-08"}


def test_full_edge_months_are_not_partial() -> None:
    result = monthly_counts([date(2024, 1, 1), date(2024, 2, 29)], min_coverage=0.9)

    assert result.partial == set()


def test_spike_needs_ratio_and_min_count() -> None:
    data = series({"2024-01": 10, "2024-02": 10, "2024-03": 10, "2024-04": 20, "2024-05": 12})

    checks = detect_spikes(data, SpikeConfig(ratio=2.0, baseline_months=3, min_count=5))

    assert [(c.month, c.spike) for c in checks] == [("2024-04", True), ("2024-05", False)]
    assert checks[0].baseline == 10.0
    assert checks[0].ratio == 2.0


def test_small_counts_are_not_spikes() -> None:
    data = series({"2024-01": 1, "2024-02": 1, "2024-03": 1, "2024-04": 4})

    [check] = detect_spikes(data, SpikeConfig(min_count=5))

    assert not check.spike


def test_last_year_filter_removes_seasonal_peak() -> None:
    counts = {m: 10 for m in month_range("2024-01", "2025-07")}
    counts["2024-07"] = 30
    counts["2025-07"] = 32  # 직전 3개월 평균의 3.2배지만 전년 같은 달과 비슷하다

    checks = {c.month: c for c in detect_spikes(series(counts), SpikeConfig(yoy_ratio=1.5))}

    assert checks["2025-07"].basic_spike
    assert not checks["2025-07"].spike
    assert checks["2024-07"].spike  # 전년 자료가 없으면 기본 규칙만 본다


def test_partial_months_are_skipped() -> None:
    data = series(
        {"2024-01": 3, "2024-02": 10, "2024-03": 10, "2024-04": 10, "2024-05": 25},
        partial={"2024-01"},
    )

    checks = detect_spikes(data, SpikeConfig())

    assert [c.month for c in checks] == ["2024-05"]
    assert checks[0].baseline == 10.0
