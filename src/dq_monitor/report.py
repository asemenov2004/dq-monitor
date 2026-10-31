"""Текстовые таблицы для консоли: отчёт о прогоне и список проверок."""

from collections.abc import Sequence

from dq_monitor.checks import PASS
from dq_monitor.runner import RunReport

INCIDENT_LABELS = {"opened": "открыт", "closed": "закрыт"}


def format_value(value: float | None) -> str:
    if value is None:
        return "-"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.4g}"


def format_bounds(low: float | None, high: float | None) -> str:
    if low is None and high is None:
        return "-"
    return f"[{format_value(low)}; {format_value(high)}]"


def format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows, strict=True)]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(str(c).ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    separator = "  ".join("-" * w for w in widths)
    return "\n".join([line(headers), separator, *(line(r) for r in rows)])


def format_run(report: RunReport) -> str:
    rows = [
        (
            str(item.check_id),
            item.check_name,
            item.dataset,
            item.check_type,
            item.outcome.status.upper(),
            format_value(item.outcome.observed),
            format_bounds(item.outcome.expected_low, item.outcome.expected_high),
            str(item.duration_ms),
            INCIDENT_LABELS.get(item.incident_event, ""),
        )
        for item in report.items
    ]
    headers = ("ID", "Проверка", "Таблица", "Тип", "Статус", "Значение", "Норма", "мс", "Инцидент")
    parts = [
        f"Прогон {report.run_id}, логическое время {report.logical_now:%Y-%m-%d %H:%M} UTC",
        "",
        format_table(headers, rows) if rows else "Активных проверок нет",
    ]

    problems = [item for item in report.items if item.outcome.status != PASS]
    if problems:
        parts += ["", "Подробности:"]
        parts += [
            f"  [{i.outcome.status.upper()}] {i.check_name}: {i.outcome.message}" for i in problems
        ]

    parts += ["", summary_line(report)]
    return "\n".join(parts)


def summary_line(report: RunReport) -> str:
    return (
        f"Итого: pass {report.count('pass')}, fail {report.count('fail')}, "
        f"error {report.count('error')}; инциденты: открыто {report.incidents('opened')}, "
        f"закрыто {report.incidents('closed')}; время {report.duration_sec:.2f} с"
    )
