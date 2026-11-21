"""从本轮实际工具回执登记产物和专家业务结论。"""

import json
from collections.abc import Sequence

from langchain.tools import tool
from langchain_core.messages import BaseMessage, ToolMessage

from dataagent.engines.assistant.contracts.sessions import TaskOutcome


@tool(args_schema=TaskOutcome)
def submit_task_result(
    verdict: str,
    checks: dict[str, str],
    evidence: list[str] | None = None,
    repair_target: str = "",
    metric: str = "",
    data_scope: str = "",
    replaces_path: str | None = None,
) -> dict:
    """提交本轮业务判定、逐项检查结果、固定证据路径，以及需要修补的具体问题。"""
    if verdict != "passed" and not repair_target.strip():
        return {"error": "请说明阻断原因和修补目标"}
    return TaskOutcome(
        verdict=verdict,
        checks=checks,
        evidence=evidence or [],
        repair_target=repair_target,
        metric=metric,
        data_scope=data_scope,
        replaces_path=replaces_path,
    ).model_dump()


def task_outputs(
    messages: Sequence[BaseMessage],
) -> tuple[TaskOutcome | None, list[str], list[str]]:
    outcome = None
    outputs = []
    query_outputs = []
    for message in messages:
        if not isinstance(message, ToolMessage) or message.status == "error":
            continue
        if message.name not in {
            "submit_task_result",
            "execute_sql",
            "freeze_artifact",
            "render_amount_comparison",
            "verify_amount_comparison",
        } or not isinstance(message.content, str):
            continue
        try:
            data = json.loads(message.content)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if message.name == "submit_task_result":
            outcome = TaskOutcome.model_validate(data) if "verdict" in data else None
        elif data.get("status") == "success" and isinstance(data.get("path"), str):
            outputs.append(data["path"])
            if message.name == "execute_sql":
                query_outputs.append(data["path"])
    return outcome, list(dict.fromkeys(outputs)), list(dict.fromkeys(query_outputs))
