"""售后复发 V1 的封闭请求、受控查询和证据汇总。时间在源映射层统一为 UTC 微秒。"""

import csv
import io
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from dataagent.engines.query.contracts import SemanticQueryRequest
from dataagent.engines.query.execution.guard import QueryGuardService

NON_MATCHES = {"NO_PREDECESSOR", "PREDECESSOR_OPEN", "OUTSIDE_INTERVAL"}
UNKNOWN_REASONS = {
    "EVENT_IDENTITY_UNRESOLVED",
    "DEVICE_UNRESOLVED",
    "FAULT_UNMAPPED",
    "HISTORY_INCOMPLETE",
    "PREDECESSOR_AMBIGUOUS",
    "LIFECYCLE_INCOMPLETE",
    "LIFECYCLE_AMBIGUOUS",
    "CLOSE_TIME_MISSING",
}
RULE_FILE = Path(__file__).with_name("recurrent_faults.sql")
MAX_INTERVAL_US = 30 * 24 * 3600 * 1000000


def utc_us(ts: datetime) -> int:
    delta = ts.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def compile_query(
    request: SemanticQueryRequest, *, authorized_scope: str, dialect: str = "doris"
) -> str:
    """scope 由调用方的服务端权限解析器提供。模型只能填写 SemanticQueryRequest。"""
    if not authorized_scope:
        raise ValueError("缺少授权范围")
    tree = sqlglot.parse_one(RULE_FILE.read_text("utf8"), read="doris")
    literals = {
        "authorized_scope": exp.Literal.string(authorized_scope),
        "start_us": exp.Literal.number(utc_us(request.window_start)),
        "end_us": exp.Literal.number(utc_us(request.window_end)),
    }
    for param in list(tree.find_all(exp.Placeholder)):
        param.replace(literals[param.name].copy())
    if request.device_ids:
        tree = tree.where(
            exp.column("device_id", table="n").isin(
                *[exp.Literal.string(v) for v in request.device_ids]
            )
        )
    if request.customer_ids:
        ids = [exp.Literal.string(v) for v in request.customer_ids]
        customer = exp.column("customer_id", table="o")
        # Preserve unresolved ownership for coverage, within the requested visible cohort.
        visible = (
            exp.select("1")
            .from_("mfg_customer")
            .where(
                exp.column("scope_id").eq(exp.Literal.string(authorized_scope)),
                exp.column("customer_id").isin(*ids),
            )
        )
        tree = tree.where(
            exp.Exists(this=visible),
            exp.or_(
                customer.isin(*ids),
                customer.is_(exp.Null()),
                exp.column("relations", table="o").neq(1),
            ),
        )
    return tree.sql(dialect=dialect)


async def checked_query(
    request: SemanticQueryRequest, scope: str, guard: QueryGuardService
) -> str:
    sql = compile_query(request, authorized_scope=scope)
    result = await guard.check(sql)
    if not result.valid or not result.normalized_sql:
        raise ValueError(f"语义查询未通过只读检查：{result.issues}")
    return result.normalized_sql


def summarize(request: SemanticQueryRequest, rows: Iterable[Mapping[str, Any]]) -> dict:
    """按同一证据集统计；缺失客户不进客户数，范围不明单独披露。"""
    evidence, pairs, devices, customers, reasons = [], [], set(), set(), {}
    seen, matched_events = set(), set()
    unknown_customer = 0
    low, high = utc_us(request.window_start), utc_us(request.window_end)
    for item in rows:
        r = dict(item)
        reason = r["reason"]
        record = r["record_id"]
        if record in seen or not low <= r["occurred_us"] < high:
            raise ValueError("重复证据或事件超出请求时间窗")
        seen.add(record)
        if reason not in NON_MATCHES | UNKNOWN_REASONS | {"MATCHED"}:
            raise ValueError("无法识别的规则判定")
        customer = r["customer_id"]
        if request.customer_ids and customer and customer not in request.customer_ids:
            continue
        if reason == "MATCHED":
            if not (
                r["event_id"]
                and r["prior_event"]
                and r["event_id"] != r["prior_event"]
                and r["event_id"] not in matched_events
                and r["prior_record"]
                and r["device_id"]
                and r["category_id"]
                and r["transition_id"]
                and r["last_state"] == "closed"
                and r["prior_us"] <= r["last_us"]
                and 0 < r["occurred_us"] - r["last_us"] <= MAX_INTERVAL_US
            ):
                raise ValueError("复发事件证据不完整或时间关系不成立")
            if customer and not (
                r["relations"] == 1
                and r["relation_id"]
                and r["valid_from_us"] <= r["occurred_us"]
                and (r["valid_to_us"] is None or r["occurred_us"] < r["valid_to_us"])
            ):
                raise ValueError("使用客户关系证据不完整")
            matched_events.add(r["event_id"])
            if not customer:
                unknown_customer += 1
                if request.customer_ids:
                    continue
            else:
                customers.add(customer)
            pairs.append([r["prior_event"], r["event_id"]])
            devices.add(r["device_id"])
            r["elapsed_us"] = r["occurred_us"] - r["last_us"]
            r["customer_status"] = (
                "confirmed"
                if customer
                else "conflict"
                if (r["relations"] or 0) > 1
                else "unknown"
            )
        if reason in UNKNOWN_REASONS:
            reasons[reason] = reasons.get(reason, 0) + 1
        r["status"] = (
            "MATCHED"
            if reason == "MATCHED"
            else "NOT_MATCHED"
            if reason in NON_MATCHES
            else "UNKNOWN"
        )
        if not request.customer_ids or customer:
            evidence.append(r)
    affected_complete = not reasons and not unknown_customer
    event_complete = not reasons and not (request.customer_ids and unknown_customer)
    coverage = {
        k: (
            affected_complete
            if k in {"customers", "evidence", "summary"}
            else event_complete
        )
        for k in request.outputs
    }
    values = {
        "evidence": evidence,
        "event_pairs": pairs,
        "devices": sorted(devices),
        "customers": sorted(customers),
        "summary": {
            "events": len(pairs),
            "devices": len(devices),
            "confirmed_customers": len(customers),
            "unknown_customer_events": unknown_customer,
            "unknown_recurrence_records": sum(reasons.values()),
        },
    }
    return {
        "coverage": coverage,
        "complete": all(coverage.values()),
        "unknown_reasons": reasons,
        **{k: values[k] for k in request.outputs},
    }


def handler_digest() -> str:
    return sha256(RULE_FILE.read_bytes()).hexdigest()


def evidence_csv(rows):
    fields = (
        "record_id",
        "event_id",
        "device_id",
        "device_model",
        "asset_tag",
        "product_source",
        "category_id",
        "occurred_us",
        "prior_record",
        "prior_event",
        "prior_us",
        "transition_id",
        "last_us",
        "last_state",
        "reason",
        "relations",
        "customer_id",
        "relation_id",
        "valid_from_us",
        "valid_to_us",
        "status",
        "elapsed_us",
        "customer_status",
    )
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf8")
