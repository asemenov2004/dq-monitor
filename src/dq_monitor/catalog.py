"""Загрузка набора проверок из YAML-файла в базу метаданных.

Набор проверок хранится в файле под контролем версий: так видно, какие проверки
и пороги действовали в каждый момент. Загрузка идемпотентна: новые проверки
добавляются, изменившиеся обновляются, совпадающие не трогаются. Если в файле
есть хотя бы одна ошибка, в базу не записывается ничего.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from dq_monitor.checks import create_check
from dq_monitor.errors import error_text
from dq_monitor.meta.models import SEVERITIES, Connection, Dataset, DqCheck
from dq_monitor.scheduling import validate_schedule


class CheckSpec(BaseModel):
    """Описание одной проверки в файле."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    dataset: str = Field(pattern=r"^[^.\s]+\.[^.\s]+$")  # schema.table
    type: str
    severity: str = "critical"
    params: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    schedule: str | None = None
    active: bool = True


class CatalogFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection: str
    checks: list[CheckSpec] = Field(min_length=1)


class CatalogError(Exception):
    """Ошибки в файле проверок. problems содержит все найденные проблемы, а не только первую."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass
class LoadReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


def parse_catalog(text: str) -> CatalogFile:
    """Разбирает YAML и проверяет структуру файла и параметры каждой проверки."""
    try:
        raw = yaml.safe_load(text)
        catalog = CatalogFile.model_validate(raw)
    except yaml.YAMLError as exc:
        raise CatalogError([f"Файл не является корректным YAML: {exc}"]) from exc
    except ValidationError as exc:
        raise CatalogError([f"Структура файла: {error_text(exc)}"]) from exc

    problems = []
    seen = set()
    for spec in catalog.checks:
        key = (spec.dataset, spec.name)
        if key in seen:
            problems.append(f"{spec.dataset} / {spec.name}: имя проверки повторяется")
        seen.add(key)
        if spec.severity not in SEVERITIES:
            problems.append(f"{spec.name}: severity должна быть одной из {SEVERITIES}")
        try:
            create_check(spec.type, spec.params)
            validate_schedule(spec.schedule)
        except (ValueError, ValidationError) as exc:
            problems.append(f"{spec.name}: {error_text(exc)}")
    if problems:
        raise CatalogError(problems)
    return catalog


def load_catalog(meta_engine: Engine, path: Path) -> LoadReport:
    """Загружает проверки из файла в базу метаданных."""
    catalog = parse_catalog(path.read_text(encoding="utf-8"))
    report = LoadReport()

    with Session(meta_engine) as session, session.begin():
        connection = session.scalar(select(Connection).where(Connection.name == catalog.connection))
        if connection is None:
            raise CatalogError([f"Подключение {catalog.connection} не зарегистрировано"])

        datasets = {
            f"{d.schema_name}.{d.table_name}": d
            for d in session.scalars(select(Dataset).where(Dataset.connection_id == connection.id))
        }
        missing = sorted({s.dataset for s in catalog.checks} - datasets.keys())
        if missing:
            raise CatalogError([f"Датасет {name} не зарегистрирован" for name in missing])

        existing = {
            (c.dataset_id, c.name): c
            for c in session.scalars(
                select(DqCheck).where(DqCheck.dataset_id.in_([d.id for d in datasets.values()]))
            )
        }

        for spec in catalog.checks:
            dataset = datasets[spec.dataset]
            values = {
                "check_type": spec.type,
                "params": spec.params,
                "severity": spec.severity,
                "description": spec.description,
                "schedule": spec.schedule,
                "is_active": spec.active,
            }
            label = f"{spec.dataset} / {spec.name}"
            check = existing.get((dataset.id, spec.name))
            if check is None:
                session.add(DqCheck(dataset_id=dataset.id, name=spec.name, **values))
                report.added.append(label)
            elif any(getattr(check, k) != v for k, v in values.items()):
                for k, v in values.items():
                    setattr(check, k, v)
                report.updated.append(label)
            else:
                report.unchanged.append(label)

    return report
