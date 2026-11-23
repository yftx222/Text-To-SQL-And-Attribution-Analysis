"""DataAgent 后端启动入口；在后端根目录运行 python -m dataagent.main。"""

import os
import sys

import uvicorn

from dataagent.app.app import app
from dataagent.engines.contract.config import cfg

__all__ = ["app"]

if __name__ == "__main__":
    application = "dataagent.app.app:app"
    if token := os.getenv("AIOPS_HTTP_METRICS_TOKEN"):
        try:
            from agent.adapters.http_metrics import HTTPMetrics
        except ModuleNotFoundError as exc:
            if exc.name not in {"agent", "agent.adapters", "agent.adapters.http_metrics"}:
                raise
        else:
            def postgres_pools():
                resources = app.state.resources
                return {
                    name: getattr(resources, name).engine.pool
                    for name in ("auth", "meta", "query", "assistant")
                }

            application = HTTPMetrics(app, token, pools=postgres_pools)
    uvicorn.run(
        application,
        host="0.0.0.0",
        port=cfg.port,
        loop=(
            "dataagent.engines.common.async_runtime:create_event_loop"
            if sys.platform == "win32"
            else "auto"
        ),
    )
