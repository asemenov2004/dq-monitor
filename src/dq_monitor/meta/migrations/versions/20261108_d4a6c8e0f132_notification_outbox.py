"""Durable outbox email и Mattermost, с сохранением истории Telegram.

Revision ID: d4a6c8e0f132
Revises: c3f5b7d9e021
"""

import sqlalchemy as sa
from alembic import op

revision = "d4a6c8e0f132"
down_revision = "c3f5b7d9e021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Старые sent/failed остаются историей, без повторной отправки Telegram."""
    op.drop_constraint(op.f("ck_notification_channel"), "notification", type_="check")
    op.drop_constraint(op.f("ck_notification_status"), "notification", type_="check")
    op.create_check_constraint(op.f("ck_notification_channel"), "notification", "channel IN ('telegram', 'email', 'mattermost')")
    op.create_check_constraint(op.f("ck_notification_status"), "notification", "status IN ('pending', 'sent', 'failed')")
    op.alter_column("notification", "status", server_default="pending")
    op.alter_column("notification", "sent_at", nullable=True, server_default=None)
    op.add_column("notification", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("notification", sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.add_column("notification", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()))
    op.add_column("notification", sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("notification", sa.Column("message", sa.Text(), nullable=False, server_default=""))
    op.execute("UPDATE notification SET created_at = COALESCE(sent_at, now()), last_attempt_at = sent_at, attempts = 1, next_attempt_at = NULL")
    op.execute("UPDATE notification SET sent_at = NULL WHERE status = 'failed'")
    op.create_check_constraint(op.f("ck_notification_attempts"), "notification", "attempts >= 0")
    op.create_index("uq_notification_outbox_event", "notification", ["incident_id", "channel", "event"], unique=True, postgresql_where=sa.text("channel IN ('email', 'mattermost')"))
    op.create_index(op.f("ix_notification_status_next_attempt_at"), "notification", ["status", "next_attempt_at"])


def downgrade() -> None:
    """Удаляет новый outbox, сохраняя совместимую историю Telegram."""
    op.execute("DELETE FROM notification WHERE channel <> 'telegram' OR status = 'pending'")
    op.execute("UPDATE notification SET sent_at = COALESCE(sent_at, last_attempt_at, created_at)")
    op.drop_index(op.f("ix_notification_status_next_attempt_at"), table_name="notification")
    op.execute("ALTER TABLE notification DROP CONSTRAINT IF EXISTS uq_notification_incident_id_channel_event")
    op.execute("DROP INDEX IF EXISTS uq_notification_outbox_event")
    op.drop_constraint(op.f("ck_notification_attempts"), "notification", type_="check")
    for name in ("message", "last_attempt_at", "next_attempt_at", "created_at", "attempts"):
        op.drop_column("notification", name)
    op.alter_column("notification", "sent_at", nullable=False, server_default=sa.func.now())
    op.alter_column("notification", "status", server_default=None)
    op.drop_constraint(op.f("ck_notification_status"), "notification", type_="check")
    op.drop_constraint(op.f("ck_notification_channel"), "notification", type_="check")
    op.create_check_constraint(op.f("ck_notification_status"), "notification", "status IN ('sent', 'failed')")
    op.create_check_constraint(op.f("ck_notification_channel"), "notification", "channel IN ('telegram')")
