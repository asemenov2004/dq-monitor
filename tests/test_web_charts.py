"""Шкала графика сохраняет длительность интервалов, совпадения дат и пропуски."""

from datetime import UTC, datetime
from types import SimpleNamespace

from dq_monitor.web.charts import metric_chart


def row(day, value):
    return SimpleNamespace(
        run=SimpleNamespace(logical_now=datetime(2025, 1, day, tzinfo=UTC)),
        observed_value=value,
        expected_low=None,
        expected_high=None,
    )


def test_real_time_scale():
    chart = metric_chart([row(1, 3), row(2, 4), row(31, 5)])
    xs = [point[0] for point in chart["dots"]]
    assert xs[0] == 60 and xs[2] == 860
    assert abs(xs[1] - (60 + 800 / 30)) < 1e-8


def test_identical_time_and_gaps():
    chart = metric_chart([row(1, 3), row(1, None), row(1, 4)])
    assert [x for x, _ in chart["dots"]] == [460, 460]
    assert chart["observed"].count("M") == 2
    assert "L" not in chart["observed"]


def test_nonfinite_and_empty():
    assert metric_chart([]) is None
    assert metric_chart([row(1, float("nan"))]) is None
    assert metric_chart([row(1, float("inf"))]) is None
