"""Разогрев и отклонение метрики через HTTP на собственной маленькой таблице.

Проверка после демонстрации выключается; её история остаётся доступной в UI.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from demo import prepare, run
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from dq_monitor.config import get_settings
from dq_monitor.meta.models import CheckResult, Dataset, DqCheck

NAME = "demo_amount_anomaly"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="mvp-anomaly.json")
    args = parser.parse_args()
    settings = get_settings()
    engine, warehouse = create_engine(settings.meta_url), create_engine(settings.warehouse_url)
    check_id = None
    report = {"recorded_at": datetime.now(UTC).isoformat(), "runs": []}
    try:
        prepare(settings)
        with Session(engine) as s, s.begin():
            dataset = s.scalar(
                select(Dataset).where(
                    Dataset.schema_name == "dq_demo", Dataset.table_name == "orders"
                )
            )
            check = s.scalar(
                select(DqCheck).where(DqCheck.dataset_id == dataset.id, DqCheck.name == NAME)
            )
            if check is None:
                check = DqCheck(
                    dataset_id=dataset.id,
                    name=NAME,
                    check_type="anomaly",
                    params={
                        "scope": "table",
                        "metric": "mean",
                        "column": "amount",
                        "window": 7,
                        "threshold": 3,
                    },
                    description="Приёмка MVP: постоянная средняя, разогрев 7 дней и выброс",
                )
                s.add(check)
                s.flush()
            check.is_active = True
            check.check_type = "anomaly"
            check.params = {
                "scope": "table",
                "metric": "mean",
                "column": "amount",
                "window": 7,
                "threshold": 3,
            }
            check_id = check.id
        # Каждое повторение начинает демонстрацию после ранее сохранённой истории.
        with Session(engine) as s:
            previous = s.scalars(select(CheckResult).where(CheckResult.check_id == check_id)).all()
            last = max(
                (r.run.logical_now for r in previous), default=datetime(2026, 1, 1, tzinfo=UTC)
            )
        start = last + timedelta(days=8)
        for day in range(9):
            if day == 7:
                with warehouse.begin() as c:
                    c.execute(text("UPDATE dq_demo.orders SET amount=amount*10 WHERE id IN (1,2)"))
            elif day == 8:
                with warehouse.begin() as c:
                    c.execute(text("UPDATE dq_demo.orders SET amount=id*100 WHERE id IN (1,2)"))
            run_id = run(settings, NAME, start + timedelta(days=day))
            with Session(engine) as s:
                result = s.scalar(
                    select(CheckResult).where(
                        CheckResult.run_id == run_id, CheckResult.check_id == check_id
                    )
                )
                expected = "skip" if day < 7 else "fail" if day == 7 else "pass"
                assert result.status == expected, (day, result.status, result.message)
                report["runs"].append(
                    {
                        "day": day + 1,
                        "run_id": run_id,
                        "status": result.status,
                        "observed": str(result.observed_value),
                        "message": result.message,
                        "details": result.details,
                    }
                )
        report.update(result="PASS", check_id=check_id, warmup_days=7)
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Аномалия: 7 SKIP, FAIL, PASS. История сохранена, проверка выключена.")
    finally:
        with warehouse.begin() as c:
            c.execute(text("UPDATE dq_demo.orders SET amount=id*100 WHERE id IN (1,2)"))
        if check_id is not None:
            with Session(engine) as s, s.begin():
                s.get(DqCheck, check_id).is_active = False
        warehouse.dispose()
        engine.dispose()


if __name__ == "__main__":
    main()
