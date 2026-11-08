"""SMTP и Mattermost: одна попытка доставки с ограниченным временем ожидания."""

import json
import smtplib
import ssl
from email.message import EmailMessage
from urllib import request
from urllib.parse import urlsplit

from dq_monitor.notifications.settings import NotificationSettings


class DeliveryError(RuntimeError):
    """Ошибка доставки; исходный текст исключения никогда не сохраняется в outbox."""


class NoRedirect(request.HTTPRedirectHandler):
    """Webhook не перенаправляется на другой адрес."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_email(message: str, settings: NotificationSettings) -> None:
    """SMTP по умолчанию без TLS для локального демо, SSL/STARTTLS для реальной почты."""
    if not settings.recipients:
        raise DeliveryError("Не заданы получатели")
    mail = EmailMessage()
    mail["Subject"] = "dq-monitor: событие инцидента"
    mail["From"] = settings.email_from
    mail["To"] = ", ".join(settings.recipients)
    mail.set_content(message)
    smtp_class = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
    kwargs = {"timeout": settings.timeout_sec}
    if settings.smtp_ssl:
        kwargs["context"] = ssl.create_default_context()
    with smtp_class(settings.smtp_host, settings.smtp_port, **kwargs) as smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            password = settings.smtp_password
            smtp.login(settings.smtp_username, password.get_secret_value() if password else "")
        refused = smtp.send_message(
            mail, from_addr=settings.email_from, to_addrs=list(settings.recipients)
        )
        if refused:
            raise DeliveryError("SMTP отклонил часть получателей")


def send_mattermost(message: str, settings: NotificationSettings) -> None:
    """POST incoming webhook; URL и ответ сервера не попадают в журнал ошибок."""
    secret = settings.mattermost_webhook
    if secret is None:
        raise DeliveryError("Не задан webhook")
    url = secret.get_secret_value()
    if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
        raise DeliveryError("Некорректный webhook")
    req = request.Request(
        url,
        data=json.dumps({"text": message}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.build_opener(NoRedirect()).open(req, timeout=settings.timeout_sec) as response:
        if not 200 <= response.status < 300:
            raise DeliveryError("Webhook отклонил запрос")


def deliver(channel: str, message: str, settings: NotificationSettings) -> None:
    """Отправляет сообщение, не перехватывая исключения транспорта."""
    if channel == "email":
        send_email(message, settings)
    elif channel == "mattermost":
        send_mattermost(message, settings)
    else:
        raise DeliveryError("Неизвестный канал")


def sanitized_error(exc: Exception) -> str:
    """Только категория из фиксированного списка, без URL, пароля и тела ответа."""
    if isinstance(exc, TimeoutError):
        return "Превышено время ожидания доставки"
    if isinstance(exc, smtplib.SMTPException):
        return "Ошибка SMTP"
    return "Ошибка доставки уведомления"
