"""Дымовые тесты каркаса: пакет импортируется, настройки собирают правильные адреса БД."""

import pydantic
import pytest

from dq_monitor import __version__
from dq_monitor.config import Settings

SETTINGS_ENV_VARS = (
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "WAREHOUSE_DB",
    "META_DB",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Убирает переменные окружения, чтобы тест не зависел от настроек машины."""
    for name in SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make_settings(**overrides) -> Settings:
    values = {"postgres_user": "u", "postgres_password": "p@ss"} | overrides
    return Settings(_env_file=None, **values)


def test_version():
    assert __version__


def test_urls_point_to_separate_databases(clean_env):
    s = make_settings(postgres_port=5433)
    assert s.warehouse_url.database == "warehouse"
    assert s.meta_url.database == "meta"
    assert s.warehouse_url.port == 5433
    # пароль со спецсимволом не ломает строку подключения
    assert s.meta_url.password == "p@ss"


def test_settings_are_immutable(clean_env):
    s = make_settings()
    with pytest.raises(pydantic.ValidationError):
        s.postgres_port = 1
