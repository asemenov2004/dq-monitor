"""Подготовка локального Airflow и запуск его отдельных компонентов.

Адреса и пароли берутся из окружения; ключи хранятся в Docker volume.
Ни пакет ядра, ни DAG не зависят от этого скрипта.
"""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet
from psycopg import sql
from sqlalchemy import URL

STATE = Path("/opt/airflow/config")


def configure() -> None:
    """Общие постоянные ключи для API, scheduler и dag-processor."""
    for filename, setting in (
        ("jwt-secret", "AIRFLOW__API_AUTH__JWT_SECRET"),
        ("jwt-secret", "AIRFLOW__API__SECRET_KEY"),
        ("fernet-key", "AIRFLOW__CORE__FERNET_KEY"),
    ):
        os.environ[setting] = (STATE / filename).read_text().strip()
    os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = URL.create(
        "postgresql+psycopg",
        username="airflow",
        password=os.environ["AIRFLOW_DB_PASSWORD"],
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ["POSTGRES_PORT"]),
        database="airflow",
    ).render_as_string(hide_password=False)


def initialize() -> None:
    """Идемпотентно создаёт отдельную БД/роль Airflow и обновляет обе схемы миграциями."""
    STATE.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("jwt-secret", secrets.token_urlsafe(48)),
        ("fernet-key", Fernet.generate_key().decode()),
    ):
        path = STATE / name
        if not path.exists():
            path.write_text(value)
            path.chmod(0o600)
    (STATE / "passwords.json").write_text(
        json.dumps({"admin": os.environ["AIRFLOW_ADMIN_PASSWORD"]})
    )
    (STATE / "passwords.json").chmod(0o600)

    with psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["WAREHOUSE_DB"],
        autocommit=True,
    ) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname='airflow'").fetchone()
        command = (
            "ALTER ROLE airflow PASSWORD {}" if exists else "CREATE ROLE airflow LOGIN PASSWORD {}"
        )
        conn.execute(sql.SQL(command).format(sql.Literal(os.environ["AIRFLOW_DB_PASSWORD"])))
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname='airflow'").fetchone():
            conn.execute("CREATE DATABASE airflow OWNER airflow")
    configure()
    # Миграции приложения ставятся вместе с пакетом, конфиг проекта на диске не нужен.
    from dq_monitor.db import meta_engine
    from dq_monitor.meta.migrate import upgrade

    engine = meta_engine()
    try:
        upgrade(engine)
    finally:
        engine.dispose()
    subprocess.run(["airflow", "db", "migrate"], check=True)
    print("Базы meta и airflow подготовлены", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["init"]:
        initialize()
    else:
        configure()
        os.execvp("airflow", ["airflow", *sys.argv[1:]])
