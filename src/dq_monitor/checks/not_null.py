"""not_null: доля пустых значений в столбце не выше порога."""

from collections.abc import Sequence
from typing import Any

from pydantic import Field

from dq_monitor.checks.base import FAIL, PASS, Check, CheckContext, CheckParams, Outcome, Query


class NotNullParams(CheckParams):
    column: str
    max_null_ratio: float = Field(0.0, ge=0, le=1)


class NotNullCheck(Check):
    type_name = "not_null"
    Params = NotNullParams
    params: NotNullParams

    def build_query(self, ctx: CheckContext) -> Query:
        column = ctx.connector.quote(self.params.column)
        condition, params = self.batch_filter(ctx)
        # COUNT(столбец) не считает NULL, разность с COUNT(*) даёт число пустых значений
        sql = (
            f"SELECT COUNT(*), COUNT(*) - COUNT({column}) "
            f"FROM {ctx.table_sql}{self.where(condition)}"
        )
        return Query(sql, params)

    def evaluate(self, row: Sequence[Any], ctx: CheckContext) -> Outcome:
        total, nulls = row
        limit = self.params.max_null_ratio
        if total == 0:
            return Outcome(PASS, None, 0.0, limit, "Нет строк для проверки")
        ratio = nulls / total
        return Outcome(
            FAIL if ratio > limit else PASS,
            ratio,
            0.0,
            limit,
            f"NULL в {self.params.column}: {nulls} из {total} ({ratio:.2%}), "
            f"допустимо до {limit:.2%}",
        )
