"""Базовые понятия ядра проверок: контекст, запрос, итог и абстрактная проверка.

Каждый тип проверки задаёт три вещи:
  1. модель параметров (pydantic): неверные параметры отклоняются до выполнения;
  2. построение SQL-запроса: стандартный SQL, имена экранирует коннектор,
     значения передаются параметрами;
  3. оценку: из результата запроса получается наблюдаемое значение и статус.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from dq_monitor.connectors.base import Connector

PASS = "pass"
FAIL = "fail"
ERROR = "error"


def to_naive_utc(moment: datetime) -> datetime:
    """Приводит момент времени к UTC без часового пояса.

    Отметки времени в хранилище обычно без пояса (timestamp), а logical_now
    может прийти с поясом. Сравнивать их можно только в одной системе отсчёта;
    время без пояса считается временем UTC.
    """
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class CheckContext:
    """Всё, что проверке нужно знать о месте и моменте выполнения."""

    connector: Connector
    schema: str
    table: str
    batch_column: str | None
    loaded_at_column: str | None
    logical_now: datetime

    @property
    def batch_date(self) -> date:
        """Ожидаемая партия: за вчерашний день относительно логического времени.

        Проверяется именно ожидаемая, а не последняя загруженная партия:
        если партия не пришла, проверка объёма увидит 0 строк.
        """
        return to_naive_utc(self.logical_now).date() - timedelta(days=1)

    @property
    def table_sql(self) -> str:
        return self.connector.table(self.schema, self.table)


@dataclass(frozen=True)
class Query:
    sql: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    """Итог проверки: статус, наблюдаемое значение и допустимый коридор."""

    status: str
    observed: float | None = None
    expected_low: float | None = None
    expected_high: float | None = None
    message: str = ""


class CheckParams(BaseModel):
    """Общие параметры всех проверок. Лишние ключи запрещены, чтобы опечатка не прошла молча."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # batch: только ожидаемая партия (если у таблицы есть столбец партии), table: вся таблица
    scope: Literal["batch", "table"] = "batch"


class Check(ABC):
    """Абстрактная проверка. Конкретный тип задаёт type_name, Params, build_query и evaluate."""

    type_name: ClassVar[str]
    Params: ClassVar[type[CheckParams]] = CheckParams

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = self.Params.model_validate(params)

    @abstractmethod
    def build_query(self, ctx: CheckContext) -> Query:
        """Строит SQL-запрос, возвращающий одну строку."""

    @abstractmethod
    def evaluate(self, row: Sequence[Any], ctx: CheckContext) -> Outcome:
        """Превращает строку результата в итог проверки."""

    def run(self, ctx: CheckContext) -> Outcome:
        query = self.build_query(ctx)
        row = ctx.connector.fetch_one(query.sql, query.params)
        return self.evaluate(row, ctx)

    # Общие помощники для конкретных проверок

    def batch_filter(self, ctx: CheckContext, alias: str | None = None) -> tuple[str, dict]:
        """Условие WHERE для ожидаемой партии.

        Пустое, если выбрана вся таблица или у таблицы нет столбца партии (справочник).
        """
        if self.params.scope == "table" or ctx.batch_column is None:
            return "", {}
        column = ctx.connector.quote(ctx.batch_column)
        if alias:
            column = f"{alias}.{column}"
        return f"{column} = :batch_date", {"batch_date": ctx.batch_date}

    @staticmethod
    def where(*conditions: str) -> str:
        """Склеивает непустые условия в предложение WHERE."""
        present = [c for c in conditions if c]
        return f" WHERE {' AND '.join(present)}" if present else ""


def as_float(value: Any) -> float | None:
    """Число из результата запроса (Decimal, int) в float; None остаётся None."""
    return None if value is None else float(value)
