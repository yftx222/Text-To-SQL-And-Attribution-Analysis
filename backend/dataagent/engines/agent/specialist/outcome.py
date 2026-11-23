"""从本轮实际工具回执登记产物和专家业务结论。"""

import json
from collections.abc import Sequence
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from langchain.tools import tool
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from dataagent.engines.assistant.contracts.sessions import (
    CalendarWindow,
    SemanticReceipt,
    TaskOutcome,
)


def primary_query_result(
    messages: Sequence[BaseMessage], path: str
) -> tuple[str | None, dict[str, str]]:
    """Resolve the submitted CSV to the successful, audited call in this delegation."""
    calls = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call["name"] == "execute_sql":
                    calls[call["id"]] = call["args"].get("sql")
        elif (
            isinstance(message, ToolMessage)
            and message.name == "execute_sql"
            and message.status != "error"
            and isinstance(message.content, str)
        ):
            try:
                result = json.loads(message.content)
            except ValueError:
                continue
            if (
                isinstance(result, dict)
                and result.get("path") == path
                and result.get("status") == "success"
                and result.get("audit_status") == "committed"
            ):
                sql = (
                    (result.get("executed_sql") or calls[message.tool_call_id])
                    if message.tool_call_id in calls
                    else None
                )
                values = {}
                sample = result.get("sample")
                if (
                    sql
                    and result.get("row_count") == 1
                    and isinstance(sample, list)
                    and len(sample) == 1
                    and isinstance(sample[0], dict)
                ):
                    for column, value in sample[0].items():
                        s = str(value)
                        if len(s) > 80:
                            continue
                        try:
                            number = Decimal(s)
                            if (
                                number.is_finite()
                                and abs(number.as_tuple().exponent) <= 80
                            ):
                                values[column] = s
                        except InvalidOperation:
                            continue
                return sql, values
    return None, {}


def primary_query_issue(
    sql: str | None, window: CalendarWindow | None, sum_required: bool = False
) -> str | None:
    if not sql:
        return "首项证据须为本轮用于回答问题的已审计 execute_sql CSV。请按委派口径执行并提交。"
    try:
        tree = parse_one(sql, read="doris")
    except ParseError:
        return "无法解析首项证据对应的 SQL，请重新核实该查询。"
    if sum_required and tree.find(exp.Sum) is None:
        return "本题要汇总业务数量或金额；首项证据没有 SUM。请先按资格和业务键去重，再汇总对应度量，将该查询作为首项证据。"
    if window is None:
        return None
    if window.as_of_end:
        for ranked in tree.find_all(exp.Window):
            select = ranked.find_ancestor(exp.Select)
            source = select.args.get("from_") if select is not None else None
            if (
                isinstance(ranked.this, exp.RowNumber)
                and isinstance(source, exp.From)
                and isinstance(source.this, exp.Table)
                and source.this.name.endswith("_status_event_di")
                and not any(select.args.get(key) for key in ("where", "joins"))
            ):
                return f"首项 SQL 在全部状态事件上取末态，可能把期后事件计入本期。请先用事件时间 < '{window.end}' 限定历史，再按业务键取最后状态。"
    # A plain whole-table aggregate cannot enforce a calendar window.
    if (
        isinstance(tree, exp.Select)
        and isinstance(tree.args.get("from_"), exp.From)
        and isinstance(tree.args["from_"].this, exp.Table)
        and not any(
            tree.args.get(k) for k in ("where", "with_", "joins", "qualify", "having")
        )
        and all(
            isinstance(item.unalias(), exp.AggFunc)
            and isinstance(item.unalias().this, (exp.Star, exp.Column, exp.Distinct))
            and not item.find(exp.Predicate, exp.If, exp.Case, exp.Select)
            for item in tree.expressions
        )
    ):
        return f"首项证据是全表聚合。请在实际主查询限定 [{window.start}, {window.end}) 后重新执行。"
    last_second = datetime.combine(window.end, time.min) - timedelta(seconds=1)
    for where in tree.find_all(exp.Where):
        for condition in (
            where.this.flatten() if isinstance(where.this, exp.And) else [where.this]
        ):
            bound = (
                condition.args.get("high")
                if isinstance(condition, exp.Between)
                else condition.expression
                if isinstance(condition, exp.LTE)
                else None
            )
            if isinstance(bound, exp.Literal) and bound.is_string:
                try:
                    end = datetime.fromisoformat(bound.this)
                except ValueError:
                    continue
                if end == last_second:
                    return f"首项证据截至 {last_second}，会漏掉最后一秒内的小数时间。请使用时间字段 < '{window.end}' 并重新执行。"
    return None


