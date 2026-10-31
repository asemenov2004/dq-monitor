"""Разбор файла проверок: структура, параметры, сбор всех ошибок сразу."""

from pathlib import Path

import pytest

from dq_monitor.catalog import CatalogError, parse_catalog

VALID = """
connection: tpch_warehouse
checks:
  - name: a
    dataset: dwh.orders
    type: not_null
    params: {column: o_custkey}
"""


def problems(text: str) -> list[str]:
    with pytest.raises(CatalogError) as info:
        parse_catalog(text)
    return info.value.problems


def test_valid_file():
    catalog = parse_catalog(VALID)
    assert catalog.connection == "tpch_warehouse"
    assert catalog.checks[0].severity == "critical"
    assert catalog.checks[0].active is True


def test_repository_tpch_file_is_valid():
    path = Path(__file__).parent.parent / "checks" / "tpch.yaml"
    catalog = parse_catalog(path.read_text(encoding="utf-8"))
    assert len(catalog.checks) == 22
    assert {c.type for c in catalog.checks} == {
        "not_null",
        "unique",
        "freshness",
        "row_count",
        "range",
        "referential",
        "custom_sql",
    }


def test_broken_yaml():
    assert "корректным YAML" in problems("checks: [unclosed")[0]


def test_unknown_top_level_key():
    assert "Структура файла" in problems(VALID + "extra: 1\n")[0]


def test_all_problems_reported_at_once():
    text = """
connection: c
checks:
  - {name: a, dataset: dwh.t, type: not_null, params: {}}
  - {name: b, dataset: dwh.t, type: nope}
  - {name: c, dataset: dwh.t, type: row_count, params: {min: 5, max: 1}}
  - {name: c, dataset: dwh.t, type: row_count, params: {min: 1}, severity: low}
"""
    found = problems(text)
    assert len(found) == 5
    joined = "\n".join(found)
    assert "a: Неверные параметры: column" in joined
    assert "b: Неизвестный тип проверки" in joined
    assert "min больше max" in joined
    assert "имя проверки повторяется" in joined
    assert "severity" in joined


def test_dataset_must_be_schema_dot_table():
    text = VALID.replace("dwh.orders", "orders")
    assert "Структура файла" in problems(text)[0]
