"""Реальные прогоны, расписания, перезапуск и конкуренция процессов в отдельной БД."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from dq_monitor.config import get_settings
from dq_monitor.coordination import RunInProgressError, run_lock
from dq_monitor.meta.models import Base, CheckResult, Connection, Dataset, DqCheck, Incident, Run
from dq_monitor.runner import run_checks
from dq_monitor.scheduling import run_due_checks

TEST_DB = "dq_schedule_test"
NOW = datetime(2026, 10, 31, 12, 5, tzinfo=UTC)
LOGICAL = datetime(2025, 1, 4, 2, tzinfo=UTC)
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
        pytest.skip("PostgreSQL недоступен")
    eng = create_engine(settings.meta_url.set(database=TEST_DB))
    try:
        migrate(eng, "upgrade", "head")
        with eng.begin() as conn:
            conn.execute(text("CREATE SCHEMA dwh"))
            conn.execute(text("CREATE TABLE dwh.facts AS SELECT generate_series(1,5) id"))
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
                "dataset, connection RESTART IDENTITY CASCADE"
            )
        )
    with Session(engine) as s, s.begin():
        connection = Connection(
            name="c",
            db_type="postgres",
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=TEST_DB,
            username=settings.postgres_user,
            password_env="POSTGRES_PASSWORD",
        )
        ds = Dataset(connection=connection, schema_name="dwh", table_name="facts")
        s.add_all(
            [
                DqCheck(
                    dataset=ds,
                    name="hour",
                    check_type="row_count",
                    params={"min": 1},
                    schedule="0 * * * *",
                ),
                DqCheck(dataset=ds, name="minute", check_type="row_count", params={"min": 1}),
            ]
        )
    return engine


def test_due_run_saves_real_slots_and_separate_model_time(fresh):
    report = run_due_checks(fresh, NOW, LOGICAL)
    assert report.ok and len(report.items) == 2
    with Session(fresh) as s:
        assert s.get(Run, report.run_id).logical_now == LOGICAL
        assert s.get(Run, report.run_id).trigger == "schedule"
        slots = s.scalars(select(CheckResult.scheduled_for).order_by(CheckResult.check_id)).all()
    assert slots == [NOW.replace(minute=0), NOW]


def test_same_tick_no_empty_run_and_next_minute_selects_only_minute(fresh):
    first = run_due_checks(fresh, NOW)
    assert run_due_checks(fresh, NOW + timedelta(seconds=20)) is None
    second = run_due_checks(fresh, NOW + timedelta(minutes=1))
    assert [item.check_name for item in second.items] == ["minute"]
    with Session(fresh) as s:
        assert s.scalar(select(func.count()).select_from(Run)) == 2
    assert first.run_id != second.run_id


def test_new_check_is_picked_up_without_restarting_scheduler(fresh):
    run_due_checks(fresh, NOW)
    with Session(fresh) as s, s.begin():
        s.add(
            DqCheck(
                dataset_id=1,
                name="new",
                check_type="row_count",
                params={"min": 1},
                schedule="0 2 * * *",
            )
        )
    report = run_due_checks(fresh, NOW)
    assert [item.check_name for item in report.items] == ["new"]


def test_inactive_and_manual_runs_do_not_advance_cron(fresh):
    with Session(fresh) as s, s.begin():
        s.get(DqCheck, 2).is_active = False
    manual = run_checks(fresh, LOGICAL)
    assert len(manual.items) == 1
    with Session(fresh) as s:
        assert s.scalar(select(CheckResult.scheduled_for)) is None
    assert len(run_due_checks(fresh, NOW).items) == 1


@pytest.mark.parametrize(
    "kind,params,status",
    [("row_count", {"min": 99}, "fail"), ("custom_sql", {"sql": "SELECT missing"}, "error")],
)
def test_fail_and_error_mark_slot_without_duplicate_retries(fresh, kind, params, status):
    with Session(fresh) as s, s.begin():
        check = s.get(DqCheck, 1)
        check.check_type, check.params = kind, params
        s.get(DqCheck, 2).is_active = False
    first = run_due_checks(fresh, NOW)
    assert first.count(status) == 1 and not first.ok
    assert run_due_checks(fresh, NOW) is None
    assert run_due_checks(fresh, NOW + timedelta(hours=1)).count(status) == 1
    if status == "fail":
        with Session(fresh) as s:
            assert s.scalar(select(func.count()).select_from(Incident)) == 1


def test_restart_preserves_processed_slots(fresh):
    run_due_checks(fresh, NOW)
    another_engine = create_engine(get_settings().meta_url.set(database=TEST_DB))
    try:
        assert run_due_checks(another_engine, NOW) is None
    finally:
        another_engine.dispose()


def test_global_lock_blocks_manual_and_scheduler(fresh):
    with run_lock(fresh):
        with pytest.raises(RunInProgressError):
            run_checks(fresh, NOW)
        with pytest.raises(RunInProgressError):
            run_due_checks(fresh, NOW)
    assert run_checks(fresh, NOW).ok


def test_lock_released_after_exception(fresh):
    with pytest.raises(RuntimeError), run_lock(fresh):
        raise RuntimeError("test failure")
    assert run_due_checks(fresh, NOW).ok


def test_global_lock_blocks_another_cli_process(fresh):
    env = os.environ | {"META_DB": TEST_DB, "PYTHONIOENCODING": "utf-8"}
    with run_lock(fresh):
        result = subprocess.run(
            [sys.executable, "-m", "dq_monitor", "run"],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
    assert result.returncode == 1 and "Уже выполняется" in result.stderr


def test_lost_lock_backend_cannot_continue_writing_metadata(fresh):
    with run_lock(fresh) as connection:
        pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        connection.commit()
        with fresh.connect() as killer:
            killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        with pytest.raises(DBAPIError), Session(connection) as session:
            session.add(Run(logical_now=NOW))
            session.commit()
    assert run_checks(fresh, NOW).ok


def test_missed_intervals_coalesce_and_no_backwards_replay(fresh):
    run_due_checks(fresh, NOW)
    future = NOW + timedelta(days=10)
    report = run_due_checks(fresh, future)
    assert len(report.items) == 2
    assert run_due_checks(fresh, NOW) is None


def test_unique_slot_and_migration_roundtrip(fresh):
    run_due_checks(fresh, NOW)
    with pytest.raises(IntegrityError), Session(fresh) as s, s.begin():
        s.add(
            CheckResult(
                run=Run(logical_now=NOW),
                check_id=1,
                status="pass",
                scheduled_for=NOW.replace(minute=0),
            )
        )
    migrate(fresh, "downgrade", "b2e4a6c8d010")
    migrate(fresh, "upgrade", "head")
    with fresh.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
