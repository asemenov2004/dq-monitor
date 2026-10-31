"""Ядро проверок на настоящей базе: полигон SF 0.01, внесённые дефекты, инциденты, ошибки.

Одна временная база играет роль и хранилища (схемы src, dwh, stand), и базы
метаданных (схема public). Перед каждым сценарием полигон пересоздаётся,
загружаются три партии (01.01, 02.01, 03.01), история прогонов очищается.
Проверки смотрят на ожидаемую партию 03.01 (логическое время 04.01 02:00).
"""

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from dq_monitor.catalog import CatalogError, load_catalog
from dq_monitor.checks import ERROR, FAIL, PASS
from dq_monitor.config import get_settings
from dq_monitor.meta.models import (
    CheckResult,
    Connection,
    Dataset,
    DqCheck,
    Incident,
    Notification,
    Run,
)
from dq_monitor.report import format_run
from dq_monitor.runner import run_checks
from dq_stand.config import StandSettings
from dq_stand.generate import generate_src
from dq_stand.loader import create_dwh, run_cycles
from dq_stand.register import register_tpch

TEST_DB = "dq_core_test"
NOW = datetime(2025, 1, 4, 2, 0, tzinfo=UTC)
BATCH = "2025-01-03"
REPO = Path(__file__).parent.parent
ALL_CHECKS = 22


def quiet(_):
    pass


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    settings = get_settings()
    admin = create_engine(
        settings.meta_url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3}
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    except OperationalError:
        pytest.skip("PostgreSQL недоступен")

    eng = create_engine(settings.meta_url.set(database=TEST_DB))
    generate_src(eng, 0.01, StandSettings(_env_file=None).start_date, log=quiet)
    create_dwh(eng)
    cfg = Config()
    cfg.set_main_option("script_location", "dq_monitor.meta:migrations")
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    register_tpch(eng, settings.model_copy(update={"warehouse_db": TEST_DB}))

    # Тот же набор проверок, что для SF 1, но с порогами объёма под SF 0.01 (единицы заказов в день)
    catalog = yaml.safe_load((REPO / "checks" / "tpch.yaml").read_text(encoding="utf-8"))
    for check in catalog["checks"]:
        if check["name"] == "orders_row_count":
            check["params"] = {"min": 1, "max": 100}
        if check["name"] == "lineitem_row_count":
            check["params"] = {"min": 1, "max": 500}
    path = tmp_path_factory.mktemp("catalog") / "tpch_sf001.yaml"
    path.write_text(yaml.safe_dump(catalog, allow_unicode=True), encoding="utf-8")
    load_catalog(eng, path)

    yield eng

    eng.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE {TEST_DB} WITH (FORCE)"))
    admin.dispose()


@pytest.fixture
def fresh(engine):
    """Чистый полигон с тремя партиями и пустой историей прогонов."""
    create_dwh(engine)
    run_cycles(engine, 3, log=quiet)
    with Session(engine) as s, s.begin():
        for model in (Notification, Incident, CheckResult, Run):
            s.execute(delete(model))
        s.execute(text("UPDATE dq_check SET is_active = (name NOT LIKE 'tmp_%')"))
    with engine.connect() as conn:
        empty = conn.execute(
            text("SELECT count(*) FROM dwh.orders WHERE batch_date = :d"), {"d": BATCH}
        ).scalar_one()
    assert empty > 0, "в тестовой партии нет заказов, сценарии не имеют смысла"
    return engine


def execute(engine, *statements):
    with engine.begin() as conn:
        for sql in statements:
            conn.execute(text(sql))


