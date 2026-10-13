"""Полигон целиком на SF 0.01 во временной базе. Нужен запущенный PostgreSQL."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

pytest.importorskip("duckdb")

from dq_monitor.config import get_settings  # noqa: E402
from dq_stand.config import StandSettings  # noqa: E402
from dq_stand.generate import generate_src  # noqa: E402
from dq_stand.loader import create_dwh, get_status, reset, run_cycles  # noqa: E402

TEST_DB = "dq_stand_test"


@pytest.fixture(scope="module")
def engine():
    settings = get_settings()
    admin = create_engine(
        settings.warehouse_url,
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 3},
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    except OperationalError:
        pytest.skip("PostgreSQL недоступен")

    test_engine = create_engine(settings.warehouse_url.set(database=TEST_DB))
    stand = StandSettings(_env_file=None, scale_factor=0.01)
    generate_src(test_engine, stand.scale_factor, stand.start_date, log=lambda _: None)
    create_dwh(test_engine)
    yield test_engine

    test_engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE {TEST_DB} WITH (FORCE)"))
    admin.dispose()


def scalar(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


def test_src_starts_at_start_date(engine):
    assert str(scalar(engine, "SELECT min(o_orderdate) FROM src.orders")) == "2025-01-01"


def test_dimensions_copied_fully(engine):
    assert scalar(engine, "SELECT count(*) FROM dwh.customer") == scalar(
        engine, "SELECT count(*) FROM src.customer"
    )


def test_cycles_load_matching_rows(engine):
    reset(engine)
    run_cycles(engine, 5, log=lambda _: None)

    assert scalar(engine, "SELECT count(DISTINCT batch_date) FROM dwh.orders") == 5
    src_orders = scalar(
        engine, "SELECT count(*) FROM src.orders WHERE o_orderdate < DATE '2025-01-06'"
    )
    assert scalar(engine, "SELECT count(*) FROM dwh.orders") == src_orders
    src_lineitem = scalar(
        engine,
        "SELECT count(*) FROM src.lineitem l JOIN src.orders o ON o.o_orderkey = l.l_orderkey "
        "WHERE o.o_orderdate < DATE '2025-01-06'",
    )
    assert scalar(engine, "SELECT count(*) FROM dwh.lineitem") == src_lineitem


def test_next_load_continues_and_reset_restarts(engine):
    reset(engine)
    run_cycles(engine, 2, log=lambda _: None)
    run_cycles(engine, 1, log=lambda _: None)
    status = get_status(engine)
    assert status.batches == 3
    assert str(status.last_batch_date) == "2025-01-03"
    assert status.lag_days == 0

    reset(engine)
    status = get_status(engine)
    assert status.batches == 0
    assert status.orders_rows == 0
    assert str(status.model_now) == "2025-01-01 02:00:00"


def test_loaded_at_is_next_day_at_load_time(engine):
    reset(engine)
    run_cycles(engine, 3, log=lambda _: None)
    bad = scalar(
        engine,
        "SELECT count(*) FROM dwh.orders "
        "WHERE loaded_at <> batch_date + INTERVAL '1 day' + TIME '02:00'",
    )
    assert bad == 0
