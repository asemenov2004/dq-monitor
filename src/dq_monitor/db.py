"""Подключение к базам данных через SQLAlchemy."""

from sqlalchemy import URL, Engine, create_engine, text

from dq_monitor.config import get_settings

# Без таймаута libpq ждёт недоступный сервер бесконечно
CONNECT_TIMEOUT_SEC = 5


def _engine(url: URL) -> Engine:
    return create_engine(url, connect_args={"connect_timeout": CONNECT_TIMEOUT_SEC})


def warehouse_engine() -> Engine:
    """Движок для хранилища данных, в котором выполняются проверки."""
    return _engine(get_settings().warehouse_url)


def meta_engine() -> Engine:
    """Движок для базы метаданных dq-monitor."""
    return _engine(get_settings().meta_url)


def server_version(engine: Engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version()")).scalar_one()
