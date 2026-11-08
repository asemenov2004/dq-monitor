"""Сквозная приёмка локального MVP. Меняет только dq_demo.orders и её историю.

Нужен запущенный python scripts/mvp.py up. Отчёт не содержит секретов.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from demo import prepare, run
from dotenv import dotenv_values
from mvp import api, local_url
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from dq_monitor.config import get_settings
from dq_monitor.meta.models import CheckResult, Dataset, DqCheck, Incident, Notification


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="mvp-acceptance.json")
    args = parser.parse_args()
    settings = get_settings()
    values = dotenv_values(".env")
    engine = create_engine(settings.meta_url)
    warehouse = create_engine(settings.warehouse_url)
    report = {"recorded_at": datetime.now(UTC).isoformat(), "runs": []}
    try:
        prepare(settings)
        with Session(engine) as s:
            check_id = s.scalar(
                select(DqCheck.id)
                .join(Dataset)
                .where(DqCheck.name == "demo_amount_not_null", Dataset.schema_name == "dq_demo")
            )
        for step, expected in [
            ("clean", "pass"),
            ("defect", "fail"),
            ("repeat", "fail"),
            ("restored", "pass"),
        ]:
            if step in ("defect", "restored"):
                with warehouse.begin() as c:
                    query = (
                        "UPDATE dq_demo.orders SET amount=NULL WHERE id IN (1,2)"
                        if step == "defect"
                        else "UPDATE dq_demo.orders SET amount=id*100 WHERE id IN (1,2)"
                    )
                    c.execute(text(query))
            started = time.perf_counter()
            run_id = run(settings)
            duration = time.perf_counter() - started
            with Session(engine) as s:
                result = s.scalar(
                    select(CheckResult).where(
                        CheckResult.run_id == run_id, CheckResult.check_id == check_id
                    )
                )
                assert result.status == expected, (step, result.status, result.message)
                report["runs"].append(
                    {
                        "step": step,
                        "run_id": run_id,
                        "status": result.status,
                        "observed": str(result.observed_value),
                        "http_seconds": round(duration, 3),
                        "query_ms": result.duration_ms,
                    }
                )
                if step == "defect":
                    incident_id = s.scalar(
                        select(Incident.id).where(Incident.opened_result_id == result.id)
                    )
                    assert incident_id is not None
                if step == "repeat":
                    assert (
                        s.scalar(
                            select(func.count())
                            .select_from(Incident)
                            .where(Incident.check_id == check_id, Incident.state == "open")
                        )
                        == 1
                    )
        with Session(engine) as s:
            incident = s.get(Incident, incident_id)
            assert incident.state == "closed" and incident.closed_result_id is not None
        deadline = time.monotonic() + 90
        while True:
            with Session(engine) as s:
                rows = s.scalars(
                    select(Notification).where(Notification.incident_id == incident_id)
                ).all()
                assert len(rows) == 4
                if all(row.status == "sent" for row in rows):
                    report["notifications"] = [
                        {
                            "id": r.id,
                            "channel": r.channel,
                            "event": r.event,
                            "status": r.status,
                            "attempts": r.attempts,
                            "created_at": r.created_at.isoformat(),
                            "sent_at": r.sent_at.isoformat(),
                            "delivery_seconds": round(
                                (r.sent_at - r.created_at).total_seconds(), 3
                            ),
                        }
                        for r in rows
                    ]
                    break
            if time.monotonic() > deadline:
                raise AssertionError("Worker не доставил четыре сообщения за 90 секунд")
            time.sleep(1)
        marker = f"DQ incident #{incident_id}:"
        mail_base = local_url(values, "MAILPIT_PORT", 8025)
        with httpx.Client(base_url=mail_base, timeout=10) as client:
            response = client.get("/api/v1/messages", params={"limit": 100})
            response.raise_for_status()
            matches = []
            for message in response.json()["messages"]:
                detail = client.get("/api/v1/message/" + message["ID"])
                detail.raise_for_status()
                if marker in detail.json().get("Text", ""):
                    matches.append(message["ID"])
            assert len(matches) == 2, ("Mailpit", len(matches))
            report["mailpit_message_ids"] = matches
        base = local_url(values, "MATTERMOST_PORT", 8065)
        _, headers = api(
            base,
            "POST",
            "/users/login",
            {
                "login_id": values["MATTERMOST_ADMIN_USERNAME"],
                "password": values["MATTERMOST_ADMIN_PASSWORD"],
            },
        )
        token = headers["Token"]
        try:
            teams, _ = api(base, "GET", "/users/me/teams", token=token)
            team = next(t for t in teams if t["name"] == "dq-monitor")
            channel, _ = api(base, "GET", f"/teams/{team['id']}/channels/name/alerts", token=token)
            posts, _ = api(
                base, "GET", f"/channels/{channel['id']}/posts?per_page=100", token=token
            )
            matches = [p["id"] for p in posts["posts"].values() if marker in p["message"]]
            assert len(matches) == 2, ("Mattermost", len(matches))
            report["mattermost_post_ids"] = matches
        finally:
            api(base, "POST", "/users/logout", {}, token)
        report.update(
            incident_id=incident_id,
            check_id=check_id,
            incident_state="closed",
            result="PASS",
            demo_rows=10,
        )
        report["services"] = subprocess.check_output(
            [sys.executable, "scripts/mvp.py", "status"], encoding="utf-8", errors="replace"
        )
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Приёмка PASS. Отчёт: " + str(output))
    finally:
        # Даже при сбое сценария собственные демонстрационные данные восстанавливаются.
        with warehouse.begin() as c:
            c.execute(text("UPDATE dq_demo.orders SET amount=id*100 WHERE id IN (1,2)"))
        warehouse.dispose()
        engine.dispose()


if __name__ == "__main__":
    main()