def published_time_issue(
    messages, metric: str, sql: str, window: CalendarWindow | None
) -> str | None:
    """对已召回且明确声明归期的指标，核对查询是否使用该事件的时间。"""
    if not window or not metric:
        return None
    for msg in reversed(messages):
        if (
            not isinstance(msg, ToolMessage)
            or msg.name not in {"recall_context", "get_recall", "merge_recalls"}
            or not isinstance(msg.content, str)
        ):
            continue
        try:
            record = json.loads(msg.content)
        except ValueError:
            continue
        for name, meta in (record.get("metrics") or {}).items():
            if (
                not (
                    metric == name
                    or any(
                        metric.startswith(name + separator)
                        for separator in ("（", "(", "，", "：", " ")
                    )
                )
                or "归属统计期" not in meta["description"]
            ):
                continue
            clocks = {
                (c["t_name"], c["c_name"])
                for c in meta.get("relevant_columns", [])
                if c["c_name"].endswith("_time") and c["c_name"] != "event_time"
            }
            if len(clocks) != 1:
                continue
            table, clock = clocks.pop()
            tree = parse_one(sql, read="doris")
            for condition in tree.find_all(exp.Where, exp.Join):
                predicate = (
                    condition.this
                    if isinstance(condition, exp.Where)
                    else condition.args.get("on")
                )
                if predicate is None:
                    continue
                select = condition.find_ancestor(exp.Select)
                if select is None:
                    continue
                local = [
                    t
                    for t in select.find_all(exp.Table)
                    if t.find_ancestor(exp.Select) is select
                ]
                aliases = {t.alias_or_name for t in local if t.name == table}
                for col in predicate.find_all(exp.Column):
                    if col.find_ancestor(exp.Select) is not select:
                        continue
                    if col.name == clock or (
                        col.name == "biz_date"
                        and (
                            col.table in aliases
                            or (not col.table and aliases and len(local) == 1)
                        )
                    ):
                        return None
            return f"已发布指标「{name}」按 {table}.{clock} 归期。首项 SQL 没有按此业务时钟筛选；请对照定义重查，明确指定的其他队列需单列为自定义口径。"
    return None


def semantic_receipts(messages: Sequence[BaseMessage]) -> list[SemanticReceipt]:
    receipts = {}
    for message in messages:
        if (
            not isinstance(message, ToolMessage)
            or message.name != "execute_semantic_query"
            or message.status == "error"
            or not isinstance(message.content, str)
        ):
            continue
        try:
            payload = json.loads(message.content)
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("status") != "success":
            continue
        manifest = payload["manifest"]
        if (
            manifest.get("window_basis") != "recurrence_occurred_at"
            or manifest.get("audit_status") != "recorded"
        ):
            continue
        request = manifest["request"]
        receipt = SemanticReceipt(
            execution_id=manifest["execution_id"],
            window_basis=manifest["window_basis"],
            window_start=request["window_start"],
            window_end=request["window_end"],
            customer_ids=request["customer_ids"],
            device_ids=request["device_ids"],
            unavailable_customer_ids=manifest["unavailable_customer_ids"],
            summary=payload["result"].get("summary"),
            complete=payload["result"]["complete"],
        )
        receipts[receipt.execution_id] = receipt
    return list(receipts.values())


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
