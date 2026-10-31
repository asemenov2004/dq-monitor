"""История, версии, миграции и инциденты детектора на отдельной временной базе PostgreSQL."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, joinedload

from dq_monitor.anomalies.history import fingerprint, load_history
from dq_monitor.catalog import load_catalog, parse_catalog
from dq_monitor.checks import ERROR, FAIL, PASS, SKIP, create_check
from dq_monitor.config import get_settings
from dq_monitor.meta.models import Base, CheckResult, Connection, Dataset, DqCheck, Incident, Run
from dq_monitor.report import format_run
from dq_monitor.runner import run_checks

TEST_DB = "dq_anomaly_test"
NOW = datetime(2025, 1, 5, 2, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[1]


def migrate(engine, action, revision):
    cfg = Config()
    cfg.set_main_option("script_location", "dq_monitor.meta:migrations")
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        getattr(command, action)(cfg, revision)


@pytest.fixture(scope="module")
def engine():
    settings = get_settings()
    admin = create_engine(
        settings.meta_url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3}
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    except OperationalError:
        admin.dispose()
        pytest.skip("PostgreSQL недоступен")
    eng = create_engine(settings.meta_url.set(database=TEST_DB))
    try:
        migrate(eng, "upgrade", "head")
        with eng.begin() as conn:
            conn.execute(text("CREATE SCHEMA dwh"))
            conn.execute(text("CREATE TABLE dwh.facts (id int, value numeric, batch_date date)"))
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE {TEST_DB} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture
def fresh(engine):
    settings = get_settings()
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE notification, incident, check_result, run, dq_check, "
                "dataset, connection, dwh.facts RESTART IDENTITY CASCADE"
            )
        )
    with Session(engine) as s, s.begin():
        connection = Connection(
            name="tpch_warehouse",
            db_type="postgres",
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=TEST_DB,
            username=settings.postgres_user,
            password_env="POSTGRES_PASSWORD",
        )
        dataset = Dataset(
            connection=connection, schema_name="dwh", table_name="facts", batch_column="batch_date"
        )
        s.add(
            DqCheck(
                dataset=dataset,
                name="volume",
                check_type="anomaly",
                params={"metric": "row_count", "window": 3},
            )
        )
    set_batch(engine, 10)
    return engine


def set_batch(engine, count, day="2025-01-04", value=10):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE dwh.facts"))
        conn.execute(
            text("INSERT INTO dwh.facts SELECT n, :value, :day FROM generate_series(1, :count) n"),
            {"value": value, "day": day, "count": count},
        )


def config(engine, **changes):
    with Session(engine) as s, s.begin():
        check = s.get(DqCheck, 1)
        check.params = check.params | changes


def key(engine):
    with Session(engine) as s:
        check = s.scalar(
            select(DqCheck).options(joinedload(DqCheck.dataset).joinedload(Dataset.connection))
        )
        return fingerprint(check, create_check(check.check_type, check.params))


def seed(engine, when, value, status=PASS, config_hash=None):
    config_hash = key(engine) if config_hash is None else config_hash
    with Session(engine) as s, s.begin():
        run = Run(logical_now=when)
        s.add(
            CheckResult(
                run=run, check_id=1, status=status, observed_value=value, config_hash=config_hash
            )
        )


def baseline(engine):
    for day, value in enumerate((9, 10, 11), start=2):
        seed(engine, datetime(2025, 1, day, 2, tzinfo=UTC), value)


def run(engine, now=NOW):
    return run_checks(engine, now, statement_timeout_sec=3)


def outcome(engine, now=NOW):
    return run(engine, now).items[0].outcome


def history(engine, when=NOW, window=3):
    with Session(engine) as s:
        return load_history(s, 1, key(engine), when, 10**9, window)


def test_real_measurements_warmup_then_detect_and_close(fresh):
    for day, count in enumerate((9, 10, 11), start=1):
        set_batch(fresh, count, f"2025-01-{day:02}")
        now = datetime(2025, 1, day + 1, 2, tzinfo=UTC)
        report = run(fresh, now)
        assert report.ok and report.count(SKIP) == 1
        assert report.items[0].outcome.observed == count
    set_batch(fresh, 20)
    first = run(fresh)
    assert first.count(FAIL) == 1 and first.incidents("opened") == 1
    assert first.items[0].outcome.details["count"] == 3
    second = run(fresh)
    assert second.count(FAIL) == 1 and second.incidents("opened") == 0
    set_batch(fresh, 10)
    third = run(fresh)
    assert third.ok and third.incidents("closed") == 1
    with Session(fresh) as s:
        assert len(s.scalars(select(Incident)).all()) == 1
        saved = s.scalar(select(CheckResult).where(CheckResult.run_id == first.run_id))
        assert saved.expected_low == 7 and saved.expected_high == 13
        assert saved.details["z_score"] == 10 and len(saved.config_hash) == 64
        assert s.scalar(select(Incident.state)) == "closed"
    assert "skip 0" in format_run(third)


def test_same_day_does_not_complete_warmup(fresh):
    for _ in range(4):
        result = outcome(fresh)
        assert result.status == SKIP and result.details["count"] == 0


def test_skip_and_error_do_not_close_open_incident(fresh):
    baseline(fresh)
    set_batch(fresh, 30)
    assert run(fresh).incidents("opened") == 1
    config(fresh, window=4)
    assert outcome(fresh).status == SKIP  # новая конфигурация, истории ещё нет
    config(fresh, metric="mean", column="missing_column")
    assert outcome(fresh).status == ERROR
    with Session(fresh) as s:
        assert s.scalar(select(Incident.state)) == "open"


@pytest.mark.parametrize(
    "metric,count,value,observed,status",
    [
        ("row_count", 0, 10, 0, FAIL),
        ("mean", 0, 10, None, SKIP),
        ("null_ratio", 0, 10, None, SKIP),
        ("mean", 10, None, None, SKIP),
        ("null_ratio", 10, None, 1, FAIL),
        ("mean", 10, 100, 100, FAIL),
    ],
)
def test_empty_null_and_shifted_metrics(fresh, metric, count, value, observed, status):
    params = {"metric": metric}
    if metric != "row_count":
        params["column"] = "value"
    config(fresh, **params)
    baseline(fresh)
    set_batch(fresh, count, value=value)
    result = outcome(fresh)
    assert result.observed == observed and result.status == status


def test_null_ratio_uses_real_zero_history(fresh):
    config(fresh, metric="null_ratio", column="value")
    for day in range(2, 5):
        seed(fresh, datetime(2025, 1, day, tzinfo=UTC), 0, SKIP)
    assert outcome(fresh).status == PASS
    with fresh.begin() as conn:
        conn.execute(text("UPDATE dwh.facts SET value = NULL WHERE id = 1"))
    result = outcome(fresh)
    assert result.status == FAIL and result.observed == 0.1
    assert result.details["constant_shift"] and result.details["z_score"] is None
    json.dumps(result.details, allow_nan=False)


def test_history_deduplicates_and_excludes_current_future(fresh):
    baseline(fresh)
    seed(fresh, datetime(2025, 1, 4, 3, tzinfo=UTC), 12)
    seed(fresh, NOW - timedelta(hours=1), 900)  # тот же день UTC
    seed(fresh, NOW + timedelta(days=1), 999)
    assert history(fresh) == (9, 10, 12)
    assert history(fresh, window=2) == (10, 12)


def test_calendar_window_prevents_stale_baseline(fresh):
    baseline(fresh)
    assert history(fresh, NOW + timedelta(days=30)) == ()
    result = outcome(fresh, NOW + timedelta(days=30))
    assert result.status == SKIP and result.details["count"] == 0


def test_table_scope_measures_all_rows(fresh):
    config(fresh, scope="table")
    baseline(fresh)
    with fresh.begin() as conn:
        conn.execute(text("INSERT INTO dwh.facts VALUES (100, 10, '1990-01-01')"))
    result = outcome(fresh)
    assert result.status == PASS and result.observed == 11


def test_history_last_attempt_not_last_logical_timestamp(fresh):
    baseline(fresh)
    seed(fresh, datetime(2025, 1, 4, 1, tzinfo=UTC), 12)
    assert history(fresh) == (9, 10, 12)


@pytest.mark.parametrize("status,value", [(ERROR, None), (SKIP, None)])
def test_last_invalid_attempt_does_not_fall_back_to_stale_value(fresh, status, value):
    baseline(fresh)
    seed(fresh, datetime(2025, 1, 4, 3, tzinfo=UTC), value, status)
    assert history(fresh) == (9, 10)


def test_failed_and_numeric_warmup_values_are_used(fresh):
    seed(fresh, datetime(2025, 1, 2, tzinfo=UTC), 9, SKIP)
    seed(fresh, datetime(2025, 1, 3, tzinfo=UTC), 10, FAIL)
    seed(fresh, datetime(2025, 1, 4, tzinfo=UTC), 11, PASS)
    assert history(fresh) == (9, 10, 11)


def test_history_excludes_results_recorded_after_run_started(fresh):
    baseline(fresh)
    with Session(fresh) as s:
        assert load_history(s, 1, key(fresh), NOW, 3, 30) == (9, 10)


def test_history_groups_by_utc_day(fresh):
    seed(fresh, datetime(2025, 1, 2, 1, tzinfo=timezone(timedelta(hours=3))), 9)
    seed(fresh, datetime(2025, 1, 1, 23, tzinfo=UTC), 10)
    assert history(fresh, window=30) == (10,)
    # 01:00 +03:00 = предыдущий день 22:00 UTC, точки этого дня ещё не исторические
    when = datetime(2025, 1, 2, 1, tzinfo=timezone(timedelta(hours=3)))
    assert history(fresh, when) == ()


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_corrupt_nonfinite_history_excluded(fresh, value):
    baseline(fresh)
    seed(fresh, datetime(2025, 1, 4, tzinfo=UTC), value)
    assert history(fresh) == (9, 10)


def test_config_change_and_old_unversioned_results(fresh):
    baseline(fresh)
    assert outcome(fresh).status == PASS
    config(fresh, metric="mean", column="value")
    assert outcome(fresh).status == SKIP
    assert history(fresh) == ()
    with fresh.begin() as conn:
        conn.execute(text("UPDATE check_result SET config_hash = NULL"))
    config(fresh, metric="row_count", column=None)
    assert history(fresh) == ()


def test_default_params_and_source_identity(fresh):
    original = key(fresh)
    config(fresh, threshold=3.0, min_history=None, scope="batch")
    assert key(fresh) == original
    with Session(fresh) as s, s.begin():
        s.get(Dataset, 1).table_name = "different_source"
    assert key(fresh) != original


def test_errors_do_not_stop_other_checks(fresh):
    config(fresh, metric="mean", column="absent")
    with Session(fresh) as s, s.begin():
        s.add(DqCheck(dataset_id=1, name="ordinary", check_type="row_count", params={"min": 1}))
    report = run(fresh)
    assert report.count(ERROR) == 1 and report.count(PASS) == 1
    with Session(fresh) as s:
        assert s.get(Run, report.run_id).finished_at is not None


def test_anomaly_catalog_is_valid_and_idempotent(fresh, tmp_path):
    parse_catalog((REPO / "checks/tpch-anomalies.yaml").read_text(encoding="utf-8"))
    path = tmp_path / "anomaly.yaml"
    path.write_text(
        "connection: tpch_warehouse\nchecks:\n"
        "  - name: volume\n    dataset: dwh.facts\n    type: anomaly\n"
        "    params: {metric: row_count, window: 3}\n",
        encoding="utf-8",
    )
    assert load_catalog(fresh, path).unchanged == ["dwh.facts / volume"]
    assert load_catalog(fresh, path).unchanged == ["dwh.facts / volume"]


def test_cli_skip_returns_zero_but_is_visible(fresh):
    env = os.environ | {"META_DB": TEST_DB, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, "-m", "dq_monitor", "run", "--now", NOW.isoformat()],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0 and "skip 1" in result.stdout
    assert "SKIP" in result.stdout and "Разогрев" in result.stdout


def test_migration_matches_models_and_guards_downgrade(fresh):
    with fresh.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    outcome(fresh)
    with pytest.raises(DBAPIError, match="Нельзя откатить"):
        migrate(fresh, "downgrade", "1ac1478a8e17")
    with fresh.connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "b2e4a6c8d010"
        )


def test_upgrade_preserves_stage3_results_and_empty_downgrade(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE notification, incident, check_result, run, dq_check, "
                "dataset, connection RESTART IDENTITY CASCADE"
            )
        )
    migrate(engine, "downgrade", "1ac1478a8e17")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connection (name, db_type, host, port, database, username, "
                "password_env) VALUES ('old','postgres','h',1,'d','u','P')"
            )
        )
        conn.execute(
            text("INSERT INTO dataset(connection_id,schema_name,table_name) VALUES(1,'s','t')")
        )
        conn.execute(
            text("INSERT INTO dq_check(dataset_id,name,check_type) VALUES(1,'old','row_count')")
        )
        conn.execute(text("INSERT INTO run(logical_now) VALUES(now())"))
        conn.execute(
            text(
                "INSERT INTO check_result(run_id,check_id,status,observed_value) "
                "VALUES(1,1,'pass',42)"
            )
        )
    migrate(engine, "upgrade", "head")
    with Session(engine) as s:
        result = s.get(CheckResult, 1)
        assert result.observed_value == 42 and result.config_hash is None and result.details == {}
