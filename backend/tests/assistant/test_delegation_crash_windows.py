"""隔离子进程退出后，用同一委派标识恢复并核对实际产物次数。"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest


async def _worker(folder: Path, stop: str) -> None:
    # 按应用入口初始化模块，再装配测试用模型与检查点适配器。
    import dataagent.app.app  # noqa: F401
    from tests.assistant.test_dynamic_subagents import (
        _CONVERSATION_ID,
        _FakeAgent,
        _request,
        _service,
        build_planner_config,
    )

    namespace = "subagents/sales-decline/analyst/region"
    checkpoint = folder / "checkpoint.json"

    class CrashProbe(_FakeAgent):
        async def ainvoke(self, input, config):
            checkpoint.write_text(
                json.dumps({"delegation_records": input["delegation_records"]})
            )
            if stop == "before_tool":
                os._exit(73)
            (folder / f"{uuid4()}.csv").write_bytes(b"value\n1\n")
            if stop == "after_artifact":
                os._exit(73)
            return await super().ainvoke(input, config)

        async def aupdate_state(self, config, values):
            await super().aupdate_state(config, values)
            checkpoint.write_text(
                json.dumps({"delegation_records": values["delegation_records"]})
            )
            if stop == "after_terminal":
                os._exit(73)

    agent = CrashProbe()
    if checkpoint.exists():
        agent.state_values[namespace] = json.loads(checkpoint.read_text())
    result = await _service(agent).executor.execute_delegation(
        _request("region"),
        build_planner_config(12, _CONVERSATION_ID),
        delegation_id="crash-window",
    )
    assert result.status == "completed", result


@pytest.mark.parametrize(
    "window,expected_attempts",
    [
        ("before_tool", 1),
        ("after_artifact", 2),
        ("after_terminal", 1),
    ],
)
def test_crash_then_resume(tmp_path, window, expected_attempts):
    command = [
        sys.executable,
        "-m",
        "tests.assistant.test_delegation_crash_windows",
        str(tmp_path),
    ]
    assert subprocess.run([*command, window], check=False, timeout=120).returncode == 73
    subprocess.run([*command, "resume"], check=True, timeout=120)
    assert len(list(tmp_path.glob("*.csv"))) == expected_attempts


if __name__ == "__main__":
    asyncio.run(_worker(Path(sys.argv[1]), sys.argv[2]))
