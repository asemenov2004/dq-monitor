"""Генерация TPC-H через DuckDB и выгрузка полного слепка в схему src хранилища."""

from datetime import date

import duckdb
from sqlalchemy import URL, Engine, text

TPCH_TABLES = ("region", "nation", "supplier", "customer", "part", "partsupp", "orders", "lineitem")

DATE_COLUMNS = {
    "orders": ("o_orderdate",),
    "lineitem": ("l_shipdate", "l_commitdate", "l_receiptdate"),
}


def date_offset_days(data_start: date, start_date: date) -> int:
    """На сколько дней сдвинуть даты, чтобы данные начинались не с data_start, а с start_date.

    Сдвиг именно в днях, а не в годах: при сдвиге на годы 29 февраля
    превращается в 28-е, и два разных дня сливаются в одну партию.
    """
    return (start_date - data_start).days


def libpq_conninfo(url: URL) -> str:
    """Строка подключения libpq (key='value' ...) для расширения postgres в DuckDB."""

    def quote(value) -> str:
        return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"

    parts = {
        "host": url.host,
        "port": url.port,
        "dbname": url.database,
        "user": url.username,
        "password": url.password,
    }
    return " ".join(f"{key}={quote(value)}" for key, value in parts.items() if value is not None)


def select_with_shifted_dates(table: str, offset_days: int) -> str:
    columns = DATE_COLUMNS.get(table)
    if not columns:
        return f"SELECT * FROM {table}"
    replaced = ", ".join(f"{col} + {offset_days} AS {col}" for col in columns)
    return f"SELECT * REPLACE ({replaced}) FROM {table}"


def generate_src(engine: Engine, scale_factor: float, start_date: date, log=print) -> None:
    """Пересоздаёт схему src и заполняет её данными TPC-H со сдвинутыми датами."""
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS src CASCADE"))
        conn.execute(text("CREATE SCHEMA src"))

    duck = duckdb.connect()
    try:
        log(f"Генерация TPC-H, SF {scale_factor}")
        duck.execute("CALL dbgen(sf = ?)", [scale_factor])
        # Точку отсчёта берём из самих данных, а не из спецификации TPC-H
        (data_start,) = duck.execute("SELECT min(o_orderdate) FROM orders").fetchone()
        offset = date_offset_days(data_start, start_date)

        duck.execute("INSTALL postgres")
        duck.execute("LOAD postgres")
        conninfo = libpq_conninfo(engine.url).replace("'", "''")
        duck.execute(f"ATTACH '{conninfo}' AS pg (TYPE postgres)")

        for table in TPCH_TABLES:
            log(f"  src.{table}")
            duck.execute(
                f"CREATE TABLE pg.src.{table} AS {select_with_shifted_dates(table, offset)}"
            )
    finally:
        duck.close()

    with engine.begin() as conn:
        conn.execute(text("ANALYZE"))
