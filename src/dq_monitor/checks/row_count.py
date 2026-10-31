"""row_count: число строк (в партии или в таблице) в заданном диапазоне."""

from collections.abc import Sequence
from typing import Any, Self

from pydantic import Field, model_validator

from dq_monitor.checks.base import FAIL, PASS, Check, CheckContext, CheckParams, Outcome, Query


class RowCountParams(CheckParams):
    min: int | None = Field(None, ge=0)
    max: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def check_bounds(self) -> Self:
        if self.min is None and self.max is None:
            raise ValueError("нужна хотя бы одна граница: min или max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min больше max")
        return self


class RowCountCheck(Check):
    type_name = "row_count"
    Params = RowCountParams
    params: RowCountParams

    def build_query(self, ctx: CheckContext) -> Query:
        condition, params = self.batch_filter(ctx)
        return Query(f"SELECT COUNT(*) FROM {ctx.table_sql}{self.where(condition)}", params)

    def evaluate(self, row: Sequence[Any], ctx: CheckContext) -> Outcome:
        rows = int(row[0])
        low, high = self.params.min, self.params.max
        too_few = low is not None and rows < low
        too_many = high is not None and rows > high
        bounds = f"от {low if low is not None else '-'} до {high if high is not None else '-'}"
        return Outcome(
            FAIL if too_few or too_many else PASS,
            rows,
            low,
            high,
            f"Строк: {rows}, допустимо {bounds}",
        )
