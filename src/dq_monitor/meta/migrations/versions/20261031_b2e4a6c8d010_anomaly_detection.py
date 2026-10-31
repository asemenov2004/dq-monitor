"""Аномалии: восьмой тип проверки, skip, версия конфигурации и диагностика.

Revision ID: b2e4a6c8d010
Revises: 1ac1478a8e17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b2e4a6c8d010"
down_revision = "1ac1478a8e17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Добавить поддержку детектора, сохранив старые результаты."""
    op.drop_constraint(op.f("ck_dq_check_check_type"), "dq_check", type_="check")
    op.create_check_constraint(
        op.f("ck_dq_check_check_type"),
        "dq_check",
        "check_type IN ('not_null', 'unique', 'freshness', 'row_count', 'range', 'referential', 'custom_sql', 'anomaly')",
    )
    op.drop_constraint(op.f("ck_check_result_status"), "check_result", type_="check")
    op.create_check_constraint(
        op.f("ck_check_result_status"),
        "check_result",
        "status IN ('pass', 'fail', 'error', 'skip')",
    )
    op.add_column("check_result", sa.Column("config_hash", sa.String(64), nullable=True))
    op.add_column(
        "check_result",
        sa.Column(
            "details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )


def downgrade() -> None:
    """Откат только после явного удаления данных новых типов, без молчаливой потери истории."""
    # Offline-режим генерирует SQL; проверка остаётся частью SQL и при таком применении.
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM dq_check WHERE check_type = 'anomaly')
         OR EXISTS (SELECT 1 FROM check_result WHERE status = 'skip') THEN
        RAISE EXCEPTION 'Нельзя откатить: сохранены проверки anomaly или результаты skip';
      END IF;
    END $$;
    """)
    op.drop_column("check_result", "details")
    op.drop_column("check_result", "config_hash")
    op.drop_constraint(op.f("ck_check_result_status"), "check_result", type_="check")
    op.create_check_constraint(
        op.f("ck_check_result_status"), "check_result", "status IN ('pass', 'fail', 'error')"
    )
    op.drop_constraint(op.f("ck_dq_check_check_type"), "dq_check", type_="check")
    op.create_check_constraint(
        op.f("ck_dq_check_check_type"),
        "dq_check",
        "check_type IN ('not_null', 'unique', 'freshness', 'row_count', 'range', 'referential', 'custom_sql')",
    )
