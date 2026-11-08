"""Консольный интерфейс полигона: python -m dq_stand <команда>."""

import argparse
import sys
import time

from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

from dq_monitor.config import get_settings
from dq_monitor.coordination import RunInProgressError
from dq_monitor.db import meta_engine, warehouse_engine
from dq_monitor.report import format_run, summary_line
from dq_monitor.runner import run_checks
from dq_stand.config import get_stand_settings
from dq_stand.generate import generate_src
from dq_stand.loader import create_dwh, get_status, reset, run_cycles
from dq_stand.register import register_tpch


def cmd_init(force: bool) -> int:
    engine = warehouse_engine()
    if "src" in inspect(engine).get_schema_names() and not force:
        print("Полигон уже создан. Пересоздать с нуля: init --force", file=sys.stderr)
        return 1

    settings = get_stand_settings()
    started = time.perf_counter()
    generate_src(engine, settings.scale_factor, settings.start_date)
    print(f"src готова за {time.perf_counter() - started:.1f} с")

    create_dwh(engine)
    print(f"dwh и stand созданы, всего {time.perf_counter() - started:.1f} с")
    return 0


def cmd_load(days: int) -> int:
    started = time.perf_counter()
    results = run_cycles(warehouse_engine(), days)
    elapsed = time.perf_counter() - started
    if results:
        print(f"Загружено партий: {len(results)} за {elapsed:.1f} с")
    return 0


def cmd_status() -> int:
    s = get_status(warehouse_engine())
    print(f"Модельное время:   {s.model_now:%Y-%m-%d %H:%M}")
    print(f"Партий загружено:  {s.batches}")
    print(f"Последняя партия:  {s.last_batch_date or '-'}")
    print(f"Отставание, сут:   {s.lag_days}")
    print(f"Строк в dwh:       orders {s.orders_rows}, lineitem {s.lineitem_rows}")
    print(f"Данные в src до:   {s.data_end_date}")
    return 0


def cmd_reset() -> int:
    reset(warehouse_engine())
    print("Факты и журнал загрузок очищены, часы в начале данных")
    return 0


def cmd_cycle(days: int, verbose: bool) -> int:
    """Цикл эксперимента: загрузить партию и сразу прогнать проверки на модельное время.

    Ядро не знает о модельных часах: время ему сообщает полигон.
    """
    warehouse, meta = warehouse_engine(), meta_engine()
    all_ok = True
    for _ in range(days):
        batches = run_cycles(warehouse, 1, log=lambda _: None)
        if not batches:
            print("Данные в src закончились, циклы остановлены")
            break
        batch = batches[0]
        report = run_checks(meta, batch.loaded_at, trigger="schedule")
        all_ok = all_ok and report.ok
        if verbose:
            print(format_run(report), end="\n\n")
        else:
            print(f"{batch.batch_date}: {summary_line(report)}")
    return 0 if all_ok else 1


def cmd_register() -> int:
    added, existed = register_tpch(meta_engine(), get_settings())
    print(f"Датасеты в базе метаданных: добавлено {added}, уже было {existed}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="dq_stand", description="Полигон TPC-H для dq-monitor")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="сгенерировать TPC-H и создать схемы src, dwh, stand")
    p_init.add_argument("--force", action="store_true", help="пересоздать, если уже есть")

    p_load = sub.add_parser("load", help="выполнить циклы загрузки (одни модельные сутки каждый)")
    p_load.add_argument("--days", type=int, default=1, help="сколько циклов (по умолчанию 1)")

    sub.add_parser("status", help="состояние полигона")
    sub.add_parser("reset", help="очистить факты и вернуть часы в начало")
    sub.add_parser("register", help="зарегистрировать таблицы dwh в базе метаданных dq-monitor")

    p_cycle = sub.add_parser("cycle", help="циклы «загрузка партии + прогон проверок»")
    p_cycle.add_argument("--days", type=int, default=1, help="сколько циклов (по умолчанию 1)")
    p_cycle.add_argument("--verbose", action="store_true", help="полный отчёт каждого прогона")

    args = parser.parse_args()
    try:
        return run(args)
    except RunInProgressError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OperationalError as exc:
        first_line = str(exc.orig).splitlines()[0]
        print(f"Нет подключения к базе: {first_line}", file=sys.stderr)
        print("Проверьте, что PostgreSQL запущен: docker compose up -d", file=sys.stderr)
        return 1


def run(args: argparse.Namespace) -> int:
    if args.command == "init":
        return cmd_init(args.force)
    if args.command == "load":
        return cmd_load(args.days)
    if args.command == "status":
        return cmd_status()
    if args.command == "reset":
        return cmd_reset()
    if args.command == "register":
        return cmd_register()
    if args.command == "cycle":
        return cmd_cycle(args.days, args.verbose)
    return 1


if __name__ == "__main__":
    sys.exit(main())
