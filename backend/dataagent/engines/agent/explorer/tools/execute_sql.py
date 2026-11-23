"""Explorer 受控只读 SQL 执行工具。"""

from datetime import date
from typing import Annotated, Any, cast
from uuid import UUID

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool
from loguru import logger

from dataagent.engines.agent.common.tools.errors import tool_error
from dataagent.engines.query import QueryExecutionService, standard_query
from dataagent.engines.query.contracts import QueryExecutionScope
from dataagent.engines.query.errors import QueryRejectedError


def create_execute_sql_tool(service: QueryExecutionService) -> BaseTool:
    """使用查询用例处理器构建只读 SQL 工具。"""

    @tool("execute_sql")
    async def execute_sql_tool(
        runtime: ToolRuntime,
        purpose: Annotated[str, "本次 SQL 的具体查询目的"],
        sql: Annotated[str | None, "自定义查询时提供一条只读 SQL"] = None,
        standard_metrics: Annotated[
            list[str] | None,
            "全站已发布指标的标准查询，按召回结果中 standard_query=true 的名称选择。与 sql 二选一。",
        ] = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        """安全执行只读 SQL 并写入会话产物。"""
        if standard_metrics is not None:
            if sql or start is None or end is None:
                return tool_error(
                    "标准查询参数不完整",
                    "请选择标准指标、起日和排他结束日；同时省略自定义 SQL。",
                )
            try:
                sql = standard_query(standard_metrics, start, end)
            except ValueError as exc:
                return tool_error("标准查询无法构建", str(exc))
        elif not sql or start is not None or end is not None:
            return tool_error(
                "查询参数不完整", "自定义 SQL 请在 SQL 中提供全部筛选条件。"
            )
        configurable = cast(dict[str, Any], runtime.config)["configurable"]
        session_key = QueryExecutionScope(
            user_id=configurable["user_id"],
            conversation_id=UUID(configurable["conversation_id"]),
            analysis_id=configurable["analysis_id"],
            agent_type="explorer",
            session_id=configurable["session_id"],
        )
        try:
            result = await service.execute(
                session_key,
                sql,
                purpose=purpose,
                tool_call_id=runtime.tool_call_id,
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, QueryRejectedError):
                logger.warning(
                    f"SQL 校验未通过: conversation_id={session_key.conversation_id}, {exc}"
                )
                return tool_error(
                    "SQL 在提交 Doris 执行前未通过校验",
                    f"{exc}。请根据上述问题修正 SQL，然后再次调用 execute_sql。",
                )
            logger.exception(
                f"只读查询工具执行失败: conversation_id={session_key.conversation_id}"
            )
            return tool_error("只读查询执行失败", exc)
        receipt = {"status": "success", **result.model_dump(mode="json")}
        if standard_metrics is not None:
            receipt["executed_sql"] = sql
        return receipt

    return execute_sql_tool
