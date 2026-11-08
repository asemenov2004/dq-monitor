"""Уникальность только для outbox, без ограничения старой истории Telegram.

Revision ID: e5b7d9f1a243
Revises: d4a6c8e0f132

Новая установка уже получает partial index в исправленной d4a6. Эта миграция
также исправляет базы, где исходная d4a6 была применена с полным UNIQUE.
"""

from alembic import op

revision = "e5b7d9f1a243"
down_revision = "d4a6c8e0f132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Обе исходные схемы приводятся к одному partial index без удаления строк."""
    op.execute("ALTER TABLE notification DROP CONSTRAINT IF EXISTS uq_notification_incident_id_channel_event")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_outbox_event ON notification (incident_id, channel, event) WHERE channel IN ('email', 'mattermost')")


def downgrade() -> None:
    """Исправленная d4a6 уже задаёт ту же схему; полный UNIQUE не восстанавливаем."""
    pass
