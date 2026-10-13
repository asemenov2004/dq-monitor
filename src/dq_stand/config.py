"""Настройки полигона. Читаются из переменных окружения и файла .env."""

from datetime import date
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class StandSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STAND_", env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    scale_factor: float = 1
    start_date: date = date(2025, 1, 1)


@lru_cache
def get_stand_settings() -> StandSettings:
    return StandSettings()
