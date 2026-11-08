"""Сохранение обработанных интервалов расписания.

Revision ID: c3f5b7d9e021
Revises: b2e4a6c8d010
"""

import sqlalchemy as sa
from alembic import op

revision = "c3f5b7d9e021"
down_revision = "b2e4a6c8d010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Ручные результаты остаются с NULL, cron получает уникальный интервал."""
    op.add_column(
        "check_result", sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_unique_constraint(
        op.f("uq_check_result_check_id_scheduled_for"),
        "check_result",
        ["check_id", "scheduled_for"],
    )


def downgrade() -> None:
    """Результаты сохраняются; служебная история обработанных интервалов удаляется."""
    op.drop_constraint(
        op.f("uq_check_result_check_id_scheduled_for"), "check_result", type_="unique"
    )
    op.drop_column("check_result", "scheduled_for")
