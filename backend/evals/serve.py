"""Run the regular chat API on loopback with local evaluation telemetry."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

import uvicorn

from dataagent.engines.common.async_runtime import run_async
from dataagent.engines.common.observability import context, evaluation
from dataagent.engines.contract.config import cfg


class JsonlTraceSink:
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


async def serve(folder, port):
    from dataagent.app.app import app

    cfg.query.learn_from_queries = False
    folder.mkdir(parents=True, exist_ok=True)
    writer = JsonlTraceSink(folder / "telemetry.jsonl")

    @app.middleware("http")
    async def local_observation(request, call_next):
        token = evaluation.observer.set(evaluation.Observer(writer))
        try:
            return await call_next(request)
        finally:
            evaluation.observer.reset(token)

    fixture = os.getenv("DATAAGENT_FIXTURE_VERSION")
    if fixture:
        from evals.profile import EvaluationProfile

        profile = EvaluationProfile(
            os.environ["DATAAGENT_PROFILE_USER"],
            os.environ["DATAAGENT_PROFILE_ROLE"],
            os.environ["DATAAGENT_FIXTURE_DATABASE"],
            fixture,
        )
        app.add_api_route(
            "/_eval/environment", profile.read, methods=["GET"], include_in_schema=False
        )

    target = os.getenv("DATAAGENT_TRACE_PLATFORM_URL")
    if target:
        from evals.platform_trace import EvaluationPlatformTraceSink

        sink = EvaluationPlatformTraceSink(target, os.environ["DATAAGENT_TRACE_USER"])
        app.state.eval_traces = sink
        app.add_api_route(
            "/_eval/trace/finish",
            sink.finish,
            methods=["POST"],
            include_in_schema=False,
        )

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
