"""Outbox на отдельной временной БД; SMTP и HTTP заменены mock, сети доставки нет."""

import json
import os
import smtplib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError, URLError
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dq_monitor.checks import Outcome
from dq_monitor.config import get_settings
from dq_monitor.coordination import run_lock
from dq_monitor.meta.models import (
    Base,
    CheckResult,
    Connection,
    Dataset,
    DqCheck,
    Incident,
    Notification,
    Run,
)
from dq_monitor.notifications import transports, worker
from dq_monitor.notifications.__main__ import main
from dq_monitor.notifications.outbox import queue_event
from dq_monitor.notifications.settings import NotificationSettings
from dq_monitor.runner import run_checks, update_incident

NOW = datetime(2025, 1, 2, tzinfo=UTC)
PREVIOUS = "c3f5b7d9e021"


def settings(**kwargs):
    return NotificationSettings(_env_file=None, **kwargs)


def migrate(engine, action="upgrade", revision="head"):
    cfg = Config()
    cfg.set_main_option("script_location", "dq_monitor.meta:migrations")
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        getattr(command, action)(cfg, revision)


@pytest.fixture(scope="module")
def engine():
    """Уникальная БД на каждый запуск, рабочие meta и warehouse не меняются."""
    url = get_settings().meta_url
    name = f"dq_notify_test_{uuid4().hex}"
    assert name not in (url.database, get_settings().warehouse_db)
    admin = create_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 3},
    )
    eng = create_engine(url.set(database=name))
    created = False
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        created = True
        migrate(eng)
        yield eng
    finally:
        eng.dispose()
        if created:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def fresh(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE notification, incident, check_result, run, dq_check, "
                "dataset, connection RESTART IDENTITY CASCADE"
            )
        )
    with Session(engine) as session, session.begin():
        source = Connection(
            name="test",
            db_type="postgres",
            host="unused",
            port=1,
            database="unused",
            username="unused",
            password_env="UNUSED",
        )
        dataset = Dataset(connection=source, schema_name="dwh", table_name="facts")
        session.add(DqCheck(dataset=dataset, name="volume", check_type="row_count"))
    return engine


def transition(session, status):
    run = Run(logical_now=NOW)
    result = CheckResult(run=run, check_id=1, status=status, message=f"Итог: {status}")
    session.add(result)
    session.flush()
    return update_incident(session, 1, result, NOW)


def queue(engine, close=False):
    with Session(engine) as session, session.begin():
        assert transition(session, "fail") == "opened"
        if close:
            assert transition(session, "pass") == "closed"


def rows(engine):
    with Session(engine) as session:
        return session.scalars(select(Notification).order_by(Notification.id)).all()


def due(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE notification SET next_attempt_at = now() - interval '1 second' "
                "WHERE next_attempt_at IS NOT NULL"
            )
        )


def test_settings_defaults_and_secrets(monkeypatch):
    monkeypatch.setenv("NOTIFY_MATTERMOST_WEBHOOK", "https://mm/hooks/secret")
    config = settings()
    assert not config.enabled
    assert (config.smtp_host, config.smtp_port) == ("localhost", 1026)
    assert not config.smtp_starttls and not config.smtp_ssl
    assert config.channel_names == ("email", "mattermost")
    assert isinstance(config.mattermost_webhook, SecretStr)
    assert "https://mm/hooks/secret" not in repr(config)


def test_settings_env(monkeypatch):
    monkeypatch.setenv("NOTIFY_ENABLED", "true")
    monkeypatch.setenv("NOTIFY_CHANNELS", " mattermost, mattermost ")
    monkeypatch.setenv("NOTIFY_TIMEOUT_SEC", "2")
    config = settings()
    assert config.enabled and config.channel_names == ("mattermost",)
    assert config.timeout_sec == 2


