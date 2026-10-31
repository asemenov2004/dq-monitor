"""Ядро проверок качества данных: правила, аномалии и реестр «тип → класс»."""

from typing import Any

from dq_monitor.checks.anomaly import AnomalyCheck
from dq_monitor.checks.base import ERROR, FAIL, PASS, SKIP, Check, CheckContext, Outcome, Query
from dq_monitor.checks.custom_sql import CustomSqlCheck
from dq_monitor.checks.freshness import FreshnessCheck
from dq_monitor.checks.not_null import NotNullCheck
from dq_monitor.checks.referential import ReferentialCheck
from dq_monitor.checks.row_count import RowCountCheck
from dq_monitor.checks.unique import UniqueCheck
from dq_monitor.checks.value_range import RangeCheck

CHECK_CLASSES: dict[str, type[Check]] = {
    cls.type_name: cls
    for cls in (
        NotNullCheck,
        UniqueCheck,
        FreshnessCheck,
        RowCountCheck,
        RangeCheck,
        ReferentialCheck,
        CustomSqlCheck,
        AnomalyCheck,
    )
}

__all__ = [
    "CHECK_CLASSES",
    "ERROR",
    "FAIL",
    "PASS",
    "SKIP",
    "Check",
    "CheckContext",
    "Outcome",
    "Query",
    "create_check",
]


def create_check(check_type: str, params: dict[str, Any]) -> Check:
    """Создаёт проверку нужного типа; параметры проверяются моделью этого типа."""
    try:
        cls = CHECK_CLASSES[check_type]
    except KeyError:
        raise ValueError(f"Неизвестный тип проверки: {check_type}") from None
    return cls(params)
