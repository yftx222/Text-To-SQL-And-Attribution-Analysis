"""Run the regular chat API on loopback with local evaluation telemetry."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

import uvicorn
from langchain_core.callbacks import BaseCallbackHandler

from dataagent.engines.common.async_runtime import run_async
from dataagent.engines.common.observability import context, evaluation
from dataagent.engines.contract.config import cfg


class TraceWriter(BaseCallbackHandler):
    run_inline = True

    def __init__(self, target):
        self.target = target
        self.lock = Lock()

    def write(self, kind, **fields):
        trace = context.trace_id_ctx.get()
        if not trace:
            return
        row = {
            "at": datetime.now(UTC).isoformat(),
            "trace": trace,
            "event": kind,
            **fields,
        }
        with self.lock, self.target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

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
        self, serialized, inputs, *, run_id, parent_run_id=None, name=None, **kwargs
    ):
        self.write("chain", run=run_id, parent=parent_run_id, name=name)

    def on_llm_end(self, response, *, run_id, **kwargs):
        message = response.generations[0][0].message
        usage = getattr(message, "usage_metadata", None)
        self.write("model_end", run=run_id, usage=usage)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.write("model_error", run=run_id, error=type(error).__name__)

    def on_tool_start(
        self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs
    ):
        self.write(
            "tool_start", name=serialized.get("name"), run=run_id, parent=parent_run_id
        )

    def on_tool_end(self, output, *, run_id, **kwargs):
        self.write(
            "tool_end", run=run_id, tool_call_id=getattr(output, "tool_call_id", None)
        )

    def on_tool_error(self, error, *, run_id, **kwargs):
        self.write("tool_error", run=run_id, error=type(error).__name__)


async def serve(folder, port):
    from dataagent.app.app import app

    cfg.query.learn_from_queries = False
    folder.mkdir(parents=True, exist_ok=True)
    writer = TraceWriter(folder / "telemetry.jsonl")
    evaluation.observe = writer.write
    evaluation.callback = writer

    @app.get("/_eval/status", include_in_schema=False)
    def status():
        return {
            "experience_writes": cfg.query.learn_from_queries,
            "folder": str(folder.resolve()),
        }

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False)
    )
    task = asyncio.create_task(server.serve())
    while not task.done():
        if (folder / "stop-server").exists():
            server.should_exit = True
            break
        await asyncio.sleep(1)
    await task


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--port", type=int, default=7014)
    args = parser.parse_args()
    run_async(serve(args.folder, args.port))
