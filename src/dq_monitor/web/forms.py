"""Валидация веб-форм и описание полей на основе моделей параметров ядра."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from dq_monitor.checks import CHECK_CLASSES, create_check
from dq_monitor.scheduling import validate_schedule

CHECK_LABELS = {
    "not_null": "Полнота: доля NULL",
    "unique": "Уникальность ключа",
    "freshness": "Свежесть загрузки",
    "row_count": "Объём данных",
    "range": "Границы и допустимые значения",
    "referential": "Ссылочная целостность",
    "custom_sql": "Пользовательский SQL",
    "anomaly": "Аномалии по истории",
}
FIELD_LABELS = {
    "scope": "Область проверки",
    "column": "Столбец",
    "columns": "Столбцы ключа через запятую",
    "max_null_ratio": "Допустимая доля NULL (0–1)",
    "max_duplicates": "Допустимое число дублей",
    "max_age_hours": "Допустимый возраст, часов",
    "min": "Нижняя граница",
    "max": "Верхняя граница",
    "values": "Допустимые значения — JSON-список",
    "max_bad_ratio": "Допустимая доля нарушений (0–1)",
    "ref_table": "Родительская таблица: схема.таблица",
    "ref_column": "Столбец родительской таблицы",
    "max_orphans": "Допустимое число висячих ссылок",
    "sql": "SQL, возвращающий одно число нарушений",
    "max_bad": "Допустимое число нарушений",
    "metric": "Метрика",
    "window": "Окно истории, дней",
    "threshold": "Порог |z|",
    "min_history": "Минимум точек истории (пусто = полное окно)",
}


class FormModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CheckFields(FormModel):
    name: str = Field(min_length=1, max_length=200)
    dataset_id: int = Field(gt=0)
    check_type: str
    severity: Literal["warning", "critical"] = "critical"
    schedule: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=4000)
    is_active: bool = True


class SourceFields(FormModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=1, max_length=100)
    password_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$", max_length=100)


class DatasetFields(FormModel):
    connection_id: int = Field(gt=0)
    schema_name: str = Field(min_length=1, max_length=100)
    table_name: str = Field(min_length=1, max_length=100)
    batch_column: str | None = Field(None, max_length=100)
    loaded_at_column: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=4000)


def check_form(form):
    """Поля формы не дублируют правила валидации восьми типов: вызываются модели ядра."""
    values = CheckFields.model_validate(
        {
            "name": form.get("name"),
            "dataset_id": form.get("dataset_id"),
            "check_type": form.get("check_type"),
            "severity": form.get("severity", "critical"),
            "schedule": form.get("schedule", "").strip() or None,
            "description": form.get("description", "").strip() or None,
            "is_active": form.get("is_active") == "on",
        }
    )
    validate_schedule(values.schedule)
    if values.check_type not in CHECK_CLASSES:
        raise ValueError("Неизвестный тип проверки")
    known = CHECK_CLASSES[values.check_type].Params.model_fields
    params = {}
    for key, value in form.items():
        if not key.startswith("p_") or value == "":
            continue
        name = key[2:]
        if name not in known:
            raise ValueError(f"Неизвестный параметр: {name}")
        if name == "columns":
            value = [v.strip() for v in value.split(",") if v.strip()]
        elif name == "values":
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise ValueError(
                    'Допустимые значения задаются списком, например ["O", "F", "P"]'
                ) from exc
        params[name] = value
    implementation = create_check(values.check_type, params)
    normalized = implementation.params.model_dump(mode="json", exclude_none=True)
    json.dumps(normalized, allow_nan=False)  # NaN/Infinity не должны попадать в JSONB/график
    return values.model_dump() | {"params": normalized}


def parameter_fields(current_type="row_count", params=None, posted=None):
    """Поля всех шаблонов. В браузере активен только выбранный fieldset."""
    groups = {}
    for kind, cls in CHECK_CLASSES.items():
        schema = cls.Params.model_json_schema()
        fields = []
        for name, prop in schema["properties"].items():
            value = prop.get("default", "")
            if kind == current_type:
                value = (params or {}).get(name, value)
                if posted is not None:
                    value = posted.get("p_" + name, "")
            if isinstance(value, list):
                value = (
                    ", ".join(value) if name == "columns" else json.dumps(value, ensure_ascii=False)
                )
            options = prop.get("enum") or ([prop["const"]] if "const" in prop else [])
            fields.append(
                {
                    "name": name,
                    "label": FIELD_LABELS.get(name, name),
                    "value": "" if value is None else value,
                    "options": options,
                    "required": name in schema.get("required", []),
                    "textarea": name in ("sql", "values"),
                }
            )
        groups[kind] = fields
    return groups
