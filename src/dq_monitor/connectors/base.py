"""Общий интерфейс подключения к контролируемому хранилищу.

Проверки строят SQL на стандартном подмножестве языка и обращаются к СУБД
только через этот интерфейс. Особенности конкретной СУБД (экранирование имён,
способ выполнения запроса) сосредоточены в реализациях коннектора.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any


class Connector(ABC):
    """Подключение к хранилищу. Все запросы выполняются только на чтение."""

    @abstractmethod
    def quote(self, name: str) -> str:
        """Экранирует одно имя (схемы, таблицы или столбца) по правилам СУБД."""

    def table(self, schema: str, table: str) -> str:
        """Полное экранированное имя таблицы: "schema"."table"."""
        return f"{self.quote(schema)}.{self.quote(table)}"

    @abstractmethod
    def fetch_one(self, sql: str, params: Mapping[str, Any]) -> Sequence[Any]:
        """Выполняет запрос и возвращает его единственную строку.

        Значения передаются параметрами (:name), а не подставляются в текст запроса.
        Если запрос вернул не ровно одну строку, возникает ошибка.
        """

    @abstractmethod
    def close(self) -> None:
        """Освобождает соединения."""

    def __enter__(self) -> "Connector":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
