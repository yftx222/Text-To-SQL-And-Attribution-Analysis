"""每个分析的跨进程预算。"""

from uuid import UUID

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from dataagent.engines.assistant.models.base import AssistantBase


class AnalysisBudget(AssistantBase):
    __tablename__ = "analysis_budgets"
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    analysis_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ledger: Mapped[dict] = mapped_column(JSON)
