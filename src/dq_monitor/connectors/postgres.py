"""Коннектор PostgreSQL."""

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import URL, bindparam, create_engine, text

from dq_monitor.connectors.base import Connector
from dq_monitor.db import CONNECT_TIMEOUT_SEC


class PostgresConnector(Connector):
    def __init__(self, url: URL, statement_timeout_sec: int) -> None:
        # Режим «только чтение» и таймаут задаются параметрами сеанса при подключении:
        # их применяет сервер к каждой транзакции, обойти их из текста проверки нельзя
        options = (
            "-c default_transaction_read_only=on "
            f"-c statement_timeout={statement_timeout_sec * 1000}"
        )
        self._engine = create_engine(
            url, connect_args={"connect_timeout": CONNECT_TIMEOUT_SEC, "options": options}
        )
        self._preparer = self._engine.dialect.identifier_preparer

    def quote(self, name: str) -> str:
        # quote_identifier всегда берёт имя в двойные кавычки и удваивает кавычки внутри
        return self._preparer.quote_identifier(name)

    def fetch_one(self, sql: str, params: Mapping[str, Any]) -> Sequence[Any]:
        statement = text(sql)
        # Списки (например, допустимые значения для IN) раскрываются в отдельные параметры
        lists = [name for name, value in params.items() if isinstance(value, list | tuple)]
        if lists:
            statement = statement.bindparams(*(bindparam(n, expanding=True) for n in lists))

        # Транзакция не фиксируется: при выходе из блока она откатывается
        with self._engine.connect() as conn:
            rows = conn.execute(statement, dict(params)).fetchall()
        if len(rows) != 1:
            raise ValueError(f"Запрос должен вернуть одну строку, вернул: {len(rows)}")
        return tuple(rows[0])

    def close(self) -> None:
        self._engine.dispose()
