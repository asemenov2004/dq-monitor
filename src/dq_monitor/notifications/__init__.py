"""Durable outbox уведомлений: очередь независима от настроек транспорта.

CLI: python -m dq_monitor.notifications --once (или --loop).
Настройки доставки читаются только worker, с префиксом NOTIFY_.
"""
