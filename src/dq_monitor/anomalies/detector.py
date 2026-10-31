"""Двусторонняя z-оценка: текущая точка сравнивается только с предыдущим окном."""

import math
from dataclasses import dataclass
from statistics import mean, stdev


@dataclass(frozen=True)
class Detection:
    ready: bool
    anomalous: bool = False
    count: int = 0
    mean: float | None = None
    stddev: float | None = None
    low: float | None = None
    high: float | None = None
    z_score: float | None = None
    constant_shift: bool = False


def detect(
    value: float,
    history: tuple[float, ...],
    window: int = 30,
    threshold: float = 3.0,
    min_history: int | None = None,
) -> Detection:
    """Порядок history: от старого к новому. Текущая точка не включается в оценку нормы.

    Используется выборочное стандартное отклонение (делитель n-1).
    При постоянной истории любое изменение считается аномалией; бесконечный z не сохраняется.
    """
    required = window if min_history is None else min_history
    if window < 2 or not 2 <= required <= window:
        raise ValueError("Нужно 2 <= min_history <= window")
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("Порог должен быть конечным положительным числом")
    points = history[-window:]
    if not math.isfinite(value) or any(not math.isfinite(p) for p in points):
        raise ValueError("Метрика и история должны содержать только конечные числа")
    if len(points) < required:
        return Detection(ready=False, count=len(points))
    center, spread = mean(points), stdev(points)
    low, high = center - threshold * spread, center + threshold * spread
    if not all(math.isfinite(n) for n in (center, spread, low, high)):
        raise ValueError("Переполнение при вычислении статистики")
    if spread == 0:
        changed = value != center
        return Detection(
            True, changed, len(points), center, spread, low, high, None if changed else 0.0, changed
        )
    z = (value - center) / spread
    if not math.isfinite(z):
        raise ValueError("Переполнение при вычислении z-оценки")
    return Detection(True, abs(z) > threshold, len(points), center, spread, low, high, z)
