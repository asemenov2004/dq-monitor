"""Проверяем формулы на вручную рассчитанных рядах, параметры и измерение метрик."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dq_monitor.anomalies.detector import detect
from dq_monitor.checks import FAIL, PASS, SKIP, CheckContext, create_check
from dq_monitor.connectors.base import Connector


class SampleConnector(Connector):
    def __init__(self, row):
        self.row = row

    def quote(self, name):
        return '"' + name.replace('"', '""') + '"'

    def fetch_one(self, sql, params):
        self.sql, self.params = sql, params
        return self.row

    def close(self):
        pass


def context(row, history=()):
    return CheckContext(
        SampleConnector(row),
        "dwh",
        "t",
        "batch_date",
        "loaded_at",
        datetime(2025, 2, 1, 2, tzinfo=UTC),
        history,
    )


@pytest.mark.parametrize(
    "value,anomalous,z", [(7, False, -3), (13, False, 3), (14, True, 4), (6, True, -4)]
)
def test_hand_calculated_boundaries(value, anomalous, z):
    # Среднее 10, выборочная дисперсия 1, коридор [7, 13]
    result = detect(value, (9, 10, 11), window=3)
    assert result.ready and result.anomalous == anomalous
    assert (result.mean, result.stddev, result.low, result.high, result.z_score) == (
        10,
        1,
        7,
        13,
        z,
    )


def test_current_outlier_not_added_to_baseline():
    assert detect(1000, (9, 10, 11), window=3).high == 13


def test_window_drops_oldest_point():
    assert detect(10, (10000, 9, 10, 11), window=3).mean == 10


@pytest.mark.parametrize(
    "value,anomalous,score", [(10, False, 0), (11, True, None), (0, True, None)]
)
def test_zero_variance(value, anomalous, score):
    result = detect(value, (10, 10, 10), window=3)
    assert result.anomalous == anomalous and result.z_score == score
    assert result.constant_shift == anomalous


def test_warmup_and_custom_minimum():
    assert not detect(10, (9, 11), window=3).ready
    assert detect(10, (9, 11), window=3, min_history=2).ready


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window": 1},
        {"window": 3, "min_history": 4},
        {"min_history": 1},
        {"threshold": 0},
        {"threshold": float("inf")},
        {"threshold": float("nan")},
    ],
)
def test_bad_detector_settings(kwargs):
    with pytest.raises(ValueError):
        detect(1, (1, 2, 3), **kwargs)


@pytest.mark.parametrize(
    "value,history", [(float("nan"), (1, 2)), (1, (float("inf"), 2)), (float("-inf"), (1, 2))]
)
def test_nonfinite_measurements_rejected(value, history):
    with pytest.raises(ValueError):
        detect(value, history, window=2)


@pytest.mark.parametrize(
    "params",
    [
        {"metric": "sum"},
        {"metric": "mean"},
        {"metric": "null_ratio"},
        {"metric": "row_count", "column": "x"},
        {"metric": "mean", "column": ""},
        {"metric": "row_count", "window": 1},
        {"metric": "row_count", "window": 3, "min_history": 4},
        {"metric": "row_count", "threshold": float("nan")},
    ],
)
def test_invalid_check_params(params):
    with pytest.raises(ValidationError):
        create_check("anomaly", params)


@pytest.mark.parametrize(
    "metric,column,row,value",
    [
        ("row_count", None, (0,), 0),
        ("null_ratio", "x", (20, 5), 0.25),
        ("mean", "x", (12.5,), 12.5),
    ],
)
def test_warmup_preserves_numeric_measurement(metric, column, row, value):
    params = {"metric": metric, "window": 3}
    if column:
        params["column"] = column
    result = create_check("anomaly", params).run(context(row))
    assert result.status == SKIP and result.observed == value
    assert result.details["reason"] == "warmup"


@pytest.mark.parametrize("metric,row", [("mean", (None,)), ("null_ratio", (0, 0))])
def test_empty_undefined_metric_skips(metric, row):
    result = create_check("anomaly", {"metric": metric, "column": "x"}).run(context(row))
    assert result.status == SKIP and result.observed is None
    assert result.details["reason"] == "no_data"


@pytest.mark.parametrize("value,status", [(10, PASS), (14, FAIL)])
def test_outcome_details_and_bounds(value, status):
    result = create_check("anomaly", {"metric": "mean", "column": "x", "window": 3}).run(
        context((value,), (9, 10, 11))
    )
    assert result.status == status and result.observed == value
    assert (result.expected_low, result.expected_high) == (7, 13)
    assert result.details["count"] == 3 and result.details["z_score"] == value - 10


def test_sql_quoting_and_scope():
    check = create_check("anomaly", {"metric": "mean", "column": 'x"; DROP TABLE t;--'})
    ctx = context((1,))
    query = check.build_query(ctx)
    assert 'AVG("x""; DROP TABLE t;--")' in query.sql
    assert 'WHERE "batch_date" = :batch_date' in query.sql
    assert str(query.params["batch_date"]) == "2025-01-31"
    query = check.build_query(replace(ctx, batch_column=None))
    assert "WHERE" not in query.sql
    check = create_check("anomaly", {"metric": "row_count", "scope": "table"})
    assert "WHERE" not in check.build_query(ctx).sql


def test_nonfinite_metric_becomes_execution_error():
    with pytest.raises(ValueError, match="конечным"):
        create_check("anomaly", {"metric": "mean", "column": "x"}).run(context((float("nan"),)))


@pytest.mark.parametrize("value,points", [(0, (-1e308, 1e308)), (1e308, (0, 1e-300))])
def test_statistical_overflow_is_explicit_error(value, points):
    with pytest.raises(ValueError, match="Переполнение"):
        detect(value, points, window=2)
