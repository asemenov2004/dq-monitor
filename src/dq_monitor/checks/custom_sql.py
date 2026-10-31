"""custom_sql: произвольный запрос, возвращающий одно число «плохих» строк.

В запросе доступны параметры :batch_date (ожидаемая партия) и :logical_now.
Запрос выполняется в транзакции только на чтение и с ограничением времени
(см. коннектор), поэтому изменить данные или подвесить прогон он не может.
"""

from collections.abc import Sequence
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
    as_float,
    to_naive_utc,
)


class CustomSqlParams(CheckParams):
    sql: str = Field(min_length=1)
    max_bad: float = Field(0, ge=0)
    # Область задаёт сам запрос через :batch_date, общий параметр scope здесь не действует
    scope: Literal["table"] = "table"


class CustomSqlCheck(Check):
    type_name = "custom_sql"
    Params = CustomSqlParams
    params: CustomSqlParams

    def build_query(self, ctx: CheckContext) -> Query:
        params = {"batch_date": ctx.batch_date, "logical_now": to_naive_utc(ctx.logical_now)}
        return Query(self.params.sql.strip().rstrip(";"), params)

    def evaluate(self, row: Sequence[Any], ctx: CheckContext) -> Outcome:
        if len(row) != 1:
            raise ValueError(f"Запрос должен вернуть один столбец, вернул: {len(row)}")
        bad = as_float(row[0]) or 0.0  # NULL (например, SUM по пустой выборке) считаем нулём
        limit = self.params.max_bad
        return Outcome(
            FAIL if bad > limit else PASS,
            bad,
            0.0,
            limit,
            f"Запрос вернул {bad:g}, допустимо до {limit:g}",
        )
