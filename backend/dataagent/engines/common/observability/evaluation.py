"""Optional process-local observer for isolated evaluation servers."""
from collections.abc import Callable
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

callback: BaseCallbackHandler | None = None
observe: Callable[..., None] | None = None


def emit(kind: str, **fields: Any) -> None:
    if observe is not None:
        observe(kind, **fields)
