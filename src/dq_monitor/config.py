"""Настройки приложения. Читаются из переменных окружения и файла .env."""

import os
from functools import lru_cache

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class ConnectionRoute(BaseModel):
    """Переадресация того же источника внутри Docker, без изменения его метаданных."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)


class Settings(BaseSettings):
    # frozen: объект настроек общий на весь процесс (см. get_settings), менять его нельзя
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_user: str
    postgres_password: str

    warehouse_db: str = "warehouse"
    meta_db: str = "meta"

    # Предельное время одного запроса проверки: тяжёлый или зависший запрос не подвесит прогон
    check_statement_timeout_sec: int = 60
    connection_overrides: dict[str, ConnectionRoute] = Field(
        default_factory=dict, validation_alias="DQ_CONNECTION_OVERRIDES"
    )

    def _url(self, database: str) -> URL:
        # URL.create сам экранирует спецсимволы в пароле
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=database,
        )

    @property
    def warehouse_url(self) -> URL:
        return self._url(self.warehouse_db)

    @property
    def meta_url(self) -> URL:
        return self._url(self.meta_db)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_secret(name: str) -> str:
    """Значение секрета по имени переменной: сначала из окружения, затем из файла .env.

    pydantic-settings читает .env только в поля Settings и не кладёт его в окружение,
    поэтому произвольные переменные (например, пароль из connection.password_env) ищем сами.
    """
    value = os.environ.get(name) or dotenv_values(".env").get(name)
    if not value:
        raise LookupError(f"Не задана переменная окружения {name}")
    return value
