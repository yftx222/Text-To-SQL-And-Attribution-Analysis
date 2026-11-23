"""Expose the current delegation's closing window without extending its timeout."""

from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

from langchain_core.messages import SystemMessage

_closing_at = ContextVar("specialist_closing_at", default=None)


@contextmanager
def delivery_clock(seconds):
    # Keep the last quarter (up to a minute) for the required evidence and handoff.
    token = _closing_at.set(monotonic() + seconds - min(60, seconds / 4))
    try:
        yield
    finally:
        _closing_at.reset(token)


def closing_request(request, role):
    closing = _closing_at.get()
    if role not in {"analyst", "reviewer"} or closing is None or monotonic() < closing:
        return request
    hint = (
        "本次委派已进入交付时间。优先复用本轮固定输入，停止非必需的排版和重复绘图。"
        "分析者完成必要内容、主数字核对、文件固定和回执。复核者核对当前版本和证据后立即提交结论。"
        "必要证据缺失时报告缺项，不得跳过复核或宣称交付成功。"
    )
    system = request.system_message
    if system is None:
        return request.override(system_message=SystemMessage(content=hint))
    original = system.content
    content = (
        original + "\n" + hint
        if isinstance(original, str)
        else [*original, {"type": "text", "text": hint}]
    )
    return request.override(
        system_message=system.model_copy(update={"content": content})
    )
