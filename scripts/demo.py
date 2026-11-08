"""Воспроизводимая демонстрация MVP на отдельной таблице, без изменения TPC-H.

prepare → run → break → run → run → repair → run.
Команда run обращается к настоящему HTTP-интерфейсу и общему ядру.
"""

import argparse
import re
from datetime import UTC, datetime

import httpx
from mvp import local_url
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from dq_monitor.config import get_settings
from dq_monitor.meta.models import Connection, Dataset, DqCheck

NAME = "demo_amount_not_null"


def prepare(settings):
    warehouse = create_engine(settings.warehouse_url)
    try:
        with warehouse.begin() as c:
            c.execute(text("CREATE SCHEMA IF NOT EXISTS dq_demo"))
            c.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS dq_demo.orders "
                    "(id integer PRIMARY KEY, amount numeric)"
                )
            )
            # Только собственная демонстрационная таблица. Остальные схемы не меняются.
            c.execute(text("TRUNCATE dq_demo.orders"))
            c.execute(
                text("INSERT INTO dq_demo.orders SELECT n, n*100 FROM generate_series(1,10) n")
            )
    finally:
        warehouse.dispose()
    engine = create_engine(settings.meta_url)
    try:
        with Session(engine) as s, s.begin():
            source = s.scalar(select(Connection).where(Connection.name == "tpch_warehouse"))
            if source is None:
                raise SystemExit("Сначала выполните python -m dq_stand register")
            dataset = s.scalar(
                select(Dataset).where(
                    Dataset.connection_id == source.id,
                    Dataset.schema_name == "dq_demo",
                    Dataset.table_name == "orders",
                )
            )
            if dataset is None:
                dataset = Dataset(
                    connection_id=source.id,
                    schema_name="dq_demo",
                    table_name="orders",
                    description="Изолированная таблица для демонстрации MVP: 10 заказов",
                )
                s.add(dataset)
                s.flush()
            check = s.scalar(
                select(DqCheck).where(DqCheck.dataset_id == dataset.id, DqCheck.name == NAME)
            )
            if check is None:
                s.add(
                    DqCheck(
                        dataset_id=dataset.id,
                        name=NAME,
                        check_type="not_null",
                        params={"scope": "table", "column": "amount", "max_null_ratio": 0},
                        description="Демонстрация: 10 заказов, порча двух amount, восстановление",
                    )
                )
    finally:
        engine.dispose()
    print("Подготовлены dq_demo.orders (10 строк) и проверка " + NAME)


def run(settings, check_name=NAME, logical_now=None):
    engine = create_engine(settings.meta_url)
    try:
        with Session(engine) as s:
            check_id = s.scalar(
                select(DqCheck.id)
                .join(Dataset)
                .where(DqCheck.name == check_name, Dataset.schema_name == "dq_demo")
            )
        if check_id is None:
            raise SystemExit("Сначала python scripts/demo.py prepare")
    finally:
        engine.dispose()
    from dotenv import dotenv_values

    values = dotenv_values(".env")
    base = local_url(values, "WEB_PORT", 8000)
    with httpx.Client(base_url=base, timeout=120, follow_redirects=True) as client:
        page = client.get(f"/checks/{check_id}")
        page.raise_for_status()
        token = re.search(r'name="csrf" value="([^"]+)"', page.text)
        if token is None:
            raise SystemExit("Не найдена форма запуска")
        response = client.post(
            "/runs",
            data={
                "csrf": token[1],
                "check_id": str(check_id),
                "logical_now": (logical_now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M"),
            },
        )
        response.raise_for_status()
        if not re.search(r"/runs/\d+$", str(response.url)):
            raise SystemExit("Прогон не запущен: проверьте сообщение в веб-интерфейсе")
        print("Отчёт: " + str(response.url))
        print("Карточка: " + base + f"/checks/{check_id}")
        return int(str(response.url).rsplit("/", 1)[1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run", "break", "repair"])
    args = parser.parse_args()
    settings = get_settings()
    if args.command == "prepare":
        prepare(settings)
    elif args.command == "run":
        run(settings)
    else:
        engine = create_engine(settings.warehouse_url)
        try:
            with engine.begin() as c:
                if args.command == "break":
                    c.execute(text("UPDATE dq_demo.orders SET amount=NULL WHERE id IN (1,2)"))
                else:
                    c.execute(text("UPDATE dq_demo.orders SET amount=id*100 WHERE id IN (1,2)"))
        finally:
            engine.dispose()
        print("Две суммы заменены на NULL" if args.command == "break" else "Суммы восстановлены")


if __name__ == "__main__":
    main()
