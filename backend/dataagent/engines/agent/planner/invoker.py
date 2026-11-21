"""Planner 回合执行、续写与恢复，及执行事件到聊天协议的转换。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import BaseMessage
from langgraph.types import StreamPart
from loguru import logger

from dataagent.engines.agent.common.tools.view_image import is_supported_image_path
from dataagent.engines.assistant import contracts as chat_schema
from dataagent.engines.assistant.contracts import PlannerTurnContext
from dataagent.engines.assistant.errors import PlannerContinuationLimitError
from dataagent.engines.assistant.events import (
    SubagentMessageActivity,
    SubagentMessageDeltaActivity,
    SubagentStatusActivity,
    SubagentThinkingDeltaActivity,
)
from dataagent.engines.assistant.messages.projection import (
    langchain_message_to_schema,
    schema_to_human_message,
    subagent_activity_to_event,
)
from dataagent.engines.assistant.messages.stream import MessageDeltaParser
from dataagent.engines.assistant.sessions.identity import build_planner_config
from dataagent.engines.common.observability import evaluation

if TYPE_CHECKING:
    from dataagent.engines.assistant.execution.runtime_cache import AgentManager
    from dataagent.engines.sandbox import DockerSandboxManager


async def run_agent_turn(
    #AgentManager 的实例，负责创建、缓存和取得会话对应的 Agent。
    agents: AgentManager,
    #管理 Docker 沙箱中的文件与产物
    files: DockerSandboxManager,
    turn_context: PlannerTurnContext,
    user_message: chat_schema.UserMessageRequest | None,
) -> AsyncGenerator[chat_schema.ChatStreamEventPayload]:
    """执行新回合或从待执行 Checkpoint 恢复同一回合。"""
    user_id, conversation_id = turn_context.user_id, turn_context.conversation_id
    if user_message is not None and user_message.attachments:
        attachments = []
        for item in user_message.attachments:
            if is_supported_image_path(item.f_path):
                path = await files.freeze_attachment(user_id, conversation_id, item.f_path)
                item = item.model_copy(update={"f_path": path})
            attachments.append(item)
        user_message = user_message.model_copy(update={"attachments": attachments})
    input_messages: list[BaseMessage] | None = (
        [schema_to_human_message(user_message, conversation_id)]
        if user_message is not None
        else None
    )
    logger.info(
        f"智能体回合开始: conversation_id={conversation_id}, "
        f"resume={user_message is None}, "
        f"parts={len(user_message.parts) if user_message is not None else 0}, "
        f"attachments={len(user_message.attachments or ()) if user_message is not None else 0}"
    )
    #获取当前会话对应的 Agent 运行时，并在使用期间管理它的生命周期
    #运行时（Runtime）：指程序运行期间所使用的环境或相关运行资源。
    #use_runtime负责获取、保护和释放会话级 Agent 运行时的使用权
    async with agents.use_runtime(user_id, conversation_id) as runtime:
        continuation_count = 0
        deltas = MessageDeltaParser()
        while True:
            last_finish_reason: str | None = None
            config = build_planner_config(user_id, conversation_id)
            if evaluation.callback is not None:
                config["callbacks"] = [evaluation.callback]

            # LangGraph 的重载声明返回 AsyncIterator，实际 astream 是异步生成器。
            async with aclosing(
                cast(
                    AsyncGenerator[StreamPart[Any, Any]],
                    #真正执行planner的地方，返回一个异步生成器，逐步产出执行事件
                    runtime.planner.astream(
                        input={"messages": input_messages}
                        if input_messages is not None
                        else None,
                        config=config,
                        stream_mode=["updates", "custom", "messages"],
                        version="v2",
                    ),
                )
            ) as stream:
                async for chunk in stream:
                    if chunk.get("type") == "custom":
                        activity = chunk.get("data")
                        if isinstance(
                            activity,
                            (
                                SubagentMessageActivity,
                                SubagentMessageDeltaActivity,
                                SubagentThinkingDeltaActivity,
                                SubagentStatusActivity,
                            ),
                        ):
                            event = await subagent_activity_to_event(
                                activity,
                                user_id,
                                conversation_id,
                                files=files,
                            )
                            if event is not None:
                                yield event
                        continue
                    if chunk.get("type") == "messages":
                        for kind, delta in deltas.parse(chunk.get("data")):
                            if kind == "thinking":
                                yield chat_schema.ChatStreamThinkingEvent(
                                    type="thinking", **delta
                                )
                            else:
                                yield chat_schema.ChatStreamMessageDeltaEvent(
                                    type="message_delta", **delta
                                )
                        continue
                    if chunk.get("type") != "updates":
                        continue
                    data = chunk.get("data")
                    if not isinstance(data, dict):
                        continue

                    responses: list[chat_schema.MessageResponse] = []
                    for node in ("model", "tools"):
                        update = data.get(node)
                        messages = (
                            update.get("messages") if isinstance(update, dict) else None
                        )
                        if not isinstance(messages, list):
                            continue
                        for message in messages:
                            response = await langchain_message_to_schema(
                                message,
                                files,
                                user_id,
                                conversation_id,
                                planner_reply=True,
                            )
                            if response is not None:
                                responses.append(response)
                    logger.debug(
                        f"智能体流式更新: conversation_id={conversation_id}, "
                        f"nodes={tuple(chunk)}, "
                        f"messages={len(responses)}"
                    )
                    for response in responses:
                        last_finish_reason = response.finish_reason
                        yield chat_schema.ChatStreamMessageEvent(
                            type="message",
                            message=response,
                        )

            if last_finish_reason is None or last_finish_reason == "stop":
                break
            if continuation_count >= turn_context.max_continuations:
                raise PlannerContinuationLimitError(
                    turn_context.max_continuations,
                    last_finish_reason,
                )

            continuation_count += 1
            # 空增量会保留 Checkpointer 中的已有状态并继续生成。
            input_messages = []

    logger.info(f"智能体回合结束: conversation_id={conversation_id}")
