"""Альтернативный запуск: python -m dq_monitor.notifications [--once | --loop]."""

import sys

from dq_monitor.notifications.worker import main

if __name__ == "__main__":
    sys.exit(main())
