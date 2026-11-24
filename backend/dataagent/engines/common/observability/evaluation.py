"""Observation follows the request into its background Planner task."""

import json
from contextvars import ContextVar

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables.config import var_child_runnable_config


class Observer(BaseCallbackHandler):
    run_inline = True

    def __init__(self, *sinks):
        self.sinks = sinks

    def write(self, kind, **fields):
        for sink in self.sinks:
            try:
                sink.write(kind, **fields)
            except Exception:  # noqa: BLE001 - optional telemetry must not interrupt a turn
                if hasattr(sink, "partial"):
                    sink.partial = True

    def on_chat_model_start(
        self,
        serialized,
        messages,
        *,
        run_id,
        parent_run_id=None,
        metadata=None,
        **kwargs,
    ):
        meta = metadata or {}
        self.write(
            "model_start",
            run=run_id,
            parent=parent_run_id,
            model=meta.get("ls_model_name"),
            role=meta.get("agent_type", "planner"),
            session=meta.get("session_id"),
            delegation=meta.get("delegation_id"),
        )

    def on_chain_start(
        self,
        serialized,
        inputs,
        *,
        run_id,
        parent_run_id=None,
        name=None,
        metadata=None,
        **kwargs,
    ):
        meta = metadata or {}
        self.write(
            "chain",
            run=run_id,
            parent=parent_run_id,
            name=name,
            step=meta.get("langgraph_step"),
            node=meta.get("langgraph_node"),
        )

    def on_llm_end(self, response, *, run_id, **kwargs):
        message = response.generations[0][0].message
        usage = getattr(message, "usage_metadata", None)
        self.write("model_end", run=run_id, usage=usage)

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self.write("chain_end", run=run_id)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self.write("chain_error", run=run_id)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.write("model_error", run=run_id, error=type(error).__name__)

    def on_tool_start(
        self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs
    ):
        self.write(
            "tool_start", name=serialized.get("name"), run=run_id, parent=parent_run_id
        )

    def on_tool_end(self, output, *, run_id, **kwargs):
        delivery = None
        if getattr(output, "name", None) == "publish_artifact":
            try:
                delivery = json.loads(output.content).get("status")
            except (ValueError, TypeError, AttributeError):
                pass
        self.write(
            "tool_end",
            run=run_id,
            tool_call_id=getattr(output, "tool_call_id", None),
            delivery=delivery,
        )

    def on_tool_error(self, error, *, run_id, **kwargs):
        self.write("tool_error", run=run_id, error=type(error).__name__)


observer: ContextVar[Observer | None] = ContextVar("evaluation_observer", default=None)


def emit(kind, **fields):
    active = observer.get()
    if active is not None:
        cfg = var_child_runnable_config.get() or {}
        parent = getattr(cfg.get("callbacks"), "parent_run_id", None)
        active.write(kind, parent=parent, **fields)
