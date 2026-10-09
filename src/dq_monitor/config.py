"""Настройки приложения. Читаются из переменных окружения и файла .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


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
