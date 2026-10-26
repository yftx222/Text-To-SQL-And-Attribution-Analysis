"""持久化 Agent 状态读取与当前活动状态投影。"""

from __future__ import annotations

from uuid import UUID

from langchain_core.runnables import RunnableConfig

from dataagent.engines.assistant.events import DelegationActivityHistory
from dataagent.engines.assistant.execution.activity import SessionActivity
from dataagent.engines.assistant.models.session import AgentSessionKey
from dataagent.engines.assistant.repositories.checkpoint import PostgresCheckpointStore
from dataagent.engines.assistant.repositories.checkpoint_reader import (
    CheckpointState,
    CheckpointStateReader,
)
from dataagent.engines.assistant.repositories.conversation_tombstone import (
    ConversationTombstoneStore,
)
from dataagent.engines.assistant.sessions.checkpoint_view import (
    SpecialistCheckpointView,
)
from dataagent.engines.assistant.sessions.identity import (
    build_planner_config,
    get_thread_id,
    session_checkpoint_namespace,
)
from dataagent.engines.contract.analysis import AgentType


class AgentStateReader:
    """从 Checkpoint 读取持久化上下文，并叠加进程内活动状态。"""

    def __init__(
        self,
        persistence: PostgresCheckpointStore,
        tombstones: ConversationTombstoneStore,
        activity: SessionActivity,
    ) -> None:
        """绑定检查点存储、会话删除标记和进程内活动注册表。"""
        self._persistence_manager = persistence
        self._tombstones = tombstones
        self._activity = activity

    async def can_resume_planner(self, user_id: int, conversation_id: UUID) -> bool:
        """读取 Planner 的调度通道，判断是否存在可恢复的任务。"""
        if await self._tombstones.exists(user_id, conversation_id):
            raise RuntimeError("该会话已被删除")
        reader = CheckpointStateReader(self._persistence_manager.checkpointer)
        return await reader.has_pending_tasks(
            build_planner_config(user_id, conversation_id)
        )

    async def read_planner_state(
        self,
        user_id: int,
        conversation_id: UUID,
    ) -> CheckpointState:
        """读取 Planner 根 namespace 中的持久化状态。"""
        if await self._tombstones.exists(user_id, conversation_id):
            raise RuntimeError("该会话已被删除")
        reader = CheckpointStateReader(self._persistence_manager.checkpointer)
        return await reader.read(build_planner_config(user_id, conversation_id))

    async def read_delegation_activity(
        self,
        user_id: int,
        conversation_id: UUID,
        analysis_id: str,
        agent_type: AgentType,
        session_id: str,
        delegation_id: str,
    ) -> DelegationActivityHistory | None:
        """直接从 Specialist Checkpoint 读取一次委派历史。"""
        session_key = AgentSessionKey(
            user_id=user_id,
            conversation_id=conversation_id,
            analysis_id=analysis_id,
            agent_type=agent_type,
            session_id=session_id,
        )
        reader = CheckpointStateReader(self._persistence_manager.checkpointer)
        state = await reader.read(
            RunnableConfig(
                configurable={
                    "thread_id": get_thread_id(user_id, conversation_id),
                    "checkpoint_ns": session_checkpoint_namespace(session_key),
                }
            )
        )
        active = self._activity.is_active(session_key)
        return SpecialistCheckpointView(state.values).delegation_activity(
            delegation_id,
            active=active,
        )
