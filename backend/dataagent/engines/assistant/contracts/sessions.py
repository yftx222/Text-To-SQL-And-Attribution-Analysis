"""专家 Session 管理、委派请求、结果与持久化状态协议。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)

from dataagent.engines.assistant.contracts.base import (
    Identifier,
    NonEmptyText,
    StrictProtocolModel,
    SubagentRunStatus,
)
from dataagent.engines.contract.analysis import AgentType


class DelegationMessageContext(StrictProtocolModel):
    """持久化在 Specialist 输入消息中的委派边界。"""

    delegation_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ]


class TaskInput(StrictProtocolModel):
    """当前分析中某个专家委派实际输出的固定文件。"""

    agent_type: AgentType
    session_id: Identifier
    delegation_id: NonEmptyText
    path: NonEmptyText


class TaskOutcome(StrictProtocolModel):
    verdict: Literal["passed", "needs_revision", "blocked"]
    checks: dict[NonEmptyText, NonEmptyText] = Field(
        min_length=1, description="键为委派的验收条件原文，值为实际核对结果"
    )
    evidence: list[NonEmptyText] = Field(default_factory=list)
    repair_target: str = ""
    metric: str = ""
    data_scope: str = ""
    replaces_path: str | None = None


class CalendarWindow(StrictProtocolModel):
    start: date = Field(strict=False, description="业务起始日期（含）")
    end: date = Field(strict=False, description="业务结束日期（不含）")
    as_of_end: bool = Field(default=False, description="需要还原统计期末事件状态时设为 true；明确要求跟踪至今天时保持 false。")

    @model_validator(mode="after")
    def check_order(self):
        if self.start >= self.end:
            raise ValueError("结束日期必须晚于起始日期")
        return self


class DelegationRequest(StrictProtocolModel):
    """Delegation 是 Planner 向专业 Agent 发起的一次工作委派。

    请求定位可复用的 Session；每次委派以独立 delegation_id 记录结果，
    同一 Session 可以接收多次委派并延续工作上下文。"""

    analysis_id: Identifier
    agent_type: AgentType
    session_id: Identifier
    message: NonEmptyText
    inputs: list[TaskInput] = Field(default_factory=list)
    acceptance: list[NonEmptyText] = Field(default_factory=list)
    calendar_window: CalendarWindow | None = Field(
        default=None,
        description="普通 SQL 按整日、整月或整年统计时填写用户要求的起止日期，用于核对首要查询证据；全历史或本体操作可省略。",
    )
    sum_required: bool = Field(
        default=False,
        description="目标为对业务明细的数量或金额求总和时设为 true；明细去重和支付等资格条件保留，最后仍须汇总度量。单纯计行、计单或查已汇总值时省略。",
    )


class ListSessionsRequest(StrictProtocolModel):
    """查询当前 Conversation 内专业 Session 的请求。"""

    analysis_id: Identifier | None = None


class DeleteSessionRequest(StrictProtocolModel):
    """删除专业 Agent Session 的请求。"""

    analysis_id: Identifier
    agent_type: AgentType
    session_id: Identifier


class SemanticReceipt(StrictProtocolModel):
    execution_id: UUID = Field(strict=False)
    window_basis: Literal["recurrence_occurred_at"]
    window_start: datetime = Field(strict=False)
    window_end: datetime = Field(strict=False)
    customer_ids: list[str]
    device_ids: list[str]
    unavailable_customer_ids: list[str]
    summary: dict[str, int] | None
    complete: bool


class DelegationResult(StrictProtocolModel):
    """运行时记录的委派状态、Session 身份和 Agent 原始文本。"""

    status: Literal["completed", "failed"]
    failure_code: Literal["budget_exhausted", "specialist_type_error"] | None = None
    analysis_id: Identifier
    agent_type: AgentType
    session_id: Identifier
    content: str = Field(min_length=1)
    delegation_id: str | None = None
    outcome: TaskOutcome | None = None
    query_outputs: list[str] = Field(default_factory=list)
    primary_sql: str | None = None
    primary_values: dict[str, str] = Field(default_factory=dict)
    semantic_receipts: list[SemanticReceipt] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)


class DelegationCheckpointRecord(StrictProtocolModel):
    """持久化一次委派的运行状态和文本结果。"""

    delegation_id: NonEmptyText
    status: SubagentRunStatus
    result: DelegationResult | None = None
    task: DelegationRequest | None = None


class SessionSummary(StrictProtocolModel):
    """单个专业 Agent Session 的结构化摘要。"""

    analysis_id: Identifier
    agent_type: AgentType
    session_id: Identifier
    status: Literal[
        "active",
        "completed",
        "failed",
        "interrupted",
    ]
    delegation_id: str | None = None
    business_status: Literal["passed", "needs_revision", "blocked", "stale"] | None = (
        None
    )
    inputs: list[TaskInput] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    summary: NonEmptyText | None = None
    updated_at: datetime | None = None


class ListSessionsResult(StrictProtocolModel):
    """当前 Conversation 内的专业 Session 列表。"""

    analysis_id: Identifier | None = None
    sessions: list[SessionSummary]


class DeleteSessionResult(StrictProtocolModel):
    """删除专业 Agent Session 的成功响应。"""

    status: Literal["success"] = "success"
    analysis_id: Identifier
    agent_type: AgentType
    session_id: Identifier
    existed: bool
    message: NonEmptyText
