"""会话正式交付回执。"""

from uuid import UUID

from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from dataagent.engines.assistant.models.base import AssistantBase


class DeliveryReceipt(AssistantBase):
    __tablename__ = "delivery_receipts"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    receipt: Mapped[dict] = mapped_column(JSON)
