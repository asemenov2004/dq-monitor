"""Версия метрики и выборка одной последней точки на день UTC без утечки будущего."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from dq_monitor.checks.base import Check
from dq_monitor.meta.models import CheckResult, DqCheck, Run


def fingerprint(check: DqCheck, implementation: Check) -> str:
    """Нормализованные параметры и источник. Изменение смысла проверки начинает новую историю."""
    ds, connection = check.dataset, check.dataset.connection
    payload = {
        "version": 1,
        "type": check.check_type,
        "params": implementation.params.model_dump(mode="json"),
        "dataset": [ds.id, ds.schema_name, ds.table_name, ds.batch_column, ds.loaded_at_column],
        "connection": [
            connection.id,
            connection.db_type,
            connection.host,
            connection.port,
            connection.database,
            connection.username,
            connection.password_env,
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_history(
    session: Session,
    check_id: int,
    key: str,
    logical_now: datetime,
    run_id: int,
    window: int,
) -> tuple[float, ...]:
    """Один день = одна точка и для batch, и для table. Последний результат дня имеет приоритет.

    Числовые SKIP разогрева входят в историю. SKIP без данных и ERROR не входят и не
    заменяются более старым успешным результатом того же дня. FAIL входит в историю.
    Сначала выбираем последнюю попытку, затем пригодные точки и последние window дней.
    """
    day = logical_now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    ranked = (
        select(
            CheckResult.observed_value.label("value"),
            CheckResult.status,
            Run.logical_now,
            CheckResult.id,
            func.row_number()
            .over(
                partition_by=func.date(func.timezone("UTC", Run.logical_now)),
                order_by=CheckResult.id.desc(),
            )
            .label("position"),
        )
        .join(Run, Run.id == CheckResult.run_id)
        .where(
            CheckResult.check_id == check_id,
            CheckResult.config_hash == key,
            Run.logical_now >= day - timedelta(days=window),
            Run.logical_now < day,
            Run.id < run_id,
        )
        .subquery()
    )
    query = (
        select(ranked.c.value)
        .where(
            ranked.c.position == 1,
            ranked.c.status.in_(("pass", "fail", "skip")),
            ranked.c.value.is_not(None),
            cast(ranked.c.value, String).not_in(("NaN", "Infinity", "-Infinity")),
        )
        .order_by(ranked.c.logical_now.desc(), ranked.c.id.desc())
        .limit(window)
    )
    return tuple(reversed([float(n) for n in session.scalars(query)]))
