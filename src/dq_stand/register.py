"""Регистрация полигона в базе метаданных: подключение к хранилищу и таблицы dwh как датасеты."""

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from dq_monitor.config import Settings
from dq_monitor.meta.models import Connection, Dataset

CONNECTION_NAME = "tpch_warehouse"
DWH_SCHEMA = "dwh"
FACT_TABLES = ("orders", "lineitem")
DIMENSION_TABLES = ("region", "nation", "supplier", "customer", "part", "partsupp")


def register_tpch(meta_engine: Engine, settings: Settings) -> tuple[int, int]:
    """Создаёт подключение и датасеты, если их ещё нет. Повторный вызов ничего не дублирует.

    Возвращает (сколько датасетов добавлено, сколько уже было).
    """
    with Session(meta_engine) as session, session.begin():
        connection = session.scalar(select(Connection).where(Connection.name == CONNECTION_NAME))
        if connection is None:
            connection = Connection(
                name=CONNECTION_NAME,
                db_type="postgres",
                host=settings.postgres_host,
                port=settings.postgres_port,
                database=settings.warehouse_db,
                username=settings.postgres_user,
                password_env="POSTGRES_PASSWORD",
            )
            session.add(connection)
            session.flush()  # нужен id подключения для датасетов

        existing = set(
            session.scalars(
                select(Dataset.table_name).where(
                    Dataset.connection_id == connection.id, Dataset.schema_name == DWH_SCHEMA
                )
            )
        )

        added = 0
        for table in FACT_TABLES + DIMENSION_TABLES:
            if table in existing:
                continue
            is_fact = table in FACT_TABLES
            session.add(
                Dataset(
                    connection_id=connection.id,
                    schema_name=DWH_SCHEMA,
                    table_name=table,
                    batch_column="batch_date" if is_fact else None,
                    loaded_at_column="loaded_at" if is_fact else None,
                    description="TPC-H, факт (догружается по дням)"
                    if is_fact
                    else "TPC-H, справочник",
                )
            )
            added += 1

    return added, len(existing)
