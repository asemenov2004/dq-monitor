"""Обработка durable outbox с гарантией at least once.

Crash boundary: внешний сервис мог принять сообщение, а процесс упасть до commit
статуса sent. После истечения lease возможен дубль. Транзакция БД и SMTP/webhook
не имеют общего commit; exactly once здесь не обещается. Неудачные попытки
фиксируются отдельно, после max_attempts запись остаётся failed без next_attempt_at.
Попытка и lease фиксируются ДО сети: crash расходует попытку. После истечения
последнего lease неподтверждённая запись становится terminal failed. Advisory lock
удерживается во время сети, lease защищает восстановление после падения процесса.
closed ждёт sent у opened того же канала, включая terminal failed у opened:
такое закрытие остаётся pending (blocked), пока открытие не доставлено вручную.
Если opened этого канала отсутствует (инцидент открыт до внедрения outbox), closed
доставляется самостоятельно. История Telegram в упорядочивании новых каналов не участвует.
"""

import argparse
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, exists, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, aliased

from dq_monitor.db import meta_engine
from dq_monitor.meta.models import Notification
from dq_monitor.notifications.settings import NotificationSettings
from dq_monitor.notifications.transports import deliver, sanitized_error

NOTIFICATION_LOCK_KEY = 74261108


class WorkerInProgressError(RuntimeError):
    """В этой базе уже работает другой worker уведомлений."""


@contextmanager
def worker_lock(engine: Engine):
    """Отдельный от run session lock; ORM использует то же соединение."""
    with engine.connect() as conn:
        acquired = conn.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": NOTIFICATION_LOCK_KEY}
        )
        conn.commit()
        if not acquired:
            raise WorkerInProgressError("Уже работает worker уведомлений")
        try:
            yield conn
        finally:
            if not conn.invalidated:
                try:
                    conn.rollback()
                    conn.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": NOTIFICATION_LOCK_KEY}
                    )
                    conn.commit()
                except DBAPIError:
                    conn.invalidate()


def retry_delay(attempt: int, settings: NotificationSettings) -> float:
    """Экспоненциальная задержка с верхней границей, без неограниченного роста."""
    return min(settings.retry_max_sec, settings.retry_base_sec * 2 ** min(attempt - 1, 100))


def process_once(engine: Engine, settings: NotificationSettings | None = None) -> int:
    """Обрабатывает до batch_size готовых записей; возвращает число попыток доставки.

    Выключенные каналы остаются в очереди без расходования попыток. Сетевые ошибки
    записываются в failed; сбои самой БД выходят наружу, чтобы сервис мог перезапуститься.
    """
    settings = settings or NotificationSettings()
    if not settings.enabled:
        return 0
    processed = 0
    opened = aliased(Notification)
    channel_opened = select(opened.id).where(
        opened.incident_id == Notification.incident_id,
        opened.channel == Notification.channel,
        opened.event == "opened",
    )
    with worker_lock(engine) as conn, Session(conn, expire_on_commit=False) as session:
        # Последняя попытка могла принять сообщение, но упасть до фиксации sent.
        # Не оставляем pending с исчерпанным лимитом навсегда.
        session.execute(
            update(Notification)
            .where(
                Notification.channel.in_(settings.channel_names),
                Notification.status.in_(("pending", "failed")),
                Notification.attempts >= settings.max_attempts,
                Notification.next_attempt_at <= datetime.now(UTC),
            )
            .values(
                status="failed",
                next_attempt_at=None,
                error="Исчерпаны попытки доставки; подтверждение не получено",
            )
        )
        session.commit()
        for _ in range(settings.batch_size):
            now = datetime.now(UTC)
            row = session.scalar(
                select(Notification)
                .where(
                    Notification.channel.in_(settings.channel_names),
                    Notification.status.in_(("pending", "failed")),
                    Notification.attempts < settings.max_attempts,
                    Notification.next_attempt_at <= now,
                    (Notification.event == "opened")
                    | ~exists(channel_opened)
                    | exists(channel_opened.where(opened.status == "sent")),
                )
                .order_by(Notification.id)
                .limit(1)
                .with_for_update(of=Notification)
            )
            if row is None:
                break
            row.attempts += 1
            row.last_attempt_at = now
            row.status = "pending"
            row.next_attempt_at = now + timedelta(
                seconds=settings.timeout_sec + retry_delay(row.attempts, settings)
            )
            session.commit()  # crash после этой границы уже расходует попытку
            try:
                deliver(row.channel, row.message, settings)
            except Exception as exc:  # граница транспорта: секреты исключения не сохраняем
                row.status = "failed"
                row.error = sanitized_error(exc)
                row.next_attempt_at = (
                    datetime.now(UTC) + timedelta(seconds=retry_delay(row.attempts, settings))
                    if row.attempts < settings.max_attempts
                    else None
                )
            else:
                row.status = "sent"
                row.sent_at = datetime.now(UTC)
                row.error = None
                row.next_attempt_at = None
            session.commit()
            processed += 1
    return processed


def main(argv: list[str] | None = None) -> int:
    """CLI: по умолчанию loop, --once обрабатывает один пакет очереди."""
    parser = argparse.ArgumentParser(description="Worker уведомлений dq-monitor")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Один пакет очереди")
    mode.add_argument("--loop", action="store_true", help="Непрерывный опрос очереди")
    args = parser.parse_args(argv)
    engine = None
    try:
        settings = NotificationSettings()
        if not settings.enabled:
            print("Уведомления выключены (NOTIFY_ENABLED=false)")
            return 0
        engine = meta_engine()
        while True:
            count = process_once(engine, settings)
            print(f"Попыток доставки: {count}", flush=True)
            if args.once:
                return 0
            time.sleep(settings.poll_interval_sec)
    except KeyboardInterrupt:
        return 0
    except WorkerInProgressError:
        print("Уже работает worker уведомлений", file=sys.stderr)
        return 1
    except Exception:
        # ValidationError тоже содержит входные значения, включая секретные env.
        print("Не удалось запустить worker: проверьте настройки и базу метаданных", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
