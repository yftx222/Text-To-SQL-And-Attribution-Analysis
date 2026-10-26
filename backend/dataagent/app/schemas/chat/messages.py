"""聊天及附件接口专用请求、响应。"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from dataagent.engines.assistant.contracts.messages import (
    Attachment,
    UserMessageRequest,
)


class ChatStreamRequest(BaseModel):
    """SSE 聊天请求。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID = Field(..., description="对话ID")
    message: UserMessageRequest = Field(..., description="用户消息")


class DeleteAttachmentRequest(BaseModel):
    """删除附件请求。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID = Field(..., description="对话ID")
    f_path: str = Field(..., min_length=1, description="工作区内的文件路径")


class ConversationRunStatusResponse(BaseModel):
    """Conversation 后台 Planner Run 状态。"""

    running: bool


class UploadAttachmentResponse(BaseModel):
    """上传附件响应。"""

    attachment: Attachment = Field(..., description="上传后的附件信息")
