"""Превращение исключений в короткие понятные сообщения для пользователя."""

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError


def error_text(exc: Exception) -> str:
    """Короткое понятное описание ошибки для результата проверки."""
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        return f"Ошибка СУБД: {str(exc.orig).strip().splitlines()[0]}"
    if isinstance(exc, ValidationError):
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'params'}: {e['msg']}" for e in exc.errors()
        )
        return f"Неверные параметры: {problems}"
    # Свои ошибки ядро формулирует по-русски, тип исключения в них только мешает.
    # Для неожиданных ошибок тип оставляем: он подсказывает, где искать причину
    if type(exc) in (ValueError, LookupError, NotImplementedError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"
