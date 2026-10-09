"""Консольный интерфейс: python -m dq_monitor <команда>."""

import argparse
import sys

from dq_monitor import __version__
from dq_monitor.db import meta_engine, server_version, warehouse_engine


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


def main() -> int:
    parser = argparse.ArgumentParser(prog="dq_monitor", description="Контроль качества данных")
    parser.add_argument("--version", action="version", version=f"dq-monitor {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping", help="проверить подключение к базам")

    args = parser.parse_args()
    if args.command == "ping":
        return cmd_ping()
    return 1


if __name__ == "__main__":
    sys.exit(main())
