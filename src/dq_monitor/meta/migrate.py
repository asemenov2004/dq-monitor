"""Общие миграции приложения: несколько init-сервисов не меняют схему одновременно."""

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from dq_monitor.db import meta_engine


def upgrade(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(74261032)"))
        connection.commit()
        try:
            cfg = Config()
            cfg.set_main_option("script_location", "dq_monitor.meta:migrations")
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        finally:
            if connection.in_transaction():
                connection.rollback()
            if not connection.invalidated:
                connection.execute(text("SELECT pg_advisory_unlock(74261032)"))
                connection.commit()


if __name__ == "__main__":
    engine = meta_engine()
    try:
        upgrade(engine)
    finally:
        engine.dispose()
