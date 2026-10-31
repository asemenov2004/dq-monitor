"""Измерение объёма, доли NULL или среднего и оценка относительно истории."""

import math
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from dq_monitor.anomalies.detector import detect
from dq_monitor.checks.base import (
    FAIL,
    PASS,
    SKIP,
    Check,
    CheckContext,
    CheckParams,
    Outcome,
    Query,
)


class AnomalyParams(CheckParams):
    metric: Literal["row_count", "null_ratio", "mean"]
    column: str | None = Field(None, min_length=1)
    window: int = Field(30, ge=2, le=1000)
    threshold: float = Field(3.0, gt=0, allow_inf_nan=False)
    min_history: int | None = Field(None, ge=2)

    @model_validator(mode="after")
    def validate_metric(self) -> Self:
        if self.metric != "row_count" and self.column is None:
            raise ValueError("Для null_ratio и mean нужен column")
        if self.metric == "row_count" and self.column is not None:
            raise ValueError("Для row_count параметр column не используется")
        if self.min_history is not None and self.min_history > self.window:
            raise ValueError("min_history больше window")
        return self


class AnomalyCheck(Check):
    type_name = "anomaly"
    Params = AnomalyParams
    params: AnomalyParams

    @property
    def history_limit(self) -> int:
        return self.params.window

    def build_query(self, ctx: CheckContext) -> Query:
        condition, params = self.batch_filter(ctx)
        if self.params.metric == "row_count":
            expression = "COUNT(*)"
        else:
            col = ctx.connector.quote(self.params.column)
            expression = (
                f"COUNT(*), COUNT(*) - COUNT({col})"
                if self.params.metric == "null_ratio"
                else f"AVG({col})"
            )
        return Query(f"SELECT {expression} FROM {ctx.table_sql}{self.where(condition)}", params)

    def evaluate(self, row: Sequence[Any], ctx: CheckContext) -> Outcome:
        if self.params.metric == "null_ratio":
            value = row[1] / row[0] if row[0] else None
        else:
            value = None if row[0] is None else float(row[0])
        if value is None:
            return Outcome(
                SKIP,
                message="Нет значений для вычисления метрики",
                details={"reason": "no_data", "metric": self.params.metric},
            )
        if not math.isfinite(value):
            raise ValueError("Метрика должна быть конечным числом")
        decision = detect(
            value, ctx.history, self.params.window, self.params.threshold, self.params.min_history
        )
        details = asdict(decision) | {
            "metric": self.params.metric,
            "window": self.params.window,
            "threshold": self.params.threshold,
        }
        if not decision.ready:
            required = self.params.min_history or self.params.window
            return Outcome(
                SKIP,
                value,
                message=f"Разогрев: история {decision.count}/{required}",
                details=details | {"reason": "warmup"},
            )
        z = "изменение постоянного ряда" if decision.constant_shift else f"z={decision.z_score:.4g}"
        return Outcome(
            FAIL if decision.anomalous else PASS,
            value,
            decision.low,
            decision.high,
            f"{self.params.metric}: {value:g}; {z}; история {decision.count}",
            details,
        )
