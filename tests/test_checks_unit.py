"""Ядро проверок без базы: построение SQL, оценка результата, валидация параметров."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from dq_monitor.checks import CHECK_CLASSES, ERROR, FAIL, PASS, CheckContext, create_check
from dq_monitor.checks.base import to_naive_utc
from dq_monitor.connectors.base import Connector
from dq_monitor.errors import error_text
from dq_monitor.meta.models import CHECK_TYPES
from dq_monitor.report import format_bounds, format_table, format_value

NOW = datetime(2025, 1, 4, 2, 0, tzinfo=UTC)
BATCH = date(2025, 1, 3)


class FakeConnector(Connector):
    """Запоминает запрос и возвращает заранее заданную строку."""

    def __init__(self, row=(0,)):
        self.row = row
        self.sql = None
        self.params = None

    def quote(self, name):
        return '"' + name.replace('"', '""') + '"'

    def fetch_one(self, sql, params):
        self.sql, self.params = sql, dict(params)
        return self.row

    def close(self):
        pass


def ctx(row=(0,), batch_column="batch_date", loaded_at_column="loaded_at", now=NOW, table="orders"):
    return CheckContext(FakeConnector(row), "dwh", table, batch_column, loaded_at_column, now)


def run(check_type, params, row, **kwargs):
    c = ctx(row, **kwargs)
    outcome = create_check(check_type, params).run(c)
    return outcome, c.connector


# ---------- общее ----------


def test_registry_matches_allowed_types_in_database():
    assert set(CHECK_CLASSES) == set(CHECK_TYPES)


def test_unknown_type_rejected():
    with pytest.raises(ValueError, match="Неизвестный тип"):
        create_check("no_such_type", {})


@pytest.mark.parametrize("check_type", sorted(CHECK_TYPES))
def test_typo_in_params_rejected(check_type):
    valid = {
        "not_null": {"column": "a"},
        "unique": {"columns": ["a"]},
        "freshness": {"max_age_hours": 1},
        "row_count": {"min": 1},
        "range": {"column": "a", "min": 0},
        "referential": {"column": "a", "ref_table": "s.t", "ref_column": "b"},
        "custom_sql": {"sql": "SELECT 0"},
        "anomaly": {"metric": "row_count"},
    }[check_type]
    create_check(check_type, valid)  # корректные параметры принимаются
    with pytest.raises(ValidationError):
        create_check(check_type, valid | {"colum": "опечатка"})


def test_batch_date_is_previous_day_in_utc():
    assert ctx().batch_date == BATCH
    # 01:00 по Москве 4 января это 22:00 UTC 3 января, значит ожидается партия за 2 января
    moscow = datetime(2025, 1, 4, 1, 0, tzinfo=timezone(timedelta(hours=3)))
    assert ctx(now=moscow).batch_date == date(2025, 1, 2)


def test_identifiers_are_quoted_against_injection():
    _, conn = run("not_null", {"column": 'x"; DROP TABLE t; --'}, (10, 0))
    assert '"x""; DROP TABLE t; --"' in conn.sql


def test_values_go_to_params_not_into_sql_text():
    _, conn = run("range", {"column": "s", "values": ["O'Neil", "F"]}, (10, 0))
    assert "O'Neil" not in conn.sql
    assert conn.params["allowed_values"] == ["O'Neil", "F"]


# ---------- область проверки ----------


def test_batch_scope_filters_expected_batch():
    _, conn = run("row_count", {"min": 1}, (5,))
    assert 'WHERE "batch_date" = :batch_date' in conn.sql
    assert conn.params == {"batch_date": BATCH}


def test_table_scope_has_no_filter():
    _, conn = run("row_count", {"min": 1, "scope": "table"}, (5,))
    assert "WHERE" not in conn.sql and conn.params == {}


def test_dimension_without_batch_column_checks_whole_table():
    _, conn = run("row_count", {"min": 1}, (5,), batch_column=None)
    assert "WHERE" not in conn.sql


# ---------- not_null ----------


@pytest.mark.parametrize(
    ("row", "limit", "status", "observed"),
    [
        ((100, 0), 0, PASS, 0.0),
        ((100, 1), 0, FAIL, 0.01),
        ((100, 5), 0.05, PASS, 0.05),  # ровно на границе ещё норма
        ((100, 6), 0.05, FAIL, 0.06),
    ],
)
def test_not_null_thresholds(row, limit, status, observed):
    outcome, conn = run("not_null", {"column": "c", "max_null_ratio": limit}, row)
    assert outcome.status == status
    assert outcome.observed == pytest.approx(observed)
    assert 'COUNT(*) - COUNT("c")' in conn.sql


def test_not_null_empty_batch_passes_without_value():
    outcome, _ = run("not_null", {"column": "c"}, (0, 0))
    assert outcome.status == PASS and outcome.observed is None


def test_not_null_ratio_must_be_between_0_and_1():
    with pytest.raises(ValidationError):
        create_check("not_null", {"column": "c", "max_null_ratio": 1.5})


# ---------- unique ----------


def test_unique_composite_key_and_count():
    outcome, conn = run("unique", {"columns": ["a", "b"]}, (3,))
    assert 'GROUP BY "a", "b" HAVING COUNT(*) > 1' in conn.sql
    assert outcome.status == FAIL and outcome.observed == 3


def test_unique_needs_columns():
    with pytest.raises(ValidationError):
        create_check("unique", {"columns": []})


# ---------- row_count ----------


@pytest.mark.parametrize(
    ("rows", "status"), [(0, FAIL), (449, FAIL), (450, PASS), (800, PASS), (801, FAIL)]
)
def test_row_count_bounds(rows, status):
    outcome, _ = run("row_count", {"min": 450, "max": 800}, (rows,))
    assert outcome.status == status
    assert (outcome.expected_low, outcome.expected_high) == (450, 800)


def test_row_count_one_sided():
    assert run("row_count", {"max": 10}, (0,))[0].status == PASS
    assert run("row_count", {"min": 10}, (10**9,))[0].status == PASS


@pytest.mark.parametrize("params", [{}, {"min": 10, "max": 5}, {"min": -1}])
def test_row_count_invalid_bounds(params):
    with pytest.raises(ValidationError):
        create_check("row_count", params)


# ---------- range ----------


def test_range_builds_all_violations():
    outcome, conn = run("range", {"column": "p", "min": 0, "max": 10, "values": [1, 2]}, (100, 4))
    assert '"p" < :min_value OR "p" > :max_value OR "p" NOT IN :allowed_values' in conn.sql
    assert '"p" IS NOT NULL' in conn.sql  # NULL не нарушение диапазона
    assert outcome.status == FAIL and outcome.observed == pytest.approx(0.04)


def test_range_sum_null_on_empty_selection():
    outcome, _ = run("range", {"column": "p", "min": 0}, (0, None))
    assert outcome.status == PASS


def test_range_tolerates_allowed_share():
    outcome, _ = run("range", {"column": "p", "min": 0, "max_bad_ratio": 0.05}, (100, 5))
    assert outcome.status == PASS


@pytest.mark.parametrize("params", [{"column": "p"}, {"column": "p", "min": 5, "max": 1}])
def test_range_invalid_rule(params):
    with pytest.raises(ValidationError):
        create_check("range", params)


# ---------- referential ----------


def test_referential_left_join_and_batch_alias():
    outcome, conn = run(
        "referential",
        {"column": "o_custkey", "ref_table": "dwh.customer", "ref_column": "c_custkey"},
        (2,),
    )
    assert 'LEFT JOIN "dwh"."customer" AS p ON c."o_custkey" = p."c_custkey"' in conn.sql
    assert 'c."o_custkey" IS NOT NULL AND p."c_custkey" IS NULL' in conn.sql
    assert 'c."batch_date" = :batch_date' in conn.sql
    assert outcome.status == FAIL and outcome.observed == 2


@pytest.mark.parametrize("ref", ["customer", "dwh.customer; DROP", "a.b.c", "1x.t"])
def test_referential_ref_table_format(ref):
    with pytest.raises(ValidationError):
        create_check("referential", {"column": "a", "ref_table": ref, "ref_column": "b"})


# ---------- freshness ----------


def test_freshness_age_in_hours_with_mixed_timezones():
    last = datetime(2025, 1, 3, 2, 0)  # в хранилище время без пояса (UTC)
    outcome, conn = run("freshness", {"max_age_hours": 26}, (last,))
    assert conn.sql == 'SELECT MAX("loaded_at") FROM "dwh"."orders"'
    assert outcome.observed == pytest.approx(24.0)
    assert outcome.status == PASS


def test_freshness_boundary_and_stale():
    assert run("freshness", {"max_age_hours": 24}, (datetime(2025, 1, 3, 2, 0),))[0].status == PASS
    stale = run("freshness", {"max_age_hours": 24}, (datetime(2025, 1, 3, 1, 59),))[0]
    assert stale.status == FAIL


def test_freshness_empty_table_fails():
    outcome, _ = run("freshness", {"max_age_hours": 1}, (None,))
    assert outcome.status == FAIL


def test_freshness_date_column_and_explicit_column():
    outcome, conn = run("freshness", {"max_age_hours": 48, "column": "d"}, (date(2025, 1, 3),))
    assert '"d"' in conn.sql
    assert outcome.observed == pytest.approx(26.0)


def test_freshness_without_column_raises():
    with pytest.raises(ValueError, match="столбец времени загрузки"):
        create_check("freshness", {"max_age_hours": 1}).run(ctx(loaded_at_column=None))


def test_freshness_rejects_batch_scope():
    with pytest.raises(ValidationError):
        create_check("freshness", {"max_age_hours": 1, "scope": "batch"})


def test_to_naive_utc():
    aware = datetime(2025, 1, 4, 5, 0, tzinfo=timezone(timedelta(hours=3)))
    assert to_naive_utc(aware) == datetime(2025, 1, 4, 2, 0)
    assert to_naive_utc(datetime(2025, 1, 1)) == datetime(2025, 1, 1)


# ---------- custom_sql ----------


def test_custom_sql_params_and_semicolon():
    outcome, conn = run("custom_sql", {"sql": "  SELECT 1 FROM t WHERE d = :batch_date;  "}, (0,))
    assert conn.sql == "SELECT 1 FROM t WHERE d = :batch_date"
    assert conn.params == {"batch_date": BATCH, "logical_now": datetime(2025, 1, 4, 2, 0)}
    assert outcome.status == PASS


@pytest.mark.parametrize(("value", "status"), [(None, PASS), (0, PASS), (2, PASS), (3, FAIL)])
def test_custom_sql_threshold(value, status):
    assert run("custom_sql", {"sql": "SELECT 1", "max_bad": 2}, (value,))[0].status == status


def test_custom_sql_must_return_one_column():
    with pytest.raises(ValueError, match="один столбец"):
        run("custom_sql", {"sql": "SELECT 1, 2"}, (1, 2))


# ---------- сообщения и отчёт ----------


def test_error_text_for_validation_error():
    with pytest.raises(ValidationError) as info:
        create_check("not_null", {})
    assert error_text(info.value).startswith("Неверные параметры: column:")


def test_error_status_constant():
    assert {PASS, FAIL, ERROR} == {"pass", "fail", "error"}


def test_report_formatting():
    assert format_value(None) == "-"
    assert format_value(25.0) == "25"
    assert format_value(0.016666) == "0.01667"
    assert format_bounds(None, 26) == "[-; 26]"
    assert format_bounds(None, None) == "-"
    table = format_table(("A", "Bb"), [("1", "x"), ("22", "yyy")])
    assert table.splitlines() == ["A   Bb", "--  ---", "1   x", "22  yyy"]
