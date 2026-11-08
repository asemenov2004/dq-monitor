"""Формы UI, настоящее ядро, история, XSS/CSRF и независимость запросов на временной БД."""

import re
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from dq_monitor.config import get_settings  # noqa: E402
from dq_monitor.coordination import run_lock  # noqa: E402
from dq_monitor.meta.migrate import upgrade  # noqa: E402
from dq_monitor.meta.models import CheckResult, Connection, Dataset, DqCheck, Incident  # noqa: E402
from dq_monitor.web.app import create_app  # noqa: E402

TEST_DB = f"dq_web_test_{uuid4().hex[:12]}"
NOW = "2025-01-04T02:00"


@pytest.fixture(scope="module")
def web_engine():
    settings = get_settings()
    admin = create_engine(
        settings.meta_url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3}
    )
    try:
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
            c.execute(text(f"CREATE DATABASE {TEST_DB}"))
    except OperationalError:
        pytest.skip("PostgreSQL недоступен")
    engine = create_engine(settings.meta_url.set(database=TEST_DB))
    try:
        upgrade(engine)
        with engine.begin() as c:
            c.execute(text("CREATE SCHEMA dwh"))
            c.execute(text("CREATE SCHEMA stand"))
            c.execute(
                text("CREATE TABLE stand.clock AS SELECT timestamp '2025-01-04 02:00' model_now")
            )
            c.execute(text("CREATE TABLE dwh.customers AS SELECT generate_series(1,5) id"))
            c.execute(
                text(
                    "CREATE TABLE dwh.orders (id int, cust int, price numeric, "
                    "batch_date date, loaded_at timestamp)"
                )
            )
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE {TEST_DB} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture
def client(web_engine):
    settings = get_settings()
    with web_engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE notification,incident,check_result,run,dq_check,dataset,connection,"
                "dwh.orders RESTART IDENTITY CASCADE"
            )
        )
        c.execute(
            text(
                "INSERT INTO dwh.orders SELECT n,n,10,'2025-01-03','2025-01-04 02:00' "
                "FROM generate_series(1,5) n"
            )
        )
    with Session(web_engine) as s, s.begin():
        source = Connection(
            name="tpch_warehouse",
            db_type="postgres",
            host=settings.postgres_host,
            port=settings.postgres_port,
            username=settings.postgres_user,
            database=TEST_DB,
            password_env="POSTGRES_PASSWORD",
        )
        ds = Dataset(
            connection=source,
            schema_name="dwh",
            table_name="orders",
            batch_column="batch_date",
            loaded_at_column="loaded_at",
        )
        s.add(
            DqCheck(
                dataset=ds, name="base_count", check_type="row_count", params={"min": 1, "max": 10}
            )
        )
    with TestClient(create_app(engine=web_engine)) as c:
        yield c


def csrf(client, path="/"):
    page = client.get(path)
    assert page.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', page.text)[1]


def post(client, path, data=None):
    return client.post(path, data={"csrf": csrf(client), **(data or {})})


def check_data(kind="row_count", **params):
    return {
        "name": "created_" + kind,
        "dataset_id": "1",
        "check_type": kind,
        "severity": "critical",
        "is_active": "on",
        **{"p_" + k: str(v) for k, v in params.items()},
    }


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/checks",
        "/checks/new",
        "/checks/1",
        "/checks/1/edit",
        "/sources",
        "/runs",
        "/incidents",
        "/health",
        "/checks?dataset=&status=all",
    ],
)
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "kind,params",
    [
        ("not_null", {"column": "cust"}),
        ("unique", {"columns": "id"}),
        ("freshness", {"max_age_hours": "26"}),
        ("row_count", {"min": "1", "max": "10"}),
        ("range", {"column": "price", "values": "[10,20]"}),
        ("referential", {"column": "cust", "ref_table": "dwh.customers", "ref_column": "id"}),
        ("custom_sql", {"sql": "SELECT 0"}),
        ("anomaly", {"metric": "row_count", "window": "3"}),
    ],
)
def test_all_types_created_and_executed_through_ui(client, web_engine, kind, params):
    response = post(client, "/checks/new", check_data(kind, **params))
    assert response.status_code == 200 and "Проверка сохранена" in response.text
    with Session(web_engine) as s:
        check = s.scalar(select(DqCheck).where(DqCheck.name == "created_" + kind))
        key = check.id
        assert check.is_active and check.params
    result = post(client, "/runs", {"check_id": str(key), "logical_now": NOW})
    assert result.status_code == 200 and "Прогон #" in result.text
    with Session(web_engine) as s:
        saved = s.scalar(select(CheckResult).where(CheckResult.check_id == key))
        assert saved.status == ("skip" if kind == "anomaly" else "pass")
        assert saved.scheduled_for is None


def test_fail_fail_pass_incident_history_and_chart(client, web_engine):
    post(client, "/checks/new", check_data("not_null", column="cust"))
    with web_engine.begin() as c:
        c.execute(text("UPDATE dwh.orders SET cust=NULL WHERE id=1"))
    for _ in range(2):
        response = post(client, "/runs", {"logical_now": NOW})
        assert "FAIL" in response.text
    with Session(web_engine) as s:
        assert s.scalar(select(func.count()).select_from(Incident)) == 1
    assert "ОТКРЫТ" in client.get("/incidents").text
    with web_engine.begin() as c:
        c.execute(text("UPDATE dwh.orders SET cust=id"))
    post(client, "/runs", {"logical_now": NOW})
    with Session(web_engine) as s:
        assert s.scalar(select(Incident.state)) == "closed"
    detail = client.get("/checks/2")
    assert '<svg class="chart"' in detail.text and "metric-dot" in detail.text
    assert "ЗАКРЫТ" in client.get("/incidents?state=closed").text


