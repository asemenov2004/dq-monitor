"""Подготовка SVG без внешней библиотеки и CDN. Числа и пропуски обрабатываются явно."""

import math


def metric_chart(rows):
    if not rows:
        return None

    def finite(value):
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    series = [
        [finite(getattr(row, field)) for row in rows]
        for field in ("observed_value", "expected_low", "expected_high")
    ]
    values = [v for line in series for v in line if v is not None]
    if not values:
        return None
    low, high = min(values), max(values)
    padding = max(abs(high) * 0.05, 1) if low == high else (high - low) * 0.08
    low, high = low - padding, high + padding
    if not math.isfinite(high - low):
        return None

    times = [row.run.logical_now.timestamp() for row in rows]
    span = max(times) - min(times)
    # Повторные прогоны одного модельного дня имеют одну координату времени.
    xs = [60 + 800 * (value - min(times)) / span if span else 460 for value in times]

    def path(points):
        commands, started = [], False
        for index, value in enumerate(points):
            if value is None:
                started = False
                continue
            x = xs[index]
            y = 190 - 160 * (value - low) / (high - low)
            commands.append(f"{'L' if started else 'M'}{x:.2f},{y:.2f}")
            started = True
        return " ".join(commands)

    return {
        "observed": path(series[0]),
        "lower": path(series[1]),
        "upper": path(series[2]),
        "dots": [
            (xs[i], 190 - 160 * (v - low) / (high - low))
            for i, v in enumerate(series[0])
            if v is not None
        ],
        "high": f"{high:.4g}",
        "low": f"{low:.4g}",
        "first": rows[0].run.logical_now.strftime("%d.%m.%Y %H:%M"),
        "last": rows[-1].run.logical_now.strftime("%d.%m.%Y %H:%M"),
    }
