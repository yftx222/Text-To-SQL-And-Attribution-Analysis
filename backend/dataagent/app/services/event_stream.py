"""内部聊天事件到 SSE 的适配、心跳及订阅清理。"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from uuid import UUID

from fastapi.responses import StreamingResponse
from loguru import logger

from dataagent.engines.assistant import contracts as chat_schema

_SSE_HEARTBEAT_SECONDS = 15


def _serialize_sse_event(event: chat_schema.ChatStreamEventPayload) -> str:
    """将聊天事件序列化为 SSE 数据帧。"""
    return f"data: {event.model_dump_json()}\n\n"


async def _stream_run_events(
    conversation_id: UUID,
    events: AsyncGenerator[chat_schema.ChatStreamEventPayload],
) -> AsyncIterator[str]:
    """把后台 Run 事件投影为 SSE；连接断开只取消当前订阅。"""
    next_message_task: asyncio.Future[chat_schema.ChatStreamEventPayload] | None = None
    try:
        next_message_task = asyncio.ensure_future(anext(events))
        while True:
            done, _ = await asyncio.wait(
                {next_message_task},
                timeout=_SSE_HEARTBEAT_SECONDS,
            )
            if not done:
                yield ": keep-alive\n\n"
                continue

            try:
                event = next_message_task.result()
            except StopAsyncIteration:
                break

            yield _serialize_sse_event(event)
            next_message_task = asyncio.ensure_future(anext(events))
    except asyncio.CancelledError:
        logger.info(f"SSE 订阅断开: conversation_id={conversation_id}")
        raise
    finally:
        if next_message_task is not None and not next_message_task.done():
            next_message_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await next_message_task
        await events.aclose()


def _sse_response(
    conversation_id: UUID,
    events: AsyncGenerator[chat_schema.ChatStreamEventPayload],
) -> StreamingResponse:
    """为启动、恢复和重新订阅统一配置 SSE 心跳、清理及响应头。"""
    return StreamingResponse(
        _stream_run_events(conversation_id, events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
