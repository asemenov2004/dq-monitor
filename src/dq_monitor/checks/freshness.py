"""freshness: данные загружались не раньше, чем N часов назад относительно логического времени."""

from collections.abc import Sequence
from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import Field

from dq_monitor.checks.base import (
    FAIL,
    PASS,
    Check,
    CheckContext,
    CheckParams,
    Outcome,
    Query,
    to_naive_utc,
)


class FreshnessParams(CheckParams):
    max_age_hours: float = Field(gt=0)
    # Столбец времени загрузки; по умолчанию берётся loaded_at_column датасета
    column: str | None = None
    # Свежесть всегда оценивается по всей таблице: важно, когда была последняя загрузка
    scope: Literal["table"] = "table"


class FreshnessCheck(Check):
    type_name = "freshness"
    Params = FreshnessParams
    params: FreshnessParams

    def _column(self, ctx: CheckContext) -> str:
        column = self.params.column or ctx.loaded_at_column
        if column is None:
            raise ValueError(
                "Не задан столбец времени загрузки: укажите column в параметрах "
                "или loaded_at_column у датасета"
            )
        return column

    def build_query(self, ctx: CheckContext) -> Query:
        column = ctx.connector.quote(self._column(ctx))
        # SQL только достаёт последний момент загрузки; возраст считается в Python,
        # потому что арифметика времени в разных СУБД записывается по-разному
        return Query(f"SELECT MAX({column}) FROM {ctx.table_sql}")

    def evaluate(self, row: Sequence[Any], ctx: CheckContext) -> Outcome:
        limit = self.params.max_age_hours
        last = row[0]
        if last is None:
            return Outcome(FAIL, None, None, limit, "В таблице нет ни одной загрузки")
        if not isinstance(last, datetime) and isinstance(last, date):
            last = datetime.combine(last, time.min)
        age_hours = (to_naive_utc(ctx.logical_now) - to_naive_utc(last)).total_seconds() / 3600
        return Outcome(
            FAIL if age_hours > limit else PASS,
            age_hours,
            None,
            limit,
            f"Последняя загрузка {last:%Y-%m-%d %H:%M}, прошло {age_hours:.1f} ч, "
            f"допустимо до {limit:g} ч",
        )
