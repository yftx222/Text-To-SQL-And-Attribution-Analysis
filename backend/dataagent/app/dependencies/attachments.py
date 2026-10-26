"""附件业务服务依赖。"""

from typing import Annotated

from fastapi import Depends

from dataagent.app.dependencies.assistant import (
    ConversationLifecycleServiceDep,
    ConversationPGRepoDep,
    SandboxManagerDep,
)
from dataagent.app.services.attachments import AttachmentService


def _get_attachment_service(
    repository: ConversationPGRepoDep,
    lifecycle: ConversationLifecycleServiceDep,
    sandbox: SandboxManagerDep,
) -> AttachmentService:
    """绑定当前请求的附件服务资源。"""
    return AttachmentService(repository, lifecycle, sandbox)


AttachmentServiceDep = Annotated[AttachmentService, Depends(_get_attachment_service)]
