"""Один DAG вызывает ядро. Данные и активные правила перечитываются при каждом запуске."""

from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, Param, task

with DAG(
    dag_id="dq_monitor_checks",
    description="Контроль качества данных: активные проверки по расписанию из meta",
    schedule="* * * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    tags=["dq-monitor"],
    params={
        "logical_now": Param(
            None,
            type=["null", "string"],
            description="Только для демонстрации: время из stand.clock, ISO 8601",
        )
    },
) as dag:

    @task(task_id="run_due_checks", retries=0, execution_timeout=timedelta(minutes=30))
    def execute_checks(params=None, dag_run=None):
        # Импорты и подключение к базе только во время задачи, не при разборе DAG.
        import logging

        from airflow.exceptions import AirflowException, AirflowSkipException

        from dq_monitor.coordination import RunInProgressError
        from dq_monitor.db import meta_engine
        from dq_monitor.report import format_run
        from dq_monitor.runner import as_aware_utc, run_checks
        from dq_monitor.scheduling import run_due_checks

        now_text = (params or {}).get("logical_now")
        logical_now = as_aware_utc(datetime.fromisoformat(now_text)) if now_text else None
        engine = meta_engine()
        try:
            try:
                run_type = getattr(dag_run, "run_type", "manual")
                run_type = getattr(run_type, "value", run_type)
                if run_type == "scheduled":
                    report = run_due_checks(engine)
                else:
                    report = run_checks(engine, logical_now or datetime.now(UTC))
            except RunInProgressError as exc:
                raise AirflowSkipException(str(exc)) from exc
            if report is None:
                raise AirflowSkipException("Нет наступивших проверок")
            logging.info("\n%s", format_run(report))
            if not report.ok:
                raise AirflowException(f"Прогон {report.run_id}: найдены нарушения или ошибки")
        finally:
            engine.dispose()

    execute_checks()
