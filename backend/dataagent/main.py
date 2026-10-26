"""DataAgent 后端启动入口；在后端根目录运行 python -m dataagent.main。"""

import sys

import uvicorn

from dataagent.app.app import app
from dataagent.engines.contract.config import cfg

__all__ = ["app"]

if __name__ == "__main__":
    uvicorn.run(
        "dataagent.app.app:app",
        host="0.0.0.0",
        port=cfg.port,
        loop=(
            "dataagent.engines.common.async_runtime:create_event_loop"
            if sys.platform == "win32"
            else "auto"
        ),
    )
