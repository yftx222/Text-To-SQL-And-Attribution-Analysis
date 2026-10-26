"""RFC 9457 错误响应模型。"""

from pydantic import BaseModel, ConfigDict


class ProblemDetails(BaseModel):
    """RFC 9457 错误响应协议。"""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
