"""Planner 专用工具测试。"""

import json
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from dataagent.engines.agent.planner.tools.delegation import create_delegation_tools
from dataagent.engines.agent.planner.tools.query_answer import deliver_query_values


def make_runtime() -> ToolRuntime:
    """构造仅供工具调用的最小运行时。"""
    return ToolRuntime(
        state={},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="delegation-call",
        store=None,
    )


def test_query_value_delivery_requires_a_current_passed_receipt_and_real_column():
    runtime = make_runtime()
    messages = runtime.state["messages"] = [HumanMessage(content="金额合计？")]
    data = {
        "status": "completed",
        "agent_type": "explorer",
        "outcome": {"verdict": "passed"},
        "primary_values": {"total": "0.00"},
    }
    messages.append(
        ToolMessage(
            content=json.dumps(data), name="delegation", tool_call_id="observed"
        )
    )
    args = {
        "runtime": runtime,
        "delegation_id": "observed",
        "columns": [{"column": "total", "label": "合计", "unit": "元"}],
    }
    assert deliver_query_values.invoke(args) == {
        "status": "delivered",
        "answer": "合计：0.00元",
    }
    limited = deliver_query_values.invoke({**args, "missing_inputs": ["逐行分摊金额"]})
    assert (
        limited["answer"]
        == "合计：0.00元\n仅凭题中给出的汇总指标，无法推算所问结果。还需要逐行分摊金额。"
    )
    assert (
        deliver_query_values.invoke(
            {**args, "columns": [{"column": "invented", "label": "其他"}]}
        )["status"]
        == "needs_revision"
    )
    data["outcome"]["verdict"] = "needs_revision"
    messages[-1] = ToolMessage(
        content=json.dumps(data), name="delegation", tool_call_id="observed"
    )
    assert deliver_query_values.invoke(args)["status"] == "needs_revision"
    data["outcome"]["verdict"] = "passed"
    messages[-1] = ToolMessage(
        content=json.dumps(data), name="delegation", tool_call_id="observed"
    )
    messages.append(HumanMessage(content="换一个月份"))
    assert deliver_query_values.invoke(args)["status"] == "needs_revision"


def test_planner_ends_with_only_the_verified_numeric_receipt():
    from dataagent.engines.agent.common.middleware.message_context import (
        MessageContextMiddleware,
    )

    middleware = MagicMock(_agent_name="planner")
    name = "deliver_query_values"
    call = AIMessage(content="", tool_calls=[{"name": name, "id": "pick", "args": {}}])
    receipt = ToolMessage(
        name=name,
        tool_call_id="pick",
        content=json.dumps({"status": "delivered", "answer": "合计：0.00元"}),
    )
    response = MessageContextMiddleware._terminal_response(middleware, [call, receipt])
    assert response.result[0].content == "合计：0.00元"
    receipt.content = '{"status": "needs_revision"}'
    assert (
        MessageContextMiddleware._terminal_response(middleware, [call, receipt]) is None
    )


class PlannerToolsTest(unittest.IsolatedAsyncioTestCase):
    """验证 Planner 工具将入口错误转为结构化结果。"""

    async def test_invalid_request_returns_validation_details(self) -> None:
        tool = create_delegation_tools(MagicMock(), MagicMock())[0]

        with self.assertRaises(ValidationError) as caught:
            await tool.ainvoke(
                {
                    "runtime": make_runtime(),
                    "analysis_id": "",
                    "agent_type": "explorer",
                    "session_id": "session",
                    "message": "分析数据",
                }
            )
        self.assertEqual(caught.exception.errors()[0]["loc"], ("analysis_id",))

    async def test_unexpected_failure_includes_exception_detail(self) -> None:
        service = MagicMock()
        service.execute_delegation = AsyncMock(
            side_effect=RuntimeError("Planner 执行状态不可用")
        )
        tool = create_delegation_tools(service, service)[0]
        runtime = make_runtime()

        result = await cast(Any, tool).coroutine(
            runtime=runtime,
            analysis_id="analysis",
            agent_type="explorer",
            session_id="session",
            message="分析数据",
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "delegation_failed")
        self.assertEqual(result["error"], "Planner 执行状态不可用")
        call = service.execute_delegation.await_args
        self.assertEqual(call.kwargs["delegation_id"], "delegation-call")
        self.assertIs(call.kwargs["activity_writer"], runtime.stream_writer)

    async def test_list_sessions_rejects_invalid_analysis_id(self) -> None:
        tool = create_delegation_tools(MagicMock(), MagicMock())[1]

        with self.assertRaises(ValidationError):
            await tool.ainvoke({"analysis_id": "Invalid ID"})

    async def test_delete_session_rejects_invalid_request(self) -> None:
        tool = create_delegation_tools(MagicMock(), MagicMock())[2]
        with self.assertRaises(ValidationError):
            await tool.ainvoke(
                {"analysis_id": "analysis", "agent_type": "analyst", "session_id": ""}
            )

    async def test_delete_session_includes_execution_error_detail(self) -> None:
        service = MagicMock()
        service.delete_session = AsyncMock(side_effect=TimeoutError("获取锁超时"))
        tool = create_delegation_tools(service, service)[2]

        result = await cast(Any, tool).coroutine(
            analysis_id="analysis",
            agent_type="analyst",
            session_id="session",
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "delete_session_failed")
        self.assertEqual(result["error"], "获取锁超时")
