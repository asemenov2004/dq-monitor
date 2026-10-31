"""Коннекторы к контролируемым хранилищам."""

from sqlalchemy import URL

from dq_monitor.config import get_secret
from dq_monitor.connectors.base import Connector
from dq_monitor.connectors.postgres import PostgresConnector
from dq_monitor.meta.models import Connection

__all__ = ["Connector", "PostgresConnector", "connector_for"]


def connector_for(connection: Connection, statement_timeout_sec: int) -> Connector:
    """Создаёт коннектор по записи из таблицы connection. Пароль берётся из окружения."""
    if connection.db_type == "postgres":
        url = URL.create(
            "postgresql+psycopg",
            username=connection.username,
            password=get_secret(connection.password_env),
            host=connection.host,
            port=connection.port,
            database=connection.database,
        )
        return PostgresConnector(url, statement_timeout_sec)
    raise NotImplementedError(f"Коннектор для {connection.db_type} пока не реализован")
