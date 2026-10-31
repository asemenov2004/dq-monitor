"""Консольный интерфейс: python -m dq_monitor <команда>."""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, joinedload

from dq_monitor import __version__
from dq_monitor.catalog import CatalogError, load_catalog
from dq_monitor.checks import CheckContext, create_check
from dq_monitor.config import get_settings
from dq_monitor.connectors import connector_for
from dq_monitor.db import meta_engine, server_version, warehouse_engine
from dq_monitor.meta.models import Dataset, DqCheck, Incident
from dq_monitor.report import format_run, format_table
from dq_monitor.runner import as_aware_utc, run_checks


def cmd_ping() -> int:
    """Проверяет подключение к хранилищу и к базе метаданных."""
    ok = True
    for name, make_engine in (("warehouse", warehouse_engine), ("meta", meta_engine)):
        try:
            print(f"{name}: {server_version(make_engine())}")
        except Exception as exc:
            print(f"{name}: ошибка подключения: {exc}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


def cmd_run(now: datetime | None, check_ids: list[int] | None) -> int:
    report = run_checks(meta_engine(), now or datetime.now(UTC), check_ids=check_ids)
    print(format_run(report))
    return 0 if report.ok else 1


def cmd_checks_load(path: Path) -> int:
    try:
        report = load_catalog(meta_engine(), path)
    except CatalogError as exc:
        print("Файл проверок не загружен, исправьте ошибки:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    for title, labels in (("Добавлены", report.added), ("Обновлены", report.updated)):
        for label in labels:
            print(f"{title}: {label}")
    print(
        f"Итого: добавлено {len(report.added)}, обновлено {len(report.updated)}, "
        f"без изменений {len(report.unchanged)}"
    )
    return 0


def cmd_checks_list() -> int:
    with Session(meta_engine()) as session:
        open_checks = set(
            session.scalars(select(Incident.check_id).where(Incident.state == "open"))
        )
        checks = session.scalars(
            select(DqCheck).options(joinedload(DqCheck.dataset)).order_by(DqCheck.id)
        ).all()
        rows = [
            (
                str(c.id),
                f"{c.dataset.schema_name}.{c.dataset.table_name}",
                c.name,
                c.check_type,
                c.severity,
                "да" if c.is_active else "нет",
                "открыт" if c.id in open_checks else "",
            )
            for c in checks
        ]
    if not rows:
        print("Проверок нет. Загрузите набор: python -m dq_monitor checks load <файл>")
        return 0
    headers = ("ID", "Таблица", "Проверка", "Тип", "Важность", "Активна", "Инцидент")
    print(format_table(headers, rows))
    return 0


def cmd_checks_sql(check_id: int, now: datetime | None) -> int:
    """Показывает запрос, который проверка выполнит в момент now. Сам запрос не выполняется."""
    with Session(meta_engine()) as session:
        check = session.get(
            DqCheck,
            check_id,
            options=[joinedload(DqCheck.dataset).joinedload(Dataset.connection)],
        )
        if check is None:
            print(f"Проверка {check_id} не найдена", file=sys.stderr)
            return 1
        implementation = create_check(check.check_type, check.params)
        dataset = check.dataset
        timeout = get_settings().check_statement_timeout_sec
        with connector_for(dataset.connection, timeout) as connector:
            ctx = CheckContext(
                connector,
                dataset.schema_name,
                dataset.table_name,
                dataset.batch_column,
                dataset.loaded_at_column,
                as_aware_utc(now or datetime.now(UTC)),
            )
            query = implementation.build_query(ctx)
    print(f"-- {check.name} ({check.check_type}), {dataset.schema_name}.{dataset.table_name}")
    print(query.sql)
    for name, value in query.params.items():
        print(f"-- :{name} = {value!r}")
    return 0


def parse_now(value: str) -> datetime:
    """Логическое время из строки ISO 8601; без часового пояса считается UTC."""
    return as_aware_utc(datetime.fromisoformat(value))


def main() -> int:
    parser = argparse.ArgumentParser(prog="dq_monitor", description="Контроль качества данных")
    parser.add_argument("--version", action="version", version=f"dq-monitor {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping", help="проверить подключение к базам")

    p_run = sub.add_parser(
        "run", help="выполнить активные проверки (код выхода 1, если есть fail или error)"
    )
    p_run.add_argument("--now", type=parse_now, help="логическое время, например 2025-01-31T02:00")
    p_run.add_argument("--check", type=int, action="append", help="только эта проверка (ID)")

    p_checks = sub.add_parser("checks", help="набор проверок")
    checks_sub = p_checks.add_subparsers(dest="checks_command", required=True)
    p_load = checks_sub.add_parser("load", help="загрузить проверки из YAML-файла")
    p_load.add_argument("path", type=Path)
    checks_sub.add_parser("list", help="список проверок")
    p_sql = checks_sub.add_parser("sql", help="показать SQL-запрос проверки")
    p_sql.add_argument("check_id", type=int)
    p_sql.add_argument("--now", type=parse_now, help="логическое время")

    args = parser.parse_args()
    try:
        return dispatch(args)
    except OperationalError as exc:
        first_line = str(exc.orig).splitlines()[0]
        print(f"Нет подключения к базе: {first_line}", file=sys.stderr)
        print("Проверьте, что PostgreSQL запущен: docker compose up -d", file=sys.stderr)
        return 1


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "ping":
        return cmd_ping()
    if args.command == "run":
        return cmd_run(args.now, args.check)
    if args.command == "checks":
        if args.checks_command == "load":
            return cmd_checks_load(args.path)
        if args.checks_command == "list":
            return cmd_checks_list()
        if args.checks_command == "sql":
            return cmd_checks_sql(args.check_id, args.now)
    return 1


if __name__ == "__main__":
    sys.exit(main())
