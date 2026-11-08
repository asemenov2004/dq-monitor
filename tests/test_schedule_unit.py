"""UTC cron, границы минут и валидация маршрутизации подключений."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import dq_monitor.connectors as connectors
from dq_monitor.catalog import CatalogError, parse_catalog
from dq_monitor.config import ConnectionRoute, Settings
from dq_monitor.meta.models import Connection
from dq_monitor.scheduling import latest_slot, validate_schedule


@pytest.mark.parametrize("value", ["", "@daily", "* * * * * *", "61 * * * *", "0 0 31 2 *", "bad"])
def test_invalid_cron(value):
    with pytest.raises(ValueError, match="schedule"):
        validate_schedule(value)


def test_valid_cron_and_whitespace():
    assert validate_schedule(" 0  2 * * * ") == "0 2 * * *"
    assert validate_schedule(None) is None
    assert validate_schedule("0 0 29 2 *") == "0 0 29 2 *"


@pytest.mark.parametrize(
    "schedule,now,expected",
    [
        (None, "2026-10-31T12:34:56+00:00", "2026-10-31T12:34:00+00:00"),
        ("0 * * * *", "2026-10-31T12:00:00+00:00", "2026-10-31T12:00:00+00:00"),
        ("0 * * * *", "2026-10-31T12:59:59+00:00", "2026-10-31T12:00:00+00:00"),
        ("0 2 * * *", "2026-10-31T01:59:59+00:00", "2026-10-30T02:00:00+00:00"),
        ("0 0 29 2 *", "2026-10-31T12:00:00+00:00", "2024-02-29T00:00:00+00:00"),
        ("0 0 * * 1", "2026-10-31T12:00:00+00:00", "2026-10-26T00:00:00+00:00"),
    ],
)
def test_cron_slots(schedule, now, expected):
    assert latest_slot(schedule, datetime.fromisoformat(now)) == datetime.fromisoformat(expected)


def test_naive_and_aware_times_use_utc():
    assert latest_slot(None, datetime(2026, 10, 31, 12, 0)) == datetime(
        2026, 10, 31, 12, 0, tzinfo=UTC
    )
    now = datetime(2026, 10, 31, 15, 0, tzinfo=timezone(timedelta(hours=3)))
    assert latest_slot("0 * * * *", now) == datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def test_bad_schedule_rejected_before_catalog_write():
    with pytest.raises(CatalogError, match="schedule"):
        parse_catalog(
            "connection: c\nchecks:\n"
            " - {name: count, dataset: s.t, type: row_count, "
            "params: {min: 1}, schedule: '0 0 31 2 *'}"
        )


def test_connection_routes_validate_host_and_port():
    with pytest.raises(ValidationError):
        ConnectionRoute(host="", port=5432)
    with pytest.raises(ValidationError):
        ConnectionRoute(host="postgres", port=0)
    settings = Settings(
        _env_file=None,
        postgres_user="u",
        postgres_password="p",
        DQ_CONNECTION_OVERRIDES={"c": {"host": "postgres", "port": 5432}},
    )
    assert settings.connection_overrides["c"].host == "postgres"


def test_route_changes_only_network_endpoint(monkeypatch):
    settings = Settings(
        _env_file=None,
        postgres_user="u",
        postgres_password="p",
        DQ_CONNECTION_OVERRIDES={"c": {"host": "postgres", "port": 5432}},
    )
    monkeypatch.setattr(connectors, "get_settings", lambda: settings)
    monkeypatch.setattr(connectors, "get_secret", lambda _: "test_password")
    monkeypatch.setattr(connectors, "PostgresConnector", lambda url, timeout: (url, timeout))
    source = Connection(
        name="c",
        db_type="postgres",
        host="localhost",
        port=5433,
        database="warehouse",
        username="u",
        password_env="P",
    )
    url, timeout = connectors.connector_for(source, 5)
    assert (url.host, url.port, url.database, url.username) == ("postgres", 5432, "warehouse", "u")
    assert timeout == 5 and (source.host, source.port) == ("localhost", 5433)
    source.name = "another"
    url, _ = connectors.connector_for(source, 5)
    assert (url.host, url.port) == ("localhost", 5433)
