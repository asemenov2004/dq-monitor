"""Прогон: выполнение активных проверок, сохранение результатов и ведение инцидентов.

Порядок прогона:
  1. создаётся запись run с логическим временем (logical_now);
  2. берутся активные проверки вместе с датасетом и подключением;
  3. каждая проверка выполняется отдельно: исключение не останавливает прогон,
     а превращается в результат со статусом error;
  4. результат сохраняется, инцидент проверки открывается или закрывается;
  5. в run записывается время окончания.

Результат каждой проверки фиксируется сразу: если прогон прервётся, уже
полученные результаты и инциденты не потеряются.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import Connection as SQLConnection
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, joinedload

from dq_monitor.anomalies.history import fingerprint, load_history
from dq_monitor.checks import ERROR, FAIL, PASS, SKIP, CheckContext, Outcome, create_check
from dq_monitor.config import get_settings
from dq_monitor.connectors import Connector, connector_for
from dq_monitor.coordination import run_lock
from dq_monitor.errors import error_text
from dq_monitor.meta.models import CheckResult, Dataset, DqCheck, Incident, Run

MAX_MESSAGE_LENGTH = 1000


@dataclass(frozen=True)
class ResultItem:
    """Строка отчёта о прогоне: одна проверка."""

    check_id: int
    check_name: str
    dataset: str
    check_type: str
    severity: str
    outcome: Outcome
    duration_ms: int
    incident_event: str | None  # "opened", "closed" или None


@dataclass(frozen=True)
class RunReport:
    run_id: int
    logical_now: datetime
    started_at: datetime
    finished_at: datetime
    items: list[ResultItem]

    def count(self, status: str) -> int:
        return sum(1 for item in self.items if item.outcome.status == status)

    def incidents(self, event: str) -> int:
        return sum(1 for item in self.items if item.incident_event == event)

    @property
    def ok(self) -> bool:
        """Нет нарушений и ошибок. Пропуски отдельно видны в отчёте и не ломают разогрев."""
        return all(item.outcome.status in (PASS, SKIP) for item in self.items)

    @property
    def duration_sec(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def as_aware_utc(moment: datetime) -> datetime:
    """Время без пояса считается временем UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def run_checks(
    meta_engine: Engine,
    logical_now: datetime,
    trigger: str = "manual",
    check_ids: Iterable[int] | None = None,
    statement_timeout_sec: int | None = None,
) -> RunReport:
    """Выполняет активные проверки (или только перечисленные) и возвращает отчёт."""
    with run_lock(meta_engine) as connection:
        return _run_checks_locked(
            meta_engine,
            logical_now,
            trigger,
            check_ids,
            statement_timeout_sec,
            metadata_connection=connection,
        )


def _run_checks_locked(
    meta_engine: Engine,
    logical_now: datetime,
    trigger: str = "manual",
    check_ids: Iterable[int] | None = None,
    statement_timeout_sec: int | None = None,
    scheduled_slots: dict[int, datetime] | None = None,
    metadata_connection: SQLConnection | None = None,
) -> RunReport:
    """Внутренний прогон: вызывающий удерживает общую блокировку."""
    logical_now = as_aware_utc(logical_now)
    timeout = statement_timeout_sec or get_settings().check_statement_timeout_sec
    connectors: dict[int, Connector] = {}
    items: list[ResultItem] = []

    # expire_on_commit=False: после фиксации объекты остаются доступны для чтения
    with Session(metadata_connection or meta_engine, expire_on_commit=False) as session:
        run = Run(logical_now=logical_now, trigger=trigger, started_at=datetime.now(UTC))
        session.add(run)
        session.commit()

        query = (
            select(DqCheck)
            .where(DqCheck.is_active)
            .options(joinedload(DqCheck.dataset).joinedload(Dataset.connection))
            .order_by(DqCheck.id)
        )
        if check_ids is not None:
            query = query.where(DqCheck.id.in_(list(check_ids)))
        checks = session.scalars(query).all()

        try:
            for check in checks:
                outcome, duration_ms, config_hash = _execute(
                    check, connectors, timeout, logical_now, session, run.id
                )
                result = CheckResult(
                    run_id=run.id,
                    check_id=check.id,
                    status=outcome.status,
                    observed_value=outcome.observed,
                    expected_low=outcome.expected_low,
                    expected_high=outcome.expected_high,
                    message=outcome.message[:MAX_MESSAGE_LENGTH],
                    duration_ms=duration_ms,
                    config_hash=config_hash,
                    details=outcome.details,
                    scheduled_for=(scheduled_slots or {}).get(check.id),
                )
                session.add(result)
                session.flush()  # нужен id результата для инцидента
                event = update_incident(session, check.id, result, logical_now)
                session.commit()

                dataset = check.dataset
                items.append(
                    ResultItem(
                        check.id,
                        check.name,
                        f"{dataset.schema_name}.{dataset.table_name}",
                        check.check_type,
                        check.severity,
                        outcome,
                        duration_ms,
                        event,
                    )
                )
        finally:
            for connector in connectors.values():
                connector.close()

        run.finished_at = datetime.now(UTC)
        session.commit()

    return RunReport(run.id, logical_now, run.started_at, run.finished_at, items)


def _execute(
    check: DqCheck,
    connectors: dict[int, Connector],
    timeout: int,
    logical_now: datetime,
    session: Session,
    run_id: int,
) -> tuple[Outcome, int, str | None]:
    """Выполняет одну проверку. Любая ошибка превращается в итог со статусом error."""
    started = perf_counter()
    config_hash = None
    try:
        implementation = create_check(check.check_type, check.params)
        dataset = check.dataset
        config_hash = fingerprint(check, implementation)
        history = ()
        if implementation.history_limit:
            history = load_history(
                session, check.id, config_hash, logical_now, run_id, implementation.history_limit
            )
        connector = connectors.get(dataset.connection_id)
        if connector is None:
            connector = connector_for(dataset.connection, timeout)
            connectors[dataset.connection_id] = connector
        ctx = CheckContext(
            connector=connector,
            schema=dataset.schema_name,
            table=dataset.table_name,
            batch_column=dataset.batch_column,
            loaded_at_column=dataset.loaded_at_column,
            logical_now=logical_now,
            history=history,
        )
        outcome = implementation.run(ctx)
    except Exception as exc:  # граница проверки: ошибка одной проверки не роняет прогон
        outcome = Outcome(ERROR, message=error_text(exc))
    return outcome, round((perf_counter() - started) * 1000), config_hash


def update_incident(
    session: Session, check_id: int, result: CheckResult, logical_now: datetime
) -> str | None:
    """Открывает инцидент на первом нарушении и закрывает на первом успешном результате.

    Ошибка выполнения (error) инцидент не меняет: про качество данных она ничего не говорит.
    Время открытия и закрытия логическое, чтобы задержки считались в модельном времени.
    """
    from dq_monitor.notifications.outbox import queue_event

    incident = session.scalar(
        select(Incident).where(Incident.check_id == check_id, Incident.state == "open")
    )
    if result.status == FAIL and incident is None:
        incident = Incident(check_id=check_id, opened_result_id=result.id, opened_at=logical_now)
        session.add(incident)
        session.flush()
        queue_event(session, incident, "opened", result)
        return "opened"
    if result.status == PASS and incident is not None:
        incident.state = "closed"
        incident.closed_result_id = result.id
        incident.closed_at = logical_now
        queue_event(session, incident, "closed", result)
        return "closed"
    return None
