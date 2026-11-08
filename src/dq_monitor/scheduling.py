"""Выбор наступивших cron-проверок. Расписание считается в реальном UTC, отдельно от модели."""

from datetime import UTC, datetime, timedelta

from croniter import CroniterBadCronError, CroniterBadDateError, croniter
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from dq_monitor.coordination import run_lock
from dq_monitor.meta.models import CheckResult, DqCheck


def validate_schedule(schedule: str | None) -> str | None:
    """NULL = каждый минутный тик. Cron только из пяти полей, без секунд и макросов."""
    if schedule is None:
        return None
    fields = schedule.split()
    if len(fields) != 5 or not croniter.is_valid(schedule):
        raise ValueError("schedule: нужен корректный cron из 5 полей или null")
    try:
        croniter(schedule, datetime(2000, 1, 1, tzinfo=UTC), max_years_between_matches=8).get_next(
            datetime
        )
    except (CroniterBadCronError, CroniterBadDateError) as exc:
        raise ValueError("schedule: в пределах 8 лет нет подходящей даты") from exc
    return " ".join(fields)


def latest_slot(schedule: str | None, now: datetime) -> datetime:
    """Последняя наступившая минута/cron-дата, включая точное совпадение с now."""
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    if schedule is None:
        return now.replace(second=0, microsecond=0)
    schedule = validate_schedule(schedule)
    try:
        return croniter(
            schedule, now + timedelta(microseconds=1), max_years_between_matches=8
        ).get_prev(datetime)
    except (CroniterBadCronError, CroniterBadDateError) as exc:
        raise ValueError("schedule: в пределах 8 лет нет подходящей даты") from exc


def due_slots(session: Session, now: datetime) -> dict[int, datetime]:
    """Пропущенные интервалы объединяются в один, исторический backfill не выполняется.

    Новая проверка подхватывается на первом тике. FAIL/ERROR считаются обработанной
    попыткой: повтор возможен на следующем интервале, а не каждую минуту одного интервала.
    """
    previous = dict(
        session.execute(
            select(CheckResult.check_id, func.max(CheckResult.scheduled_for))
            .where(CheckResult.scheduled_for.is_not(None))
            .group_by(CheckResult.check_id)
        ).all()
    )
    due = {}
    for check in session.scalars(select(DqCheck).where(DqCheck.is_active).order_by(DqCheck.id)):
        slot = latest_slot(check.schedule, now)
        if previous.get(check.id) is None or previous[check.id] < slot:
            due[check.id] = slot
    return due


def run_due_checks(
    meta_engine: Engine, now: datetime | None = None, logical_now: datetime | None = None
):
    """Точка входа для CLI и Airflow; без наступивших проверок не создаёт пустой run.

    now — реальное время выбора расписаний; logical_now отдельно задаётся только для
    демонстрации на модельных данных. По умолчанию проверка выполняется на now.
    """
    from dq_monitor.runner import _run_checks_locked

    now = now or datetime.now(UTC)
    with run_lock(meta_engine) as connection:
        with Session(connection) as session:
            slots = due_slots(session, now)
        if not slots:
            return None
        return _run_checks_locked(
            meta_engine,
            logical_now or now,
            trigger="schedule",
            check_ids=slots,
            scheduled_slots=slots,
            metadata_connection=connection,
        )
