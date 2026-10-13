"""Логика полигона без базы: сдвиг дат, модельные часы, строка подключения."""

from datetime import date, datetime

import pytest
from sqlalchemy import URL

pytest.importorskip("duckdb")

from dq_stand.generate import (  # noqa: E402
    date_offset_days,
    libpq_conninfo,
    select_with_shifted_dates,
)
from dq_stand.loader import Status, initial_clock, next_cycle  # noqa: E402


def test_offset_maps_data_start_to_start_date():
    offset = date_offset_days(date(1992, 1, 1), date(2025, 1, 1))
    assert date(1992, 1, 1).toordinal() + offset == date(2025, 1, 1).toordinal()


def test_offset_keeps_leap_day_separate():
    # 29.02.1992 и 01.03.1992 после сдвига остаются разными днями
    offset = date_offset_days(date(1992, 1, 1), date(2025, 1, 1))
    feb29 = date.fromordinal(date(1992, 2, 29).toordinal() + offset)
    mar01 = date.fromordinal(date(1992, 3, 1).toordinal() + offset)
    assert feb29 != mar01


def test_only_date_columns_are_shifted():
    assert select_with_shifted_dates("customer", 10) == "SELECT * FROM customer"
    sql = select_with_shifted_dates("lineitem", 10)
    for col in ("l_shipdate", "l_commitdate", "l_receiptdate"):
        assert f"{col} + 10 AS {col}" in sql


def test_conninfo_escapes_password():
    url = URL.create("postgresql+psycopg", "u", r"p'a\ss w", "localhost", 5433, "warehouse")
    assert r"password='p\'a\\ss w'" in libpq_conninfo(url)


def test_first_cycle_loads_first_day():
    now, batch = next_cycle(initial_clock(date(2025, 1, 1)))
    assert batch == date(2025, 1, 1)
    assert now == datetime(2025, 1, 2, 2, 0)


def test_lag_when_batch_missing():
    status = Status(datetime(2025, 1, 10, 2), 7, date(2025, 1, 7), 0, 0, date(2031, 1, 1))
    assert status.lag_days == 2
