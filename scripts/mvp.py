"""Запуск локального MVP и идемпотентная настройка канала Mattermost.

Запускать из активированного venv: python scripts/mvp.py up [--airflow].
Секреты остаются в игнорируемом .env. Скрипт не сбрасывает полигон и историю.
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key
from sqlalchemy import URL

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"


def local_url(values, name, default):
    """Порт из .env не может подменить localhost через userinfo или URL-путь."""
    port = str(values.get(name) or default)
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ValueError(f"{name}: требуется номер порта от 1 до 65535")
    return f"http://127.0.0.1:{int(port)}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(base, method, path, payload=None, token=None):
    target = urlsplit(base)
    if (
        target.scheme != "http"
        or target.hostname not in ("localhost", "127.0.0.1")
        or target.username
        or target.password
        or target.path
        or target.query
        or target.fragment
    ):
        raise ValueError("Bootstrap обращается только к локальному Mattermost")
    local_url({"port": target.port}, "port", "8065")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        base + "/api/v4" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=10) as response:
        body = response.read()
        return json.loads(body) if body else {}, response.headers


def prepare_env():
    if not ENV.exists():
        raise SystemExit("Сначала скопируйте .env.example в .env")
    values = dict(dotenv_values(ENV))
    for name, default in (
        ("MATTERMOST_PORT", 8065),
        ("MAILPIT_PORT", 8025),
        ("MAILPIT_SMTP_PORT", 1026),
        ("WEB_PORT", 8000),
    ):
        local_url(values, name, default)
    defaults = {
        "MATTERMOST_DB_PASSWORD": secrets.token_urlsafe(32),
        "MATTERMOST_ADMIN_PASSWORD": secrets.token_urlsafe(24) + "Aa1!",
        "MATTERMOST_ADMIN_EMAIL": "admin@example.test",
        "MATTERMOST_ADMIN_USERNAME": "dqadmin",
        "NOTIFY_ENABLED": "true",
        "NOTIFY_SMTP_HOST": "localhost",
        "NOTIFY_SMTP_PORT": "1026",
        "NOTIFY_SMTP_FROM": "dq-monitor@example.test",
        "NOTIFY_SMTP_TO": "demo@example.test",
    }
    for key, value in defaults.items():
        if not values.get(key):
            set_key(str(ENV), key, value)
            values[key] = value
    # Команда up запускает именно локальную демонстрацию с двумя каналами.
    set_key(str(ENV), "NOTIFY_ENABLED", "true")
    values["NOTIFY_ENABLED"] = "true"
    dsn = URL.create(
        "postgresql",
        username="mattermost",
        password=values["MATTERMOST_DB_PASSWORD"],
        host="postgres",
        port=5432,
        database="mattermost",
        query={"sslmode": "disable", "connect_timeout": "10"},
    ).render_as_string(hide_password=False)
    set_key(str(ENV), "MATTERMOST_DB_DSN", dsn)
    return values


def bootstrap(values):
    base = local_url(values, "MATTERMOST_PORT", 8065)
    print("Ожидаю локальный Mattermost...", flush=True)
    for attempt in range(180):
        try:
            api(base, "GET", "/system/ping")
            break
        except (urllib.error.URLError, TimeoutError):
            if attempt == 179:
                raise SystemExit(
                    "Mattermost не запустился. Проверьте docker compose logs mattermost"
                ) from None
            time.sleep(2)
    login = {
        "login_id": values["MATTERMOST_ADMIN_USERNAME"],
        "password": values["MATTERMOST_ADMIN_PASSWORD"],
    }
    try:
        _, headers = api(base, "POST", "/users/login", login)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        api(
            base,
            "POST",
            "/users",
            {
                "email": values["MATTERMOST_ADMIN_EMAIL"],
                "username": values["MATTERMOST_ADMIN_USERNAME"],
                "password": values["MATTERMOST_ADMIN_PASSWORD"],
            },
        )
        _, headers = api(base, "POST", "/users/login", login)
    token = headers["Token"]
    try:
        teams, _ = api(base, "GET", "/users/me/teams", token=token)
        team = next((t for t in teams if t["name"] == "dq-monitor"), None)
        if team is None:
            team, _ = api(
                base,
                "POST",
                "/teams",
                {
                    "name": "dq-monitor",
                    "display_name": "DQ Monitor",
                    "type": "I",
                },
                token,
            )
        try:
            channel, _ = api(base, "GET", f"/teams/{team['id']}/channels/name/alerts", token=token)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            channel, _ = api(
                base,
                "POST",
                "/channels",
                {
                    "team_id": team["id"],
                    "name": "alerts",
                    "display_name": "Оповещения DQ",
                    "type": "O",
                },
                token,
            )
        hooks, _ = api(base, "GET", f"/hooks/incoming?team_id={team['id']}", token=token)
        hook = next((h for h in hooks if h["channel_id"] == channel["id"]), None)
        if hook is None:
            hook, _ = api(
                base,
                "POST",
                "/hooks/incoming",
                {
                    "channel_id": channel["id"],
                    "display_name": "DQ Monitor",
                    "channel_locked": True,
                },
                token,
            )
        set_key(str(ENV), "MATTERMOST_WEBHOOK_ID", hook["id"])
        set_key(str(ENV), "NOTIFY_MATTERMOST_WEBHOOK_URL", base + "/hooks/" + hook["id"])
        print("Mattermost: команда dq-monitor, канал alerts. Пароль администратора в .env.")
    finally:
        api(base, "POST", "/users/logout", {}, token)


def compose(args, *command):
    files = ["compose.yaml", "compose.web.yaml", "compose.notifications.yaml"]
    if args.airflow:
        files.append("compose.airflow.yaml")
    options = [part for file in files for part in ("-f", file)]
    # Окружение для Compose читается из .env; не выводим config с раскрытыми секретами.
    subprocess.run(["docker", "compose", *options, *command], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["up", "status", "stop"])
    parser.add_argument("--airflow", action="store_true", help="Также запускать локальный Airflow")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.command == "up":
        values = prepare_env()
        compose(args, "up", "-d", "--build", "web", "mattermost", "mailpit")
        bootstrap(values)
        compose(args, "up", "-d", "--force-recreate", "notifications")
        if args.airflow:
            compose(
                args,
                "up",
                "-d",
                "--build",
                "airflow-api",
                "airflow-scheduler",
                "airflow-dag-processor",
            )
        print("MVP: http://localhost:" + values.get("WEB_PORT", "8000"))
        print("Почта: http://localhost:" + values.get("MAILPIT_PORT", "8025"))
        print("Чат: http://localhost:" + values.get("MATTERMOST_PORT", "8065"))
    elif args.command == "status":
        compose(args, "ps")
    else:
        compose(args, "stop")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Локальный Mattermost ответил HTTP {exc.code}; секреты не выведены."
        ) from None
