"""查询校验、执行和经验检索的公开值对象。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from dataagent.engines.contract.doris import DORIS_WORKLOAD_GROUP_PATTERN

type QueryExecutionStatus = Literal["rejected", "failed", "succeeded"]
type QueryKind = Literal["business", "catalog"]
type QueryAssetKind = Literal["table", "column"]
QUERY_EXPERIENCE_RECALL_LIMIT = 3
QueryExperienceRecallStatus = Literal["success", "partial", "failed"]


class QueryTableRef(BaseModel):
    """查询引用的数据表。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    database: str | None = None
    name: str

    @property
    def qualified_name(self) -> str:
        """返回包含数据库名的表标识。"""
        return f"{self.database}.{self.name}" if self.database else self.name


class QueryColumnRef(BaseModel):
    """查询引用的物理字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    database: str | None = None
    table: str
    name: str

    @property
    def qualified_name(self) -> str:
        """返回包含数据库名和表名的字段标识。"""
        prefix = f"{self.database}." if self.database else ""
        return f"{prefix}{self.table}.{self.name}"


class QueryValidationIssue(BaseModel):
    """一项确定性的 SQL 校验问题。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    table: str | None = None
    column: str | None = None


class QueryValidationResult(BaseModel):
    """SQL 安全检查结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    normalized_sql: str | None
    query_kind: QueryKind = "business"
    tables: list[QueryTableRef] = Field(default_factory=list)
    columns: list[QueryColumnRef] = Field(default_factory=list)
    output_columns: list[str] = Field(default_factory=list)
    issues: list[QueryValidationIssue] = Field(default_factory=list)


class QueryExecutionLimits(BaseModel):
    """Doris 单次查询资源限制。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workload_group: str = Field(
        min_length=1,
        max_length=128,
        pattern=DORIS_WORKLOAD_GROUP_PATTERN,
    )
    timeout_seconds: int = Field(gt=0)
    memory_limit_bytes: int = Field(gt=0)


class QueryExecutionOptions(BaseModel):
    """Doris 查询流式处理与结果摘要选项。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_size: int = Field(gt=0)
    sample_rows: int = Field(default=5, ge=0, le=100)


@dataclass(frozen=True, slots=True)
class QueryBatch:
    """Doris 服务端游标返回的一批结果。"""

    column_names: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


class QueryResultColumn(BaseModel):
    """查询结果字段信息。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: str
    nullable: bool


class QueryTimeRange(BaseModel):
    """时间字段在结果集中的取值范围。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: str
    end: str


class AnalysisQueryResult(BaseModel):
    """写入会话沙箱后的查询结果摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    semantic_validation_status: Literal["not_applied"] = "not_applied"
    audit_status: Literal["unrecorded", "missing", "committed"] = "unrecorded"
    index_status: Literal["not_requested", "queued", "retry_pending"] = "not_requested"
    execution_id: UUID
    sha256: str
    path: str
    columns: list[QueryResultColumn]
    row_count: int
    time_range: dict[str, QueryTimeRange]
    sample: list[dict[str, Any]]


class QueryAssetSnapshot(BaseModel):
    """查询经验返回的资产引用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: QueryAssetKind
    database: str
    table: str
    column: str | None = None
    meta_version: int


class QueryExperienceRecallResult(BaseModel):
    """提供给 Explorer 的紧凑查询经验。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    purpose: str
    execution_id: UUID | None = None
    metric: str | None = None
    data_scope: str | None = None
    sql_template: str
    assets: list[QueryAssetSnapshot]


class QueryExperienceRecall(BaseModel):
    """一次查询经验召回的结果及检索通道状态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: QueryExperienceRecallStatus
    results: list[QueryExperienceRecallResult]


@dataclass(frozen=True, slots=True)
class QueryExecutionScope:
    """查询审计和产物存储所使用的用户、会话及执行归属标识。"""

    user_id: int
    conversation_id: UUID
    analysis_id: str
    agent_type: str
    session_id: str


Output = Literal["event_pairs", "devices", "customers", "summary", "evidence"]
ALL_OUTPUTS: tuple[Output, ...] = (
    "event_pairs",
    "devices",
    "customers",
    "summary",
    "evidence",
)


class SemanticQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["trace_recurrent_faults"]
    window_start: AwareDatetime
    window_end: AwareDatetime = Field(
        description="排他的结束瞬间，以次日零点表示包含整个末日"
    )
    device_ids: tuple[str, ...] = Field(default=(), max_length=500)
    customer_ids: tuple[str, ...] = Field(default=(), max_length=500)
    outputs: tuple[Output, ...] = Field(default=ALL_OUTPUTS, min_length=1)
    as_of: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if self.as_of is not None:
            raise ValueError("AS_OF_UNSUPPORTED")
        if self.window_start >= self.window_end:
            raise ValueError("请求时间窗口必须非空")
        if any(not v.strip() for v in (*self.device_ids, *self.customer_ids)):
            raise ValueError("设备和客户编号不能为空")
        return self


class SemanticMonthRequest(BaseModel):
    """完整自然月，服务端按业务时区生成左闭右开的月份窗口。"""

    model_config = ConfigDict(extra="forbid")
    operation: Literal["trace_recurrent_faults"]
    month: str = Field(
        pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$", description="自然月，例如 2026-10"
    )
    device_ids: tuple[str, ...] = Field(default=(), max_length=500)
    customer_ids: tuple[str, ...] = Field(default=(), max_length=500)
    outputs: tuple[Output, ...] = Field(default=ALL_OUTPUTS, min_length=1)
    as_of: AwareDatetime | None = None

    def resolve(self) -> SemanticQueryRequest:
        year, month = map(int, self.month.split("-"))
        tz = ZoneInfo("Asia/Shanghai")
        start = datetime(year, month, 1, tzinfo=tz)
        end = datetime(year + int(month == 12), month % 12 + 1, 1, tzinfo=tz)
        return SemanticQueryRequest(
            **self.model_dump(exclude={"month"}), window_start=start, window_end=end
        )