def scalar(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


def run(engine, now=NOW, **kwargs):
    return run_checks(engine, now, statement_timeout_sec=kwargs.pop("timeout", 5), **kwargs)


def with_status(report, status):
    return {item.check_name for item in report.items if item.outcome.status == status}


def add_check(engine, name, check_type, params, table="orders"):
    """Временная проверка для сценария; в следующем тесте фикстура её выключит."""
    with Session(engine) as s, s.begin():
        dataset = s.scalar(select(Dataset).where(Dataset.table_name == table))
        check = s.scalar(select(DqCheck).where(DqCheck.name == name))
        if check is None:
            check = DqCheck(dataset_id=dataset.id, name=name, check_type=check_type)
            s.add(check)
        check.check_type, check.params, check.is_active = check_type, params, True
        s.flush()
        return check.id


def check_id(engine, name):
    return scalar(engine, f"SELECT id FROM dq_check WHERE name = '{name}'")


ORDER_IN_BATCH = f"(SELECT MIN(o_orderkey) FROM dwh.orders WHERE batch_date = '{BATCH}')"
LINE_IN_BATCH = f"(SELECT MIN(l_orderkey) FROM dwh.lineitem WHERE batch_date = '{BATCH}')"


# ---------- чистые данные ----------


def test_clean_data_all_checks_pass(fresh):
    report = run(fresh)
    assert len(report.items) == ALL_CHECKS
    assert with_status(report, FAIL) == set()
    assert with_status(report, ERROR) == set()
    assert report.ok


def test_run_is_saved(fresh):
    report = run(fresh)
    with Session(fresh) as s:
        saved = s.get(Run, report.run_id)
        assert saved.finished_at is not None
        assert saved.logical_now == NOW
        results = s.scalars(select(CheckResult).where(CheckResult.run_id == report.run_id)).all()
    assert len(results) == ALL_CHECKS
    assert all(r.duration_ms is not None for r in results)


# ---------- каждый дефект ловится своей проверкой и только ею ----------

DEFECTS = {
    "null_key": (
        [f"UPDATE dwh.orders SET o_custkey = NULL WHERE o_orderkey = {ORDER_IN_BATCH}"],
        {"orders_custkey_not_null"},
    ),
    "duplicate_row": (
        [f"INSERT INTO dwh.orders SELECT * FROM dwh.orders WHERE o_orderkey = {ORDER_IN_BATCH}"],
        {"orders_pk_unique"},
    ),
    "negative_price": (
        [f"UPDATE dwh.orders SET o_totalprice = -5 WHERE o_orderkey = {ORDER_IN_BATCH}"],
        {"orders_totalprice_range"},
    ),
    "price_too_high": (
        [f"UPDATE dwh.orders SET o_totalprice = 700000 WHERE o_orderkey = {ORDER_IN_BATCH}"],
        {"orders_totalprice_range"},
    ),
    "unknown_status": (
        [f"UPDATE dwh.orders SET o_orderstatus = 'X' WHERE o_orderkey = {ORDER_IN_BATCH}"],
        {"orders_status_values"},
    ),
    "orphan_customer": (
        [f"UPDATE dwh.orders SET o_custkey = 99999999 WHERE o_orderkey = {ORDER_IN_BATCH}"],
        {"orders_customer_fk"},
    ),
    "date_not_in_batch": (
        [
            "UPDATE dwh.orders SET o_orderdate = o_orderdate - 1 "
            f"WHERE o_orderkey = {ORDER_IN_BATCH}"
        ],
        {"orders_date_matches_batch"},
    ),
    "discount_out_of_range": (
        [f"UPDATE dwh.lineitem SET l_discount = 0.5 WHERE l_orderkey = {LINE_IN_BATCH}"],
        {"lineitem_discount_range"},
    ),
    "line_without_order": (
        [f"UPDATE dwh.lineitem SET l_orderkey = 99999999 WHERE l_orderkey = {LINE_IN_BATCH}"],
        {"lineitem_order_fk"},
    ),
    "line_null_key": (
        [f"UPDATE dwh.lineitem SET l_orderkey = NULL WHERE l_orderkey = {LINE_IN_BATCH}"],
        {"lineitem_orderkey_not_null"},
    ),
    "ship_before_order": (
        [
            "UPDATE dwh.lineitem l SET l_shipdate = o.o_orderdate - 1 FROM dwh.orders o "
            f"WHERE o.o_orderkey = l.l_orderkey AND l.l_orderkey = {LINE_IN_BATCH}"
        ],
        {"lineitem_ship_after_order"},
    ),
    # Пустая партия заказов: позиции этой партии остались без заказов, это тоже ловится
    "orders_batch_lost": (
        [f"DELETE FROM dwh.orders WHERE batch_date = '{BATCH}'"],
        {"orders_row_count", "lineitem_order_fk"},
    ),
    "customer_duplicate": (
        ["INSERT INTO dwh.customer SELECT * FROM dwh.customer WHERE c_custkey = 1"],
        {"customer_pk_unique"},
    ),
    "customer_bad_nation": (
        ["UPDATE dwh.customer SET c_nationkey = 99 WHERE c_custkey = 1"],
        {"customer_nation_fk"},
    ),
    "nation_lost": (
        ["DELETE FROM dwh.nation WHERE n_nationkey = 0"],
        {"nation_row_count", "customer_nation_fk"},
    ),
}


def test_report_text_lists_problems(fresh):
    execute(fresh, f"UPDATE dwh.orders SET o_orderstatus = 'X' WHERE o_orderkey = {ORDER_IN_BATCH}")
    text_report = format_run(run(fresh))
    assert "логическое время 2025-01-04 02:00 UTC" in text_report
    assert "[FAIL] orders_status_values: Значения o_orderstatus вне правила (из {F, O, P})" in (
        text_report
    )
    assert f"pass {ALL_CHECKS - 1}, fail 1, error 0; инциденты: открыто 1" in text_report


@pytest.mark.parametrize("defect", sorted(DEFECTS))
def test_defect_is_detected_by_exactly_expected_checks(fresh, defect):
    statements, expected = DEFECTS[defect]
    execute(fresh, *statements)
    report = run(fresh)
    assert with_status(report, FAIL) == expected
    assert with_status(report, ERROR) == set()


def test_defect_in_old_batch_does_not_affect_current_batch_checks(fresh):
    # Дефект в партии 01.01, а проверяется партия 03.01: партийные проверки его не видят
    execute(fresh, "UPDATE dwh.orders SET o_custkey = NULL WHERE batch_date = '2025-01-01'")
    assert with_status(run(fresh), FAIL) == set()


def test_missing_batch_then_stale_data(fresh):
    # Модельные сутки прошли, а партия за 04.01 не пришла: объём ноль, но свежесть ещё в норме
    one_day = run(fresh, now=datetime(2025, 1, 5, 2, 0, tzinfo=UTC))
    assert with_status(one_day, FAIL) == {"orders_row_count", "lineitem_row_count"}
    # Ещё сутки без загрузки: данные устарели (48 ч > 26 ч)
    two_days = run(fresh, now=datetime(2025, 1, 6, 2, 0, tzinfo=UTC))
    assert with_status(two_days, FAIL) == {
        "orders_row_count",
        "lineitem_row_count",
        "orders_freshness",
        "lineitem_freshness",
    }


# ---------- инциденты ----------


def incidents(engine, name):
    with Session(engine) as s:
        return s.scalars(
            select(Incident)
            .where(Incident.check_id == check_id(engine, name))
            .order_by(Incident.id)
        ).all()


def event(report, name):
    return next(i.incident_event for i in report.items if i.check_name == name)


def test_incident_lifecycle(fresh):
    name = "orders_custkey_not_null"
    break_it = f"UPDATE dwh.orders SET o_custkey = NULL WHERE o_orderkey = {ORDER_IN_BATCH}"
    fix_it = (
        "UPDATE dwh.orders o SET o_custkey = s.o_custkey FROM src.orders s "
        "WHERE s.o_orderkey = o.o_orderkey AND o.o_custkey IS NULL"
    )

    execute(fresh, break_it)
    first = run(fresh)
    assert event(first, name) == "opened"
    second = run(fresh)
    assert event(second, name) is None  # повторное падение не плодит инциденты
    assert len(incidents(fresh, name)) == 1

    execute(fresh, fix_it)
    third = run(fresh)
    assert event(third, name) == "closed"
    [incident] = incidents(fresh, name)
    assert incident.state == "closed"
    assert incident.opened_at == NOW and incident.closed_at == NOW
    with Session(fresh) as s:
        opened = s.get(CheckResult, incident.opened_result_id)
        closed = s.get(CheckResult, incident.closed_result_id)
    assert (opened.run_id, opened.status) == (first.run_id, FAIL)
    assert (closed.run_id, closed.status) == (third.run_id, PASS)

    # Новое падение после закрытия открывает новый инцидент
    execute(fresh, break_it)
    assert event(run(fresh), name) == "opened"
    states = [i.state for i in incidents(fresh, name)]
    assert states == ["closed", "open"]


def test_error_does_not_open_or_close_incident(fresh):
    cid = add_check(fresh, "tmp_flaky", "custom_sql", {"sql": "SELECT 1"})
    assert event(run(fresh, check_ids=[cid]), "tmp_flaky") == "opened"

    add_check(fresh, "tmp_flaky", "custom_sql", {"sql": "SELEKT 1"})
    report = run(fresh, check_ids=[cid])
    assert report.items[0].outcome.status == ERROR
    assert report.items[0].incident_event is None
    assert [i.state for i in incidents(fresh, "tmp_flaky")] == ["open"]

    add_check(fresh, "tmp_flaky", "custom_sql", {"sql": "SELECT 0"})
    assert event(run(fresh, check_ids=[cid]), "tmp_flaky") == "closed"


# ---------- ошибки и безопасность ----------


def test_errors_are_isolated_and_explained(fresh):
    orders_before = scalar(fresh, "SELECT count(*) FROM dwh.orders")
    ids = [
        add_check(fresh, "tmp_syntax", "custom_sql", {"sql": "SELEKT 1"}),
        add_check(fresh, "tmp_attack", "custom_sql", {"sql": "DELETE FROM dwh.orders RETURNING 1"}),
        add_check(fresh, "tmp_drop", "custom_sql", {"sql": "DROP TABLE dwh.orders"}),
        add_check(fresh, "tmp_slow", "custom_sql", {"sql": "SELECT pg_sleep(5)"}),
        add_check(fresh, "tmp_bad_params", "not_null", {}),
        add_check(fresh, "tmp_no_column", "not_null", {"column": "no_such_column"}),
        check_id(fresh, "orders_row_count"),
    ]
    report = run(fresh, check_ids=ids, timeout=1)
    messages = {i.check_name: i.outcome.message for i in report.items}
    statuses = {i.check_name: i.outcome.status for i in report.items}

    assert statuses.pop("orders_row_count") == PASS  # ошибки соседей не мешают
    assert set(statuses.values()) == {ERROR}
    assert "syntax error" in messages["tmp_syntax"]
    assert "read-only transaction" in messages["tmp_attack"]
    assert "read-only transaction" in messages["tmp_drop"]
    assert "statement timeout" in messages["tmp_slow"]
    assert messages["tmp_bad_params"].startswith("Неверные параметры: column")
    assert "no_such_column" in messages["tmp_no_column"]
    # Данные не пострадали, таблица на месте
    assert scalar(fresh, "SELECT count(*) FROM dwh.orders") == orders_before
    assert report.duration_sec < 4  # медленный запрос оборван по таймауту, а не дождался


def test_missing_password_variable_gives_error(fresh):
    with Session(fresh) as s, s.begin():
        conn = s.scalar(select(Connection).where(Connection.name == "tmp_conn"))
        if conn is None:
            conn = Connection(
                name="tmp_conn",
                db_type="postgres",
                host="localhost",
                port=5433,
                database=TEST_DB,
                username="dq",
                password_env="DQ_TEST_NO_SUCH_VARIABLE",
            )
            ds = Dataset(connection=conn, schema_name="dwh", table_name="nation")
            s.add_all(
                [
                    conn,
                    ds,
                    DqCheck(
                        dataset=ds, name="tmp_secret", check_type="row_count", params={"min": 1}
                    ),
                ]
            )
    cid = check_id(fresh, "tmp_secret")
    report = run(fresh, check_ids=[cid])
    assert report.items[0].outcome.status == ERROR
    assert "DQ_TEST_NO_SUCH_VARIABLE" in report.items[0].outcome.message


def test_inactive_checks_are_skipped(fresh):
    execute(fresh, "UPDATE dq_check SET is_active = false WHERE name = 'orders_custkey_not_null'")
    execute(fresh, f"UPDATE dwh.orders SET o_custkey = NULL WHERE o_orderkey = {ORDER_IN_BATCH}")
    report = run(fresh)
    assert len(report.items) == ALL_CHECKS - 1
    assert with_status(report, FAIL) == set()


# ---------- загрузка набора проверок ----------


def test_catalog_update_and_errors(fresh, tmp_path):
    base = """
connection: tpch_warehouse
checks:
  - name: orders_row_count
    dataset: dwh.orders
    type: row_count
    description: Заказов за день в обычном диапазоне
    params: {min: 1, max: 100}
"""
    path = tmp_path / "c.yaml"

    def load(text):
        path.write_text(text, encoding="utf-8")
        return load_catalog(fresh, path)

    def stored_params():
        return scalar(fresh, "SELECT params::text FROM dq_check WHERE name = 'orders_row_count'")

    assert load(base).unchanged == ["dwh.orders / orders_row_count"]
    assert load(base.replace("max: 100", "max: 99")).updated == ["dwh.orders / orders_row_count"]
    assert stored_params() == '{"max": 99, "min": 1}'
    assert load(base).updated == ["dwh.orders / orders_row_count"]
    params_before = stored_params()

    # Файл с ошибкой не меняет в базе ничего, даже корректные его части
    broken = base.replace("max: 100", "max: 5") + (
        "  - {name: x, dataset: dwh.no_such_table, type: row_count, params: {min: 1}}\n"
    )
    with pytest.raises(CatalogError, match="no_such_table не зарегистрирован"):
        load(broken)
    assert stored_params() == params_before

    with pytest.raises(CatalogError, match="nowhere не зарегистрировано"):
        load(base.replace("tpch_warehouse", "nowhere"))


# ---------- консольные команды ----------


def cli(*args):
    env = os.environ | {
        "WAREHOUSE_DB": TEST_DB,
        "META_DB": TEST_DB,
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        [sys.executable, "-m", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=REPO,
        timeout=120,
    )


def test_cli_run_exit_codes(fresh):
    clean = cli("dq_monitor", "run", "--now", "2025-01-04T02:00")
    assert clean.returncode == 0, clean.stderr
    assert f"pass {ALL_CHECKS}, fail 0, error 0" in clean.stdout

    execute(fresh, f"UPDATE dwh.orders SET o_totalprice = -1 WHERE o_orderkey = {ORDER_IN_BATCH}")
    broken = cli("dq_monitor", "run", "--now", "2025-01-04T02:00")
    assert broken.returncode == 1
    assert "[FAIL] orders_totalprice_range" in broken.stdout


def test_cli_checks_sql(fresh):
    result = cli(
        "dq_monitor",
        "checks",
        "sql",
        str(check_id(fresh, "orders_customer_fk")),
        "--now",
        "2025-01-04T02:00",
    )
    assert result.returncode == 0, result.stderr
    assert 'LEFT JOIN "dwh"."customer" AS p' in result.stdout
    assert "datetime.date(2025, 1, 3)" in result.stdout


def test_cli_stand_cycle(fresh):
    result = cli("dq_stand", "cycle", "--days", "2")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0].startswith("2025-01-04:") and lines[1].startswith("2025-01-05:")
    assert all(f"pass {ALL_CHECKS}, fail 0" in line for line in lines)
