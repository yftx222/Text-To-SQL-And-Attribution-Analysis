"""固定版本的正式交付回执，重试同一版本复用已提交的记录。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from dataagent.engines.assistant.models.delivery import DeliveryReceipt
from dataagent.engines.infra.postgres_client_manager import PostgresClientManager


class DeliveryReceiptStore:
    def __init__(self, database: PostgresClientManager):
        self.database = database

    async def save(self, ident: UUID, conversation_id: UUID, receipt: dict) -> dict:
        async with self.database.session() as session, session.begin():
            await session.execute(
                insert(DeliveryReceipt)
                .values(id=ident, conversation_id=conversation_id, receipt=receipt)
                .on_conflict_do_nothing(index_elements=[DeliveryReceipt.id])
            )
            return (
                await session.execute(
                    select(DeliveryReceipt.receipt).where(DeliveryReceipt.id == ident)
                )
            ).scalar_one()
