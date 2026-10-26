"""聊天与专业 Agent 历史消息读取。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from langchain_core.messages import BaseMessage

from dataagent.engines.assistant import contracts as chat_schema
from dataagent.engines.assistant.errors import SubagentRunNotFoundError
from dataagent.engines.assistant.messages.projection import langchain_message_to_schema
from dataagent.engines.contract.analysis import AgentType

if TYPE_CHECKING:
    from dataagent.engines.assistant.sessions.state_reader import AgentStateReader
    from dataagent.engines.sandbox import DockerSandboxManager


async def list_messages(
    state_reader: AgentStateReader,
    files: DockerSandboxManager,
    user_id: int,
    conversation_id: UUID,
) -> list[chat_schema.MessageResponse]:
    """从 LangGraph 最新线程状态读取消息。"""
    state = await state_reader.read_planner_state(user_id, conversation_id)
    messages = state.values.get("messages", [])
    if not isinstance(messages, list):
        return []

    result: list[chat_schema.MessageResponse] = []
    for message in messages:
        if not isinstance(message, BaseMessage):
            continue
        if schema := await langchain_message_to_schema(
            message,
            files,
            user_id,
            conversation_id,
        ):
            result.append(schema)
    return result


async def get_subagent_activity(
    state_reader: AgentStateReader,
    user_id: int,
    conversation_id: UUID,
    analysis_id: str,
    agent_type: AgentType,
    session_id: str,
    delegation_id: str,
    *,
    files: DockerSandboxManager,
) -> chat_schema.SubagentMessageListResponse:
    """读取一次 Specialist delegation 的公开工作消息和状态。"""
    activity = await state_reader.read_delegation_activity(
        user_id,
        conversation_id,
        analysis_id,
        agent_type,
        session_id,
        delegation_id,
    )
    if activity is None:
        raise SubagentRunNotFoundError
    return chat_schema.SubagentMessageListResponse(
        status=activity.status,
        messages=[
            schema
            for message in activity.messages
            if (
                schema := await langchain_message_to_schema(
                    message, files, user_id, conversation_id
                )
            )
            is not None
        ],
    )