def test_compose_env_names(monkeypatch):
    monkeypatch.setenv("NOTIFY_SMTP_FROM", "dq-monitor@example.test")
    monkeypatch.setenv("NOTIFY_SMTP_TO", "demo@example.test")
    monkeypatch.setenv("NOTIFY_MATTERMOST_WEBHOOK_URL", "https://mm/hooks/demo")
    config = settings()
    assert config.email_from == "dq-monitor@example.test"
    assert config.recipients == ("demo@example.test",)
    assert config.mattermost_webhook.get_secret_value() == "https://mm/hooks/demo"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"channels": "telegram"},
        {"channels": ""},
        {"max_attempts": 0},
        {"timeout_sec": 0},
        {"smtp_ssl": True, "smtp_starttls": True},
    ],
)
def test_invalid_settings(kwargs):
    with pytest.raises(ValidationError):
        settings(**kwargs)


def test_backoff_is_capped():
    config = settings(retry_base_sec=2, retry_max_sec=10)
    assert [worker.retry_delay(n, config) for n in (1, 2, 3, 4, 10000)] == [2, 4, 8, 10, 10]


@pytest.mark.parametrize("tls", ["plain", "starttls", "ssl"])
def test_smtp_transport(monkeypatch, tls):
    factory = MagicMock()
    smtp = factory.return_value.__enter__.return_value
    smtp.send_message.return_value = {}
    monkeypatch.setattr(transports.smtplib, "SMTP", factory)
    monkeypatch.setattr(transports.smtplib, "SMTP_SSL", factory)
    config = settings(
        email_from="dq@example.org",
        email_to="a@example.org, b@example.org",
        smtp_starttls=tls == "starttls",
        smtp_ssl=tls == "ssl",
        smtp_username="user",
        smtp_password="secret",
        timeout_sec=2,
    )
    transports.send_email("Инцидент открыт", config)
    assert factory.call_args.args == ("localhost", 1026)
    assert factory.call_args.kwargs["timeout"] == 2
    assert ("context" in factory.call_args.kwargs) == (tls == "ssl")
    assert smtp.starttls.called == (tls == "starttls")
    smtp.login.assert_called_once_with("user", "secret")
    mail = smtp.send_message.call_args.args[0]
    assert mail["From"] == "dq@example.org"
    assert mail.get_content().strip() == "Инцидент открыт"
    assert smtp.send_message.call_args.kwargs["to_addrs"] == ["a@example.org", "b@example.org"]


def test_smtp_missing_and_refused_recipients(monkeypatch):
    with pytest.raises(transports.DeliveryError):
        transports.send_email("text", settings())
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value.send_message.return_value = {"a@b": (550, b"no")}
    monkeypatch.setattr(transports.smtplib, "SMTP", smtp)
    with pytest.raises(transports.DeliveryError):
        transports.send_email("text", settings(email_to="a@b"))


def test_mattermost_transport(monkeypatch):
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.status = 200
    factory = Mock(return_value=opener)
    monkeypatch.setattr(transports.request, "build_opener", factory)
    transports.send_mattermost(
        "Инцидент открыт", settings(mattermost_webhook="https://mm/hooks/secret", timeout_sec=3)
    )
    req = opener.open.call_args.args[0]
    assert req.full_url == "https://mm/hooks/secret"
    assert req.method == "POST" and req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == {"text": "Инцидент открыт"}
    assert opener.open.call_args.kwargs == {"timeout": 3}
    assert isinstance(factory.call_args.args[0], transports.NoRedirect)
    handler = factory.call_args.args[0]
    assert handler.redirect_request(None, None, 302, "", {}, "https://bad") is None


@pytest.mark.parametrize("webhook", [None, "file:///secret", "broken"])
def test_missing_or_invalid_webhook(webhook):
    with pytest.raises(transports.DeliveryError):
        transports.send_mattermost("text", settings(mattermost_webhook=webhook))


def test_http_failure(monkeypatch):
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.status = 500
    monkeypatch.setattr(transports.request, "build_opener", Mock(return_value=opener))
    with pytest.raises(transports.DeliveryError):
        transports.send_mattermost("text", settings(mattermost_webhook="https://mm/hooks/secret"))


