"""Запись событий в транзакцию инцидента, без сети и настроек доставки."""

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from dq_monitor.meta.models import CheckResult, DqCheck, Incident, Notification

CHANNELS = ("email", "mattermost")


def queue_event(session: Session, incident: Incident, event: str, result: CheckResult) -> None:
    """Сохраняет по одному сообщению на канал; commit выполняет вызывающий код."""
    if event not in ("opened", "closed"):
        raise ValueError("Неизвестное событие инцидента")
    check = session.get(DqCheck, incident.check_id)
    dataset = check.dataset
    moment = incident.opened_at if event == "opened" else incident.closed_at
    message = (
        f"DQ incident #{incident.id}: {event}\n"
        f"Проверка: {check.name} (#{check.id})\n"
        f"Датасет: {dataset.schema_name}.{dataset.table_name}\n"
        f"Важность: {check.severity}\n"
        f"Логическое время: {moment.isoformat()}\n"
        f"Результат #{result.id}: {result.message or result.status}"
    )
    session.execute(
        insert(Notification)
        .values(
            [
                {"incident_id": incident.id, "channel": channel, "event": event, "message": message}
                for channel in CHANNELS
            ]
        )
        .on_conflict_do_nothing(
            index_elements=["incident_id", "channel", "event"],
            index_where=text("channel IN ('email', 'mattermost')"),
        )
    )
