"""range: значения столбца в допустимых границах и (или) из списка допустимых значений.

Пустые значения (NULL) не считаются нарушением: за них отвечает проверка not_null.
"""

from collections.abc import Sequence
from typing import Any, Self

from pydantic import Field, model_validator

from dq_monitor.checks.base import (
    FAIL,
    PASS,
    Check,
    CheckContext,
    CheckParams,
    Outcome,
    Query,
)


class RangeParams(CheckParams):
    column: str
    min: float | None = None
    max: float | None = None
    # Список допустимых значений (accepted values), например статусы заказа
    values: list[str | int | float] | None = Field(None, min_length=1)
    max_bad_ratio: float = Field(0.0, ge=0, le=1)

    @model_validator(mode="after")
    def check_rule(self) -> Self:
        if self.min is None and self.max is None and self.values is None:
            raise ValueError("нужно задать min, max или values")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min больше max")
        return self


class RangeCheck(Check):
    type_name = "range"
    Params = RangeParams
    params: RangeParams

    def build_query(self, ctx: CheckContext) -> Query:
        column = ctx.connector.quote(self.params.column)
        violations, params = [], {}
        if self.params.min is not None:
            violations.append(f"{column} < :min_value")
            params["min_value"] = self.params.min
        if self.params.max is not None:
            violations.append(f"{column} > :max_value")
            params["max_value"] = self.params.max
        if self.params.values is not None:
            violations.append(f"{column} NOT IN :allowed_values")
            params["allowed_values"] = list(self.params.values)

        condition, batch_params = self.batch_filter(ctx)
        sql = (
            "SELECT COUNT(*), "
            f"SUM(CASE WHEN {column} IS NOT NULL AND ({' OR '.join(violations)}) "
            "THEN 1 ELSE 0 END) "
            f"FROM {ctx.table_sql}{self.where(condition)}"
        )
        return Query(sql, params | batch_params)

    def _rule(self) -> str:
        parts = []
        if self.params.min is not None:
            parts.append(f">= {self.params.min:g}")
        if self.params.max is not None:
            parts.append(f"<= {self.params.max:g}")
        if self.params.values is not None:
            parts.append("из {" + ", ".join(map(str, self.params.values)) + "}")
        return " и ".join(parts)

    def evaluate(self, row: Sequence[Any], ctx: CheckContext) -> Outcome:
        total, bad = row[0], row[1] or 0  # SUM по пустой выборке даёт NULL
        limit = self.params.max_bad_ratio
        if total == 0:
            return Outcome(PASS, None, 0.0, limit, "Нет строк для проверки")
        ratio = bad / total
        return Outcome(
            FAIL if ratio > limit else PASS,
            ratio,
            0.0,
            limit,
            f"Значения {self.params.column} вне правила ({self._rule()}): "
            f"{bad} из {total} ({ratio:.2%}), допустимо до {limit:.2%}",
        )
