"""Модель метаданных dq-monitor: что проверять, как, что получилось, какие инциденты открыты.

В отличие от контролируемого хранилища, здесь ограничения целостности заданы полностью:
это база самого приложения, и записать в неё некорректные данные не должно быть возможно.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

DB_TYPES = ("postgres", "clickhouse")
CHECK_TYPES = (
    "not_null",
    "unique",
    "freshness",
    "row_count",
    "range",
    "referential",
    "custom_sql",
    "anomaly",
)
SEVERITIES = ("warning", "critical")
RUN_TRIGGERS = ("manual", "schedule")
RESULT_STATUSES = ("pass", "fail", "error", "skip")
INCIDENT_STATES = ("open", "closed")
NOTIFICATION_CHANNELS = ("telegram",)
NOTIFICATION_EVENTS = ("opened", "closed")
NOTIFICATION_STATUSES = ("sent", "failed")


def one_of(column: str, values: tuple[str, ...]) -> str:
    """Условие CHECK «значение из списка»."""
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


# Единые правила именования ограничений: Alembic ссылается на ограничения по имени,
# без соглашения Postgres придумает имена сам, и их будет трудно менять миграциями
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Все отметки времени с часовым поясом (timestamptz), чтобы не путать время сервера и клиента
    type_annotation_map = {datetime: DateTime(timezone=True)}


class Connection(Base):
    """Подключение к контролируемому хранилищу.

    Пароль в базе не хранится, только имя переменной окружения, где он лежит.
    """

    __tablename__ = "connection"
    __table_args__ = (CheckConstraint(one_of("db_type", DB_TYPES), name="db_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    db_type: Mapped[str] = mapped_column(String(20))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int]
    database: Mapped[str] = mapped_column(String(100))
    username: Mapped[str] = mapped_column(String(100))
    password_env: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    datasets: Mapped[list["Dataset"]] = relationship(back_populates="connection")


class Dataset(Base):
    """Таблица хранилища, на которую навешиваются проверки."""

    __tablename__ = "dataset"
    __table_args__ = (UniqueConstraint("connection_id", "schema_name", "table_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connection.id", ondelete="RESTRICT"))
    schema_name: Mapped[str] = mapped_column(String(100))
    table_name: Mapped[str] = mapped_column(String(100))
    # Столбец партии: проверки считаются по ожидаемой партии, а не по всей таблице
    batch_column: Mapped[str | None] = mapped_column(String(100))
    # Столбец времени загрузки: по нему проверяется свежесть
    loaded_at_column: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    connection: Mapped[Connection] = relationship(back_populates="datasets")
    checks: Mapped[list["DqCheck"]] = relationship(back_populates="dataset")


class DqCheck(Base):
    """Правило проверки качества. Параметры зависят от типа и хранятся в JSONB."""

    __tablename__ = "dq_check"
    __table_args__ = (
        UniqueConstraint("dataset_id", "name"),
        CheckConstraint(one_of("check_type", CHECK_TYPES), name="check_type"),
        CheckConstraint(one_of("severity", SEVERITIES), name="severity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(200))
    check_type: Mapped[str] = mapped_column(String(30))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    severity: Mapped[str] = mapped_column(String(20), server_default="critical")
    # Расписание в формате cron; NULL означает «в каждом прогоне»
    schedule: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    dataset: Mapped[Dataset] = relationship(back_populates="checks")
    results: Mapped[list["CheckResult"]] = relationship(back_populates="check")
    incidents: Mapped[list["Incident"]] = relationship(back_populates="check")


class Run(Base):
    """Один прогон: выполнение набора активных проверок.

    logical_now это «сейчас», с которым выполнялись проверки: в эксперименте
    модельное время полигона, в эксплуатации реальное. started_at и finished_at
    всегда реальное время, по ним считаются накладные расходы.
    """

    __tablename__ = "run"
    __table_args__ = (CheckConstraint(one_of("trigger", RUN_TRIGGERS), name="trigger"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    logical_now: Mapped[datetime]
    trigger: Mapped[str] = mapped_column(String(20), server_default="manual")
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]

    results: Mapped[list["CheckResult"]] = relationship(back_populates="run")


class CheckResult(Base):
    """Результат одной проверки в одном прогоне. observed_value образует историю метрики."""

    __tablename__ = "check_result"
    __table_args__ = (
        UniqueConstraint("run_id", "check_id"),
        CheckConstraint(one_of("status", RESULT_STATUSES), name="status"),
        Index(None, "check_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id", ondelete="RESTRICT"))
    check_id: Mapped[int] = mapped_column(ForeignKey("dq_check.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(10))
    observed_value: Mapped[Decimal | None] = mapped_column(Numeric)
    expected_low: Mapped[Decimal | None] = mapped_column(Numeric)
    expected_high: Mapped[Decimal | None] = mapped_column(Numeric)
    message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None]
    # У старых результатов версии нет: они не используются для обучения детектора
    config_hash: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    run: Mapped[Run] = relationship(back_populates="results")
    check: Mapped[DqCheck] = relationship(back_populates="results")


class Incident(Base):
    """Инцидент живёт от первого падения проверки до первого успешного результата после него."""

    __tablename__ = "incident"
    __table_args__ = (
        CheckConstraint(one_of("state", INCIDENT_STATES), name="state"),
        # Открытый инцидент не имеет даты закрытия, закрытый обязан её иметь
        CheckConstraint(
            "(state = 'open' AND closed_at IS NULL AND closed_result_id IS NULL) OR "
            "(state = 'closed' AND closed_at IS NOT NULL AND closed_result_id IS NOT NULL)",
            name="state_consistency",
        ),
        # Не больше одного открытого инцидента на проверку
        Index(
            "uq_incident_open_per_check",
            "check_id",
            unique=True,
            postgresql_where=text("state = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("dq_check.id", ondelete="RESTRICT"))
    opened_result_id: Mapped[int] = mapped_column(
        ForeignKey("check_result.id", ondelete="RESTRICT")
    )
    closed_result_id: Mapped[int | None] = mapped_column(
        ForeignKey("check_result.id", ondelete="RESTRICT")
    )
    state: Mapped[str] = mapped_column(String(10), server_default="open")
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]

    check: Mapped[DqCheck] = relationship(back_populates="incidents")
    notifications: Mapped[list["Notification"]] = relationship(back_populates="incident")


class Notification(Base):
    """Оповещение об открытии или закрытии инцидента."""

    __tablename__ = "notification"
    __table_args__ = (
        CheckConstraint(one_of("channel", NOTIFICATION_CHANNELS), name="channel"),
        CheckConstraint(one_of("event", NOTIFICATION_EVENTS), name="event"),
        CheckConstraint(one_of("status", NOTIFICATION_STATUSES), name="status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incident.id", ondelete="RESTRICT"))
    channel: Mapped[str] = mapped_column(String(20))
    event: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10))
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="notifications")
