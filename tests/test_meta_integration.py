"""База метаданных: миграции, ограничения целостности, регистрация полигона. Нужен PostgreSQL."""

from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, delete, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from dq_monitor.config import get_settings
from dq_monitor.meta.models import Base, CheckResult, Connection, Dataset, DqCheck, Incident, Run
from dq_stand.register import register_tpch

TEST_DB = "dq_meta_test"
TABLES = {"connection", "dataset", "dq_check", "run", "check_result", "incident", "notification"}
NOW = datetime(2025, 1, 2, 2, 0, tzinfo=UTC)


def alembic(engine, action: str, target: str) -> None:
    cfg = Config()
    cfg.set_main_option("script_location", "dq_monitor.meta:migrations")
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        getattr(command, action)(cfg, target)


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

    test_engine = create_engine(settings.meta_url.set(database=TEST_DB))
    alembic(test_engine, "upgrade", "head")
    yield test_engine

    test_engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE {TEST_DB} WITH (FORCE)"))
    admin.dispose()


@pytest.fixture
def check(engine):
    """Проверка с одним прогоном и результатом; после теста всё удаляется."""
    with Session(engine) as s, s.begin():
        conn = Connection(
            name="t",
            db_type="postgres",
            host="h",
            port=1,
            database="d",
            username="u",
            password_env="P",
        )
        ds = Dataset(connection=conn, schema_name="dwh", table_name="orders")
        chk = DqCheck(dataset=ds, name="nn", check_type="not_null", params={"column": "x"})
        run = Run(logical_now=NOW)
        s.add_all([conn, ds, chk, run, CheckResult(run=run, check=chk, status="fail")])
        s.flush()
        check_id = chk.id  # после коммита объект «отвязан» от сессии, id читаем заранее
    yield check_id

    with Session(engine) as s, s.begin():
        for model in (Incident, CheckResult, Run, DqCheck, Dataset, Connection):
            s.execute(delete(model))


def test_all_tables_created(engine):
    assert TABLES <= set(inspect(engine).get_table_names())


def test_models_match_migrations(engine):
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_invalid_status_rejected(engine, check):
    with pytest.raises(IntegrityError), Session(engine) as s, s.begin():
        run = Run(logical_now=NOW)
        s.add_all([run, CheckResult(run=run, check_id=check, status="foo")])


def test_check_with_history_cannot_be_deleted(engine, check):
    with pytest.raises(IntegrityError), Session(engine) as s, s.begin():
        s.execute(delete(DqCheck).where(DqCheck.id == check))


def test_only_one_open_incident_per_check(engine, check):
    with Session(engine) as s:
        result_id = s.scalar(text("SELECT id FROM check_result LIMIT 1"))
    with Session(engine) as s, s.begin():
        s.add(Incident(check_id=check, opened_result_id=result_id, opened_at=NOW))
    with pytest.raises(IntegrityError), Session(engine) as s, s.begin():
        s.add(Incident(check_id=check, opened_result_id=result_id, opened_at=NOW))


def test_closed_incident_requires_close_time(engine, check):
    with Session(engine) as s:
        result_id = s.scalar(text("SELECT id FROM check_result LIMIT 1"))
    with pytest.raises(IntegrityError), Session(engine) as s, s.begin():
        s.add(Incident(check_id=check, opened_result_id=result_id, opened_at=NOW, state="closed"))


def test_register_is_idempotent(engine):
    settings = get_settings()
    assert register_tpch(engine, settings) == (8, 0)
    assert register_tpch(engine, settings) == (0, 8)
    with Session(engine) as s, s.begin():
        s.execute(delete(Dataset))
        s.execute(delete(Connection))


def test_downgrade_removes_everything(engine):
    alembic(engine, "downgrade", "base")
    assert not TABLES & set(inspect(engine).get_table_names())
    alembic(engine, "upgrade", "head")
