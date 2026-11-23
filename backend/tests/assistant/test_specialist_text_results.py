"""文本终答按委派边界读取，附件与普通 Agent 消息共用展示协议。"""

import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.engines.assistant.contracts import DELEGATION_CONTEXT_KEY
from dataagent.engines.assistant.sessions.checkpoint_view import (
    SpecialistCheckpointView,
)


def _boundary(delegation_id: str) -> HumanMessage:
    return HumanMessage(
        content="task",
        additional_kwargs={DELEGATION_CONTEXT_KEY: {"delegation_id": delegation_id}},
    )


@pytest.mark.parametrize(
    ("sql", "issue"),
    [
        ("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM orders", True),
        (
            "SELECT SUM(units) FROM orders WHERE created_at >= '2028-02-01' AND created_at < '2028-03-01'",
            False,
        ),
        (
            "SELECT COUNT(*) FROM (SELECT id FROM orders WHERE created_at BETWEEN '2028-02-01' AND '2028-02-29 23:59:59' GROUP BY id) d",
            True,
        ),
        (
            "SELECT COUNT(*) FROM orders WHERE biz_date BETWEEN '2028-02-01' AND '2028-02-29'",
            False,
        ),
        (
            "SELECT SUM(CASE WHEN created_at >= '2028-02-01' AND created_at < '2028-03-01' THEN units ELSE 0 END) FROM orders",
            False,
        ),
    ],
)
def test_submitted_query_calendar_window(sql, issue):
    from dataagent.engines.agent.specialist.outcome import primary_query_issue
    from dataagent.engines.assistant.contracts.sessions import CalendarWindow

    window = CalendarWindow(start="2028-02-01", end="2028-03-01")
    assert bool(primary_query_issue(sql, window)) is issue


def test_quantity_total_and_detail_count_have_different_units():
    from dataagent.engines.agent.specialist.outcome import primary_query_issue

    count = "SELECT COUNT(DISTINCT line_id) FROM eligible_lines"
    assert primary_query_issue(count, None, True)
    assert primary_query_issue(count, None, False) is None
    assert (
        primary_query_issue("SELECT SUM(units) FROM eligible_lines", None, True) is None
    )


def test_first_evidence_uses_sql_from_matching_audited_call():
    from dataagent.engines.agent.specialist.outcome import primary_query_result

    messages = []
    for key, sql, status in (
        ("main", "SELECT COUNT(*) FROM orders", "committed"),
        ("other", "SELECT 1", "missing"),
    ):
        messages += [
            AIMessage(
                content="",
                tool_calls=[{"id": key, "name": "execute_sql", "args": {"sql": sql}}],
            ),
            ToolMessage(
                name="execute_sql",
                tool_call_id=key,
                content=json.dumps(
                    {
                        "status": "success",
                        "audit_status": status,
                        "path": key,
                        "row_count": 1,
                        "sample": [{"count": 0, "note": "辅助说明", "missing": None}],
                    }
                ),
            ),
        ]
    assert primary_query_result(messages, "main") == (
        "SELECT COUNT(*) FROM orders",
        {"count": "0"},
    )
    assert primary_query_result(messages, "other") == (None, {})
    assert primary_query_result(messages, "invented") == (None, {})


def test_only_current_complete_answer_is_returned():
    view = SpecialistCheckpointView(
        {
            "messages": [
                _boundary("old"),
                AIMessage(content="old answer"),
                _boundary("current"),
                AIMessage(
                    content="current answer",
                    response_metadata={"finish_reason": "stop"},
                ),
                _boundary("next"),
                AIMessage(content="next answer"),
            ]
        }
    )
    assert view.final_response("current") == "current answer"
    assert view.final_response("missing") is None


def test_tool_work_does_not_reuse_previous_text_as_final_answer():
    view = SpecialistCheckpointView(
        {
            "messages": [
                _boundary("old"),
                AIMessage(content="old answer"),
                _boundary("current"),
                AIMessage(
                    content="查询中",
                    tool_calls=[{"id": "q", "name": "execute_sql", "args": {}}],
                ),
                ToolMessage(content="rows", tool_call_id="q"),
            ]
        }
    )
    assert view.final_response("current") is None


def test_receipt_uses_only_current_successful_semantic_execution():
    from dataagent.engines.agent.specialist.outcome import semantic_receipts

    def body(customer):
        return json.dumps(
            {
                "status": "success",
                "manifest": {
                    "execution_id": str(uuid4()),
                    "audit_status": "recorded",
                    "window_basis": "recurrence_occurred_at",
                    "unavailable_customer_ids": [],
                    "request": {
                        "window_start": "2026-05-01T00:00:00+08:00",
                        "window_end": "2026-06-01T00:00:00+08:00",
                        "customer_ids": [customer],
                        "device_ids": [],
                    },
                },
                "result": {"summary": {"events": 3}, "complete": True},
            }
        )

    def query(payload):
        return ToolMessage(
            content=payload, name="execute_semantic_query", tool_call_id="q"
        )

    view = SpecialistCheckpointView(
        {
            "messages": [
                _boundary("old"),
                query(body("old_customer")),
                _boundary("current"),
                AIMessage(content=body("claimed_customer")),
                query('{"status":"error"}'),
                ToolMessage(
                    content=body("unrelated"), name="execute_sql", tool_call_id="other"
                ),
                query(body("requested_customer")),
            ]
        }
    )
    activity = view.delegation_activity("current", active=False)
    receipts = semantic_receipts(activity.messages)
    assert [r.customer_ids for r in receipts] == [["requested_customer"]]
    assert receipts[0].summary == {"events": 3}
