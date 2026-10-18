"""Окружение Alembic: откуда брать подключение к базе метаданных и описание моделей."""

from alembic import context

from dq_monitor.config import get_settings
from dq_monitor.db import meta_engine
from dq_monitor.meta.models import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Режим --sql: печатает SQL миграций, не подключаясь к базе."""
    context.configure(
        url=get_settings().meta_url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Тесты передают своё соединение с временной базой, иначе берём базу meta из настроек
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return

    # meta_engine задаёт таймаут подключения: без него недоступная база вешает alembic
    engine = meta_engine()
    with engine.connect() as conn:
        _run(conn)
    engine.dispose()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
