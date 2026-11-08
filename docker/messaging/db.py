"""Отдельная роль и база локального Mattermost. Существующие данные сохраняются."""

import os

import psycopg
from psycopg import sql

with psycopg.connect(
    host=os.environ["POSTGRES_HOST"],
    port=os.environ["POSTGRES_PORT"],
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
    dbname=os.environ["WAREHOUSE_DB"],
    autocommit=True,
) as connection:
    exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname='mattermost'").fetchone()
    query = (
        "ALTER ROLE mattermost PASSWORD {}"
        if exists
        else "CREATE ROLE mattermost LOGIN PASSWORD {}"
    )
    connection.execute(sql.SQL(query).format(sql.Literal(os.environ["MATTERMOST_DB_PASSWORD"])))
    if not connection.execute("SELECT 1 FROM pg_database WHERE datname='mattermost'").fetchone():
        connection.execute("CREATE DATABASE mattermost OWNER mattermost")
print("База локального Mattermost подготовлена")
