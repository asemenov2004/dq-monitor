"""Начальная схема метаданных: 7 таблиц

Revision ID: 1ac1478a8e17
Revises:
Create Date: 2026-10-13 21:53:20.996161

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1ac1478a8e17"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Применить миграцию."""
    op.create_table(
        "connection",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("db_type", sa.String(length=20), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("database", sa.String(length=100), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("password_env", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "db_type IN ('postgres', 'clickhouse')", name=op.f("ck_connection_db_type")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connection")),
        sa.UniqueConstraint("name", name=op.f("uq_connection_name")),
    )
    op.create_table(
        "run",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("logical_now", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.String(length=20), server_default="manual", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("trigger IN ('manual', 'schedule')", name=op.f("ck_run_trigger")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run")),
    )
    op.create_table(
        "dataset",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("schema_name", sa.String(length=100), nullable=False),
        sa.Column("table_name", sa.String(length=100), nullable=False),
        sa.Column("batch_column", sa.String(length=100), nullable=True),
        sa.Column("loaded_at_column", sa.String(length=100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connection.id"],
            name=op.f("fk_dataset_connection_id_connection"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset")),
        sa.UniqueConstraint(
            "connection_id",
            "schema_name",
            "table_name",
            name=op.f("uq_dataset_connection_id_schema_name_table_name"),
        ),
    )
    op.create_table(
        "dq_check",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("check_type", sa.String(length=30), nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("severity", sa.String(length=20), server_default="critical", nullable=False),
        sa.Column("schedule", sa.String(length=100), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "check_type IN ('not_null', 'unique', 'freshness', 'row_count', 'range', 'referential', 'custom_sql')",
            name=op.f("ck_dq_check_check_type"),
        ),
        sa.CheckConstraint(
            "severity IN ('warning', 'critical')", name=op.f("ck_dq_check_severity")
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset.id"],
            name=op.f("fk_dq_check_dataset_id_dataset"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dq_check")),
        sa.UniqueConstraint("dataset_id", "name", name=op.f("uq_dq_check_dataset_id_name")),
    )
    op.create_table(
        "check_result",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("observed_value", sa.Numeric(), nullable=True),
        sa.Column("expected_low", sa.Numeric(), nullable=True),
        sa.Column("expected_high", sa.Numeric(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pass', 'fail', 'error')", name=op.f("ck_check_result_status")
        ),
        sa.ForeignKeyConstraint(
            ["check_id"],
            ["dq_check.id"],
            name=op.f("fk_check_result_check_id_dq_check"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["run.id"], name=op.f("fk_check_result_run_id_run"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_check_result")),
        sa.UniqueConstraint("run_id", "check_id", name=op.f("uq_check_result_run_id_check_id")),
    )
    op.create_index(op.f("ix_check_result_check_id"), "check_result", ["check_id"], unique=False)
    op.create_table(
        "incident",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column("opened_result_id", sa.BigInteger(), nullable=False),
        sa.Column("closed_result_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(length=10), server_default="open", nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(state = 'open' AND closed_at IS NULL AND closed_result_id IS NULL) OR (state = 'closed' AND closed_at IS NOT NULL AND closed_result_id IS NOT NULL)",
            name=op.f("ck_incident_state_consistency"),
        ),
        sa.CheckConstraint("state IN ('open', 'closed')", name=op.f("ck_incident_state")),
        sa.ForeignKeyConstraint(
            ["check_id"],
            ["dq_check.id"],
            name=op.f("fk_incident_check_id_dq_check"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["closed_result_id"],
            ["check_result.id"],
            name=op.f("fk_incident_closed_result_id_check_result"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["opened_result_id"],
            ["check_result.id"],
            name=op.f("fk_incident_opened_result_id_check_result"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incident")),
    )
    op.create_index(
        "uq_incident_open_per_check",
        "incident",
        ["check_id"],
        unique=True,
        postgresql_where=sa.text("state = 'open'"),
    )
    op.create_table(
        "notification",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("event", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("channel IN ('telegram')", name=op.f("ck_notification_channel")),
        sa.CheckConstraint("event IN ('opened', 'closed')", name=op.f("ck_notification_event")),
        sa.CheckConstraint("status IN ('sent', 'failed')", name=op.f("ck_notification_status")),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incident.id"],
            name=op.f("fk_notification_incident_id_incident"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification")),
    )


def downgrade() -> None:
    """Откатить миграцию."""
    op.drop_table("notification")
    op.drop_index(
        "uq_incident_open_per_check",
        table_name="incident",
        postgresql_where=sa.text("state = 'open'"),
    )
    op.drop_table("incident")
    op.drop_index(op.f("ix_check_result_check_id"), table_name="check_result")
    op.drop_table("check_result")
    op.drop_table("dq_check")
    op.drop_table("dataset")
    op.drop_table("run")
    op.drop_table("connection")
