"""用本回合已核对的单行查询结果生成简短数值答复。"""

import json
import re
from decimal import Decimal
from typing import Annotated

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import BaseModel, Field


class AnswerColumn(BaseModel):
    column: str = Field(description="primary_values 中的真实列名")
    label: str = Field(
        min_length=1,
        max_length=60,
        pattern=r"^[^\r\n]+$",
        description="读者看到的指标或组成项名称，可包含日期和范围",
    )
    unit: str = Field(
        default="",
        max_length=8,
        pattern=r"^[^\r\n]*$",
        description="该列实际单位；百分数列用 %，原始小数比例用空单位",
    )


MissingInput = Annotated[str, Field(min_length=1, max_length=60, pattern=r"^[^\r\n]+$")]


@tool
def deliver_query_values(
    delegation_id: str,
    columns: Annotated[list[AnswerColumn], Field(min_length=1, max_length=8)],
    runtime: ToolRuntime,
    missing_inputs: Annotated[
        list[MissingInput],
        Field(
            max_length=4,
            description="用户明确问仅凭现有指标能否推算，且不能时填写所缺的明细、对应关系或口径。每项只写缺少的信息。",
        ),
    ]
    | None = None,
) -> dict:
    """交付已核对的查询数值。用户同时问已知信息是否足以推算另一个量时，可用 missing_inputs 说明缺什么。归因、对账或其他解释走正常回答。"""
    question = next((m for m in reversed(runtime.state["messages"]) if isinstance(m, HumanMessage)), None)
    text = question.text if question else ""
    percent_only = re.search(r"(?:^|[。？?！!，,\s])(?:请)?(?:只|仅)(?:回复|返回|输出)(?:一个)?百分[数比](?:[。.!！\s]|$)", text)
    for message in reversed(runtime.state["messages"]):
        if isinstance(message, HumanMessage):
            break
        if (
            not isinstance(message, ToolMessage)
            or message.name != "delegation"
            or message.tool_call_id != delegation_id
        ):
            continue
        if message.status == "error" or not isinstance(message.content, str):
            break
        result = json.loads(message.content)
        if (
            result.get("agent_type") != "explorer"
            or result.get("status") != "completed"
            or (result.get("outcome") or {}).get("verdict") != "passed"
        ):
            break
        values = result.get("primary_values") or {}
        if any(item.column not in values for item in columns):
            return {
                "status": "needs_revision",
                "reason": "所选列不在已审计的单行首项结果中，请核对列名或补查。",
            }
        if percent_only and (len(columns) != 1 or columns[0].unit != "%"):
            return {"status": "needs_revision", "reason": "用户只要百分数。请查询有依据的百分比列，核对分子、分母和单位后重新交付。"}
        lines = []
        for item in columns:
            number = Decimal(values[item.column])
            formatted = format(number, ",f")
            if "." in formatted and item.unit == "%":
                formatted = formatted.rstrip("0").rstrip(".")
            lines.append(f"{formatted}%" if percent_only else f"{item.label}：{formatted}{item.unit}")
        if missing_inputs:
            lines.append(
                "仅凭题中给出的汇总指标，无法推算所问结果。还需要"
                + "、".join(missing_inputs)
                + "。"
            )
        return {"status": "delivered", "answer": "\n".join(lines)}
    return {
        "status": "needs_revision",
        "reason": "本回合缺少指定的、通过验收的 Explorer 委派。",
    }
