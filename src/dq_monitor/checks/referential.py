"""referential: у каждой ссылки есть запись в родительской таблице (нет «висячих» ключей)."""

from collections.abc import Sequence
from typing import Any

from pydantic import Field

from dq_monitor.checks.base import FAIL, PASS, Check, CheckContext, CheckParams, Outcome, Query


class ReferentialParams(CheckParams):
    column: str
    # Родительская таблица в виде schema.table
    ref_table: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")
    ref_column: str
    max_orphans: int = Field(0, ge=0)


class ReferentialCheck(Check):
    type_name = "referential"
    Params = ReferentialParams
    params: ReferentialParams

    def build_query(self, ctx: CheckContext) -> Query:
        q = ctx.connector.quote
        ref_schema, ref_table = self.params.ref_table.split(".")
        child = f"c.{q(self.params.column)}"
        parent = f"p.{q(self.params.ref_column)}"
        condition, params = self.batch_filter(ctx, alias="c")
        # Строка-потомок без пары в родителе после LEFT JOIN получает NULL в столбце родителя.
        # Пустая ссылка (NULL) висячей не считается: это дело проверки not_null
        sql = (
            f"SELECT COUNT(*) FROM {ctx.table_sql} AS c "
            f"LEFT JOIN {ctx.connector.table(ref_schema, ref_table)} AS p ON {child} = {parent}"
            f"{self.where(f'{child} IS NOT NULL', f'{parent} IS NULL', condition)}"
        )
        return Query(sql, params)

    def evaluate(self, row: Sequence[Any], ctx: CheckContext) -> Outcome:
        orphans = int(row[0])
        limit = self.params.max_orphans
        target = f"{self.params.ref_table}.{self.params.ref_column}"
        return Outcome(
            FAIL if orphans > limit else PASS,
            orphans,
            0,
            limit,
            f"Ссылок {self.params.column} без пары в {target}: {orphans}, допустимо {limit}",
        )
