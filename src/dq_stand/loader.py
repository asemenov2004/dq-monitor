"""Схема dwh, модельные часы и загрузка фактов партиями «по дням».

Один цикл загрузки = одни модельные сутки:
  1. часы сдвигаются на сутки вперёд (например, до 06.03 02:00);
  2. загружается партия за вчерашний день (batch_date = 05.03, loaded_at = 06.03 02:00).
Часы хранятся отдельно от данных (stand.clock), поэтому идут, даже если партия
не пришла: иначе проверка свежести не смогла бы заметить задержку загрузки.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import Connection, Engine, text

# Партия за день D «приходит» в D+1 в это время: эмуляция ночного ETL
LOAD_TIME = time(2, 0)

DIMENSIONS = ("region", "nation", "supplier", "customer", "part", "partsupp")


@dataclass(frozen=True)
class BatchResult:
    batch_date: date
    loaded_at: datetime
    orders_rows: int
    lineitem_rows: int


@dataclass(frozen=True)
class Status:
    model_now: datetime
    batches: int
    last_batch_date: date | None
    orders_rows: int
    lineitem_rows: int
    data_end_date: date

    @property
    def lag_days(self) -> int:
        """На сколько суток последняя партия отстаёт от ожидаемой (вчерашней)."""
        expected = self.model_now.date() - timedelta(days=1)
        if self.last_batch_date is None:
            return 0
        return (expected - self.last_batch_date).days


def initial_clock(data_start: date) -> datetime:
    """Часы стоят так, что первый цикл загрузит партию за первый день данных."""
    return datetime.combine(data_start, LOAD_TIME)


def next_cycle(model_now: datetime) -> tuple[datetime, date]:
    """Новое модельное время и дата партии, которую нужно загрузить в этом цикле."""
    new_now = model_now + timedelta(days=1)
    return new_now, new_now.date() - timedelta(days=1)


def _data_range(conn: Connection) -> tuple[date, date]:
    return conn.execute(text("SELECT min(o_orderdate), max(o_orderdate) FROM src.orders")).one()


def create_dwh(engine: Engine) -> None:
    """Пересоздаёт схемы dwh и stand: справочники копируются целиком, факты пустые.

    Ограничений целостности (PK, FK) в dwh нет намеренно: как и во многих
    аналитических хранилищах, целостность не навязана, её контролирует dq-monitor.
    """
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS dwh CASCADE"))
        conn.execute(text("DROP SCHEMA IF EXISTS stand CASCADE"))
        conn.execute(text("CREATE SCHEMA dwh"))
        conn.execute(text("CREATE SCHEMA stand"))

        for table in DIMENSIONS:
            conn.execute(text(f"CREATE TABLE dwh.{table} AS TABLE src.{table}"))

        for table in ("orders", "lineitem"):
            conn.execute(text(f"CREATE TABLE dwh.{table} (LIKE src.{table})"))
            conn.execute(
                text(
                    f"ALTER TABLE dwh.{table} "
                    "ADD COLUMN batch_date date, ADD COLUMN loaded_at timestamp"
                )
            )

        # Служебные таблицы полигона, dq-monitor их не проверяет
        conn.execute(text("CREATE TABLE stand.clock (model_now timestamp NOT NULL)"))
        conn.execute(
            text(
                """
                CREATE TABLE stand.load_log (
                    batch_date    date PRIMARY KEY,
                    loaded_at     timestamp NOT NULL,
                    orders_rows   integer NOT NULL,
                    lineitem_rows integer NOT NULL
                )
                """
            )
        )
        data_start, _ = _data_range(conn)
        conn.execute(
            text("INSERT INTO stand.clock VALUES (:now)"), {"now": initial_clock(data_start)}
        )


def load_batch(conn: Connection, batch_date: date, loaded_at: datetime) -> BatchResult:
    """Переносит из src в dwh заказы за день и их строки lineitem."""
    params = {"day": batch_date, "loaded_at": loaded_at}
    orders = conn.execute(
        text(
            """
            INSERT INTO dwh.orders
            SELECT o.*, :day, :loaded_at FROM src.orders o
            WHERE o.o_orderdate = :day
            """
        ),
        params,
    ).rowcount
    # У lineitem своей даты заказа нет, партия определяется датой заказа
    lineitem = conn.execute(
        text(
            """
            INSERT INTO dwh.lineitem
            SELECT l.*, :day, :loaded_at FROM src.lineitem l
            JOIN src.orders o ON o.o_orderkey = l.l_orderkey
            WHERE o.o_orderdate = :day
            """
        ),
        params,
    ).rowcount
    conn.execute(
        text("INSERT INTO stand.load_log VALUES (:day, :loaded_at, :orders, :lineitem)"),
        params | {"orders": orders, "lineitem": lineitem},
    )
    return BatchResult(batch_date, loaded_at, orders, lineitem)


def run_cycles(engine: Engine, days: int, log=print) -> list[BatchResult]:
    """Выполняет days циклов «часы + сутки, загрузка вчерашней партии».

    Каждый цикл в своей транзакции: партия либо загружена целиком вместе
    со сдвигом часов, либо не загружена вовсе.
    """
    results = []
    with engine.connect() as conn:
        _, data_end = _data_range(conn)

    for _ in range(days):
        with engine.begin() as conn:
            model_now = conn.execute(
                text("SELECT model_now FROM stand.clock FOR UPDATE")
            ).scalar_one()
            new_now, batch_date = next_cycle(model_now)
            if batch_date > data_end:
                log(f"Данные в src закончились ({data_end}), загрузка остановлена")
                break
            conn.execute(text("UPDATE stand.clock SET model_now = :now"), {"now": new_now})
            result = load_batch(conn, batch_date, new_now)
        log(
            f"  {result.batch_date}: orders {result.orders_rows}, "
            f"lineitem {result.lineitem_rows}, loaded_at {result.loaded_at:%Y-%m-%d %H:%M}"
        )
        results.append(result)
    return results


def get_status(engine: Engine) -> Status:
    with engine.connect() as conn:
        model_now = conn.execute(text("SELECT model_now FROM stand.clock")).scalar_one()
        batches, last_batch = conn.execute(
            text("SELECT count(*), max(batch_date) FROM stand.load_log")
        ).one()
        orders = conn.execute(text("SELECT count(*) FROM dwh.orders")).scalar_one()
        lineitem = conn.execute(text("SELECT count(*) FROM dwh.lineitem")).scalar_one()
        _, data_end = _data_range(conn)
    return Status(model_now, batches, last_batch, orders, lineitem, data_end)


def reset(engine: Engine) -> None:
    """Очищает факты и журнал загрузок, возвращает часы в начало данных. src не трогает."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE dwh.orders, dwh.lineitem, stand.load_log"))
        data_start, _ = _data_range(conn)
        conn.execute(
            text("UPDATE stand.clock SET model_now = :now"), {"now": initial_clock(data_start)}
        )