@pytest.mark.parametrize(
    "exc",
    [
        URLError("https://mm/hooks/secret"),
        HTTPError("https://mm/hooks/secret", 403, "secret", {}, None),
        smtplib.SMTPAuthenticationError(535, b"secret"),
        TimeoutError("secret"),
        ValueError("secret"),
    ],
)
def test_errors_are_sanitized(exc):
    assert "secret" not in transports.sanitized_error(exc)
    assert "https" not in transports.sanitized_error(exc)


def test_disabled_worker_does_not_connect():
    engine = Mock()
    assert worker.process_once(engine, settings(enabled=False)) == 0
    engine.connect.assert_not_called()


def test_cli_disabled_and_invalid_env(monkeypatch, capsys):
    monkeypatch.setenv("NOTIFY_ENABLED", "false")
    assert main(["--once"]) == 0
    monkeypatch.setenv("NOTIFY_ENABLED", "https://mm/hooks/secret")
    assert main(["--once"]) == 1
    assert "secret" not in capsys.readouterr().err


@pytest.mark.parametrize("args", [[], ["--loop"], ["--once"]])
def test_cli_loop(monkeypatch, args):
    cli = worker

    engine = Mock()
    process = Mock(return_value=0)
    monkeypatch.setattr(cli, "NotificationSettings", lambda: settings(enabled=True))
    monkeypatch.setattr(cli, "meta_engine", lambda: engine)
    monkeypatch.setattr(cli, "process_once", process)
    monkeypatch.setattr(cli.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    assert main(args) == 0
    assert process.call_count == 1
    engine.dispose.assert_called_once()


def test_lifecycle_and_invalid_env_do_not_affect_queue(fresh, monkeypatch):
    monkeypatch.setenv("NOTIFY_ENABLED", "invalid")
    monkeypatch.setenv("NOTIFY_TIMEOUT_SEC", "invalid")
    with Session(fresh) as session, session.begin():
        assert transition(session, "fail") == "opened"
        assert transition(session, "fail") is None
        assert transition(session, "error") is None
        assert transition(session, "skip") is None
        assert transition(session, "pass") == "closed"
        assert transition(session, "pass") is None
    saved = rows(fresh)
    assert [(r.channel, r.event) for r in saved] == [
        ("email", "opened"),
        ("mattermost", "opened"),
        ("email", "closed"),
        ("mattermost", "closed"),
    ]
    assert all(r.status == "pending" and r.attempts == 0 and r.sent_at is None for r in saved)
    assert all(r.created_at and r.next_attempt_at and r.last_attempt_at is None for r in saved)
    assert all("volume" in r.message and "dwh.facts" in r.message for r in saved)
    assert "Итог: fail" in saved[0].message and "Итог: pass" in saved[-1].message


def test_atomic_rollback(fresh):
    with Session(fresh) as session:
        transition(session, "fail")
        assert len(session.scalars(select(Notification)).all()) == 2
        session.rollback()
    with Session(fresh) as session:
        assert session.scalar(select(func.count()).select_from(Incident)) == 0
        assert session.scalar(select(func.count()).select_from(CheckResult)) == 0
    assert rows(fresh) == []


def test_close_rollback_and_new_incident(fresh):
    queue(fresh)
    with Session(fresh) as session:
        transition(session, "pass")
        session.rollback()
    assert len(rows(fresh)) == 2
    with Session(fresh) as session, session.begin():
        assert session.get(Incident, 1).state == "open"
        transition(session, "pass")
        transition(session, "fail")
    assert len(rows(fresh)) == 6


def test_queue_idempotence_and_unique_constraint(fresh):
    queue(fresh)
    with Session(fresh) as session, session.begin():
        incident = session.get(Incident, 1)
        result = session.get(CheckResult, incident.opened_result_id)
        queue_event(session, incident, "opened", result)
    assert len(rows(fresh)) == 2
    with pytest.raises(IntegrityError), Session(fresh) as session, session.begin():
        session.add(Notification(incident_id=1, channel="email", event="opened"))


def test_worker_success_order_and_snapshot(fresh, monkeypatch):
    queue(fresh, close=True)
    with Session(fresh) as session, session.begin():
        session.get(DqCheck, 1).name = "renamed"
    deliver = Mock()
    monkeypatch.setattr(worker, "deliver", deliver)
    assert worker.process_once(fresh, settings(enabled=True)) == 4
    assert [call.args[0] for call in deliver.call_args_list] == [
        "email",
        "mattermost",
        "email",
        "mattermost",
    ]
    assert all(
        "volume" in call.args[1] and "renamed" not in call.args[1]
        for call in deliver.call_args_list
    )
    assert all(
        r.status == "sent"
        and r.sent_at
        and r.last_attempt_at
        and r.attempts == 1
        and r.next_attempt_at is None
        for r in rows(fresh)
    )
    assert worker.process_once(fresh, settings(enabled=True)) == 0


def test_attempt_is_committed_before_network(fresh, monkeypatch):
    queue(fresh)
    config = settings(enabled=True, channels="email", timeout_sec=3)

    def deliver(channel, message, config):
        opened = rows(fresh)[0]  # отдельное подключение видит уже зафиксированную попытку
        assert opened.status == "pending" and opened.attempts == 1
        assert opened.next_attempt_at >= opened.last_attempt_at + timedelta(seconds=3)

    monkeypatch.setattr(worker, "deliver", deliver)
    assert worker.process_once(fresh, config) == 1
    assert rows(fresh)[0].status == "sent"


@pytest.mark.parametrize("module", ["dq_monitor.notifications.worker", "dq_monitor.notifications"])
def test_cli_once_real_process(fresh, module):
    result = subprocess.run(
        [sys.executable, "-m", module, "--once"],
        env=os.environ
        | {
            "META_DB": fresh.url.database,
            "NOTIFY_ENABLED": "true",
            "NOTIFY_CHANNELS": "email",
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "Попыток доставки: 0" in result.stdout
    assert "RuntimeWarning" not in result.stderr


def test_retry_exhaustion_blocks_close_only_for_failed_channel(fresh, monkeypatch):
    queue(fresh, close=True)

    def deliver(channel, message, config):
        if channel == "email":
            raise URLError("https://mm/hooks/secret")

    monkeypatch.setattr(worker, "deliver", deliver)
    config = settings(enabled=True, max_attempts=2, retry_base_sec=10, retry_max_sec=10)
    start = datetime.now(UTC)
    assert worker.process_once(fresh, config) == 3
    email_open, mm_open, email_close, mm_close = rows(fresh)
    assert email_open.status == "failed" and email_open.attempts == 1
    assert email_open.next_attempt_at >= start + timedelta(seconds=10)
    assert "secret" not in email_open.error
    assert mm_open.status == mm_close.status == "sent"
    assert email_close.status == "pending" and email_close.attempts == 0
    assert worker.process_once(fresh, config) == 0
    due(fresh)
    assert worker.process_once(fresh, config) == 1
    email_open = rows(fresh)[0]
    assert email_open.attempts == 2 and email_open.next_attempt_at is None
    assert email_open.sent_at is None
    assert worker.process_once(fresh, config) == 0


def test_transient_failure_then_success(fresh, monkeypatch):
    queue(fresh, close=True)
    deliver = Mock(side_effect=[TimeoutError("secret"), None, None])
    monkeypatch.setattr(worker, "deliver", deliver)
    config = settings(enabled=True, channels="email")
    assert worker.process_once(fresh, config) == 1
    due(fresh)
    assert worker.process_once(fresh, config) == 2
    saved = rows(fresh)
    assert saved[0].status == saved[2].status == "sent"
    assert saved[0].attempts == 2 and saved[0].error is None
    assert saved[1].attempts == saved[3].attempts == 0


def test_worker_lock_and_runner_lock_are_independent(fresh):
    config = settings(enabled=True)
    with worker.worker_lock(fresh):
        with pytest.raises(worker.WorkerInProgressError):
            worker.process_once(fresh, config)
        with run_lock(fresh):
            pass
    with run_lock(fresh):
        assert worker.process_once(fresh, config) == 0


def test_crash_after_delivery_before_commit_can_duplicate(fresh, monkeypatch):
    queue(fresh)

    class Crash(BaseException):
        pass

    deliver = Mock(side_effect=Crash)
    monkeypatch.setattr(worker, "deliver", deliver)
    config = settings(enabled=True, channels="email")
    with pytest.raises(Crash):
        worker.process_once(fresh, config)
    assert rows(fresh)[0].status == "pending" and rows(fresh)[0].attempts == 1
    assert rows(fresh)[0].last_attempt_at and rows(fresh)[0].next_attempt_at > datetime.now(UTC)
    deliver.side_effect = None
    assert worker.process_once(fresh, config) == 0
    due(fresh)
    assert worker.process_once(fresh, config) == 1
    assert deliver.call_count == 2


def test_last_crashed_attempt_recovers_to_terminal_failed(fresh, monkeypatch):
    queue(fresh, close=True)

    class Crash(BaseException):
        pass

    monkeypatch.setattr(worker, "deliver", Mock(side_effect=Crash))
    config = settings(enabled=True, channels="email", max_attempts=1)
    with pytest.raises(Crash):
        worker.process_once(fresh, config)
    assert rows(fresh)[0].status == "pending"
    assert worker.process_once(fresh, config) == 0
    due(fresh)
    assert worker.process_once(fresh, config) == 0
    opened, _, closed, _ = rows(fresh)
    assert opened.status == "failed" and opened.attempts == 1 and opened.next_attempt_at is None
    assert closed.status == "pending" and closed.attempts == 0


def test_runner_records_incidents_without_delivery(fresh, monkeypatch):
    monkeypatch.setenv("NOTIFY_CHANNELS", "invalid")
    outcomes = iter(["fail", "fail", "pass"])
    monkeypatch.setattr(
        "dq_monitor.runner._execute", lambda *a: (Outcome(next(outcomes), message="test"), 0, None)
    )
    delivery = Mock(side_effect=AssertionError("Runner не должен отправлять сообщения"))
    monkeypatch.setattr(worker, "deliver", delivery)
    for event in ("opened", None, "closed"):
        report = run_checks(fresh, NOW)
        assert report.items[0].incident_event == event
    assert len(rows(fresh)) == 4
    delivery.assert_not_called()


def test_transports_integrated_with_worker(fresh, monkeypatch):
    queue(fresh)
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value.send_message.return_value = {}
    monkeypatch.setattr(transports.smtplib, "SMTP", smtp)
    opener = MagicMock()
    opener.open.side_effect = HTTPError("https://mm/hooks/secret", 500, "secret", {}, None)
    monkeypatch.setattr(transports.request, "build_opener", Mock(return_value=opener))
    assert (
        worker.process_once(
            fresh,
            settings(enabled=True, email_to="a@b", mattermost_webhook="https://mm/hooks/secret"),
        )
        == 2
    )
    email, mm = rows(fresh)
    assert email.status == "sent" and mm.status == "failed"
    assert "secret" not in mm.error and mm.attempts == 1


def test_migration_preserves_telegram_history_and_matches_model(fresh):
    # До миграции: две исторические записи sent/failed с прежним sent_at NOT NULL.
    with Session(fresh) as session, session.begin():
        transition(session, "fail")
        session.execute(text("DELETE FROM notification"))
    migrate(fresh, "downgrade", PREVIOUS)
    with fresh.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO notification (incident_id, channel, event, status, error, sent_at) "
                "VALUES (1, 'telegram', 'opened', 'sent', NULL, :now), "
                "(1, 'telegram', 'closed', 'failed', 'old error', :now)"
            ),
            {"now": NOW},
        )
    migrate(fresh)
    sent, failed = rows(fresh)
    assert (sent.channel, sent.status, sent.sent_at) == ("telegram", "sent", NOW)
    assert failed.status == "failed" and failed.sent_at is None and failed.error == "old error"
    assert all(
        r.created_at == NOW and r.attempts == 1 and r.next_attempt_at is None
        for r in (sent, failed)
    )
    with fresh.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    assert worker.process_once(fresh, settings(enabled=True)) == 0
    migrate(fresh, "downgrade", PREVIOUS)
    with fresh.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM notification WHERE sent_at IS NOT NULL")) == 2
    migrate(fresh)


@pytest.mark.parametrize("source", ["legacy_duplicates", "applied_d4_full_unique"])
def test_upgrade_preserves_telegram_duplicates_and_repairs_applied_d4(fresh, source):
    with Session(fresh) as session, session.begin():
        transition(session, "fail")
        session.execute(text("DELETE FROM notification"))
    migrate(fresh, "downgrade", PREVIOUS)
    with fresh.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO notification (incident_id, channel, event, status, sent_at) "
                "VALUES (1, 'telegram', 'opened', 'failed', :now)"
            ),
            {"now": NOW},
        )
        if source == "legacy_duplicates":
            conn.execute(
                text(
                    "INSERT INTO notification (incident_id, channel, event, status, sent_at) "
                    "VALUES (1, 'telegram', 'opened', 'sent', :now)"
                ),
                {"now": NOW},
            )
    migrate(fresh, revision="d4a6c8e0f132")
    if source == "applied_d4_full_unique":
        # Воспроизводим исходную уже применённую d4a6, не меняя рабочую meta.
        with fresh.begin() as conn:
            conn.execute(text("DROP INDEX uq_notification_outbox_event"))
            conn.execute(
                text(
                    "ALTER TABLE notification ADD CONSTRAINT "
                    "uq_notification_incident_id_channel_event UNIQUE (incident_id, channel, event)"
                )
            )
    migrate(fresh)
    if source == "applied_d4_full_unique":
        with fresh.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO notification (incident_id, channel, event, status, sent_at) "
                    "VALUES (1, 'telegram', 'opened', 'sent', :now)"
                ),
                {"now": NOW},
            )
    saved = rows(fresh)
    assert [(row.channel, row.event, row.status) for row in saved] == [
        ("telegram", "opened", "failed"),
        ("telegram", "opened", "sent"),
    ]
    with fresh.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    # Повторный запуск e5 безопасен и при уже существующем partial index.
    migrate(fresh, "downgrade", "d4a6c8e0f132")
    migrate(fresh)
    assert len(rows(fresh)) == 2
    with Session(fresh) as session, session.begin():
        transition(session, "pass")
    assert len(rows(fresh)) == 4


def test_close_of_legacy_open_incident_delivers_without_channel_opened(fresh, monkeypatch):
    with Session(fresh) as session, session.begin():
        transition(session, "fail")
        session.execute(text("DELETE FROM notification"))
        session.add(
            Notification(
                incident_id=1,
                channel="telegram",
                event="opened",
                status="sent",
                sent_at=NOW,
            )
        )
    with Session(fresh) as session, session.begin():
        assert transition(session, "pass") == "closed"
    deliver = Mock()
    monkeypatch.setattr(worker, "deliver", deliver)
    assert worker.process_once(fresh, settings(enabled=True)) == 2
    assert [call.args[0] for call in deliver.call_args_list] == ["email", "mattermost"]
    assert all(
        "closed" in call.args[1] and "Итог: pass" in call.args[1] for call in deliver.call_args_list
    )
    assert all(row.status == "sent" for row in rows(fresh))
