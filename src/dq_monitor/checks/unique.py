"""unique: значения ключа (одного или нескольких столбцов) не повторяются."""

from collections.abc import Sequence
from typing import Any

from pydantic import Field

from dq_monitor.checks.base import FAIL, PASS, Check, CheckContext, CheckParams, Outcome, Query


class UniqueParams(CheckParams):
    columns: list[str] = Field(min_length=1)
    max_duplicates: int = Field(0, ge=0)


class UniqueCheck(Check):
    type_name = "unique"
    Params = UniqueParams
    params: UniqueParams

    def build_query(self, ctx: CheckContext) -> Query:
        key = ", ".join(ctx.connector.quote(c) for c in self.params.columns)
        condition, params = self.batch_filter(ctx)
        # Группы с одинаковым ключом; в каждой группе лишних строк на одну меньше, чем в группе.
        # GROUP BY есть во всех СУБД, в отличие от COUNT(DISTINCT по нескольким столбцам)
        sql = (
            "SELECT COALESCE(SUM(n - 1), 0) FROM ("
            f"SELECT COUNT(*) AS n FROM {ctx.table_sql}{self.where(condition)} "
            f"GROUP BY {key} HAVING COUNT(*) > 1"
            ") AS duplicates"
        )
        return Query(sql, params)

    def evaluate(self, row: Sequence[Any], ctx: CheckContext) -> Outcome:
        duplicates = int(row[0])
        limit = self.params.max_duplicates
        key = ", ".join(self.params.columns)
        return Outcome(
            FAIL if duplicates > limit else PASS,
            duplicates,
            0,
            limit,
            f"Повторяющихся строк по ключу ({key}): {duplicates}, допустимо {limit}",
        )