def test_edit_and_toggle_preserve_results(client, web_engine):
    post(client, "/runs", {"logical_now": NOW})
    data = check_data()
    data.update(name="renamed", p_min="99", p_max="100", schedule="0 2 * * *")
    assert "Проверка сохранена" in post(client, "/checks/1/edit", data).text
    with Session(web_engine) as s:
        check = s.get(DqCheck, 1)
        assert check.name == "renamed" and check.schedule == "0 2 * * *"
        assert s.scalar(select(func.count()).select_from(CheckResult)) == 1
    post(client, "/checks/1/toggle")
    with Session(web_engine) as s:
        assert not s.get(DqCheck, 1).is_active
    response = post(client, "/runs", {"logical_now": NOW})
    assert "Результатов нет" in response.text


@pytest.mark.parametrize(
    "data",
    [
        check_data("row_count", min="10", max="1"),
        check_data("anomaly", metric="mean"),
        check_data("range", column="price", values="bad"),
        check_data("row_count", min="1") | {"schedule": "0 0 31 2 *"},
        check_data("range", column="price", min="NaN"),
        check_data("row_count", min="1", typo="1"),
    ],
)
def test_invalid_forms_do_not_write(client, web_engine, data):
    response = post(client, "/checks/new", data)
    assert response.status_code == 422
    with Session(web_engine) as s:
        assert s.scalar(select(func.count()).select_from(DqCheck)) == 1


def test_duplicate_name_and_missing_dataset(client):
    data = check_data("row_count", min="1") | {"name": "base_count"}
    assert post(client, "/checks/new", data).status_code == 422
    data.update(name="another", dataset_id="999")
    assert post(client, "/checks/new", data).status_code == 404


def test_csrf_required_and_get_cannot_mutate(client, web_engine):
    assert client.post("/checks/1/toggle").status_code == 403
    assert client.post("/runs", data={"csrf": "wrong"}).status_code == 403
    assert client.get("/checks/1/toggle").status_code == 405
    with Session(web_engine) as s:
        assert s.get(DqCheck, 1).is_active


def test_metadata_escaped_and_no_cdn(client):
    name = '<script>alert("x")</script>'
    response = post(client, "/checks/new", check_data("row_count", min="1") | {"name": name})
    assert "<script>alert" not in response.text and "&lt;script&gt;" in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert 'src="https://' not in response.text


def test_registration_probe_and_no_password_in_html(client, web_engine):
    data = {
        "name": "second",
        "host": "localhost",
        "port": "5433",
        "database": TEST_DB,
        "username": get_settings().postgres_user,
        "password_env": "POSTGRES_PASSWORD",
    }
    assert "Подключение добавлено" in post(client, "/sources", data).text
    response = post(
        client, "/datasets", {"connection_id": "2", "schema_name": "dwh", "table_name": "customers"}
    )
    assert "Таблица зарегистрирована" in response.text
    assert get_settings().postgres_password not in response.text
    assert "Подключение работает" in post(client, "/sources/1/test").text
    assert client.get("/api/demo-clock").json()["logical_now"] == NOW


def test_busy_run_returns_notice(client, web_engine):
    with run_lock(web_engine):
        response = post(client, "/runs", {"logical_now": NOW})
    assert "Уже выполняется" in response.text


def test_filters_and_missing_records(client):
    assert client.get("/checks?status=unknown").status_code == 400
    assert client.get("/checks?dataset=bad").status_code == 400
    assert client.get("/checks/999").status_code == 404
    assert client.get("/runs/999").status_code == 404
    assert client.get("/runs?page_number=0").status_code == 422


def test_notification_journal(client, web_engine):
    with web_engine.begin() as c:
        c.execute(text("DELETE FROM dwh.orders"))
    response = post(client, "/runs", {"logical_now": NOW})
    assert "FAIL" in response.text
    response = client.get("/notifications")
    assert response.status_code == 200
    assert "Почта" in response.text and "Mattermost" in response.text
    assert "Ожидает" in response.text and "base_count" in response.text
    assert client.get("/notifications?status=sent").status_code == 200
    assert client.get("/notifications?status=bad").status_code == 400
    assert client.get("/notifications?page_number=0").status_code == 422


def test_demo_prepare_restores_edited_rule(client, web_engine, monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from demo import prepare

    settings = get_settings().model_copy(update={"warehouse_db": TEST_DB, "meta_db": TEST_DB})
    prepare(settings)
    with Session(web_engine) as s, s.begin():
        check = s.scalar(select(DqCheck).where(DqCheck.name == "demo_amount_not_null"))
        check.is_active = False
        check.params = {"scope": "table", "column": "amount", "max_null_ratio": 1}
    prepare(settings)
    with Session(web_engine) as s:
        check = s.scalar(select(DqCheck).where(DqCheck.name == "demo_amount_not_null"))
        assert check.is_active and check.params["max_null_ratio"] == 0
        assert s.scalar(text("SELECT count(*) FROM dq_demo.orders")) == 10
