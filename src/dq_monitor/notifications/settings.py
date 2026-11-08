"""Изолированные настройки worker. Ядро очереди этот модуль не импортирует."""

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from dq_monitor.notifications.outbox import CHANNELS


class NotificationSettings(BaseSettings):
    """NOTIFY_CHANNELS задаётся списком через запятую: email,mattermost."""

    model_config = SettingsConfigDict(
        env_prefix="NOTIFY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    enabled: bool = False
    channels: str = "email,mattermost"
    smtp_host: str = "localhost"
    smtp_port: int = Field(default=1026, ge=1, le=65535)
    smtp_starttls: bool = False
    smtp_ssl: bool = False
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    email_from: str = Field(
        default="dq-monitor@localhost",
        validation_alias=AliasChoices("NOTIFY_SMTP_FROM", "NOTIFY_EMAIL_FROM"),
    )
    email_to: str = Field(
        default="",
        validation_alias=AliasChoices("NOTIFY_SMTP_TO", "NOTIFY_EMAIL_TO"),
    )
    mattermost_webhook: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("NOTIFY_MATTERMOST_WEBHOOK_URL", "NOTIFY_MATTERMOST_WEBHOOK"),
    )
    timeout_sec: float = Field(default=10, gt=0, le=300)
    max_attempts: int = Field(default=5, ge=1, le=100)
    retry_base_sec: float = Field(default=30, gt=0, le=86400)
    retry_max_sec: float = Field(default=3600, gt=0, le=604800)
    poll_interval_sec: float = Field(default=5, gt=0, le=60)
    batch_size: int = Field(default=100, ge=1, le=10000)

    @field_validator("channels")
    @classmethod
    def validate_channels(cls, value: str) -> str:
        channels = tuple(part.strip() for part in value.split(",") if part.strip())
        if not channels or not set(channels) <= set(CHANNELS):
            raise ValueError("Допустимые каналы: email,mattermost")
        return ",".join(dict.fromkeys(channels))

    @model_validator(mode="after")
    def validate_tls(self):
        if self.smtp_starttls and self.smtp_ssl:
            raise ValueError("Выберите SMTP SSL или STARTTLS")
        return self

    @property
    def channel_names(self) -> tuple[str, ...]:
        return tuple(self.channels.split(","))

    @property
    def recipients(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.email_to.split(",") if part.strip())
