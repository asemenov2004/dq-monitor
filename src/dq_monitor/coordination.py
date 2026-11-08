"""Общая блокировка прогонов в PostgreSQL, работающая между процессами и контейнерами."""

from contextlib import contextmanager

from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

RUN_LOCK_KEY = 74261031


class RunInProgressError(RuntimeError):
    """Другой процесс уже выполняет прогон в этой базе метаданных."""


@contextmanager
def run_lock(engine: Engine):
    # Это же соединение используется для метаданных: потеря backend остановит запись результатов.
    with engine.connect() as conn:
        acquired = conn.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": RUN_LOCK_KEY}
        ).scalar_one()
        conn.commit()
        if not acquired:
            raise RunInProgressError("Уже выполняется другой прогон проверок")
        try:
            yield conn
        finally:
            if not conn.invalidated:
                try:
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": RUN_LOCK_KEY})
                    conn.commit()
                except DBAPIError:
                    # Backend уже завершён: его advisory lock также освобождён.
                    conn.invalidate()
