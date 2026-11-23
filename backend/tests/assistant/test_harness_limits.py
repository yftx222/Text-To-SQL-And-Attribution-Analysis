"""用生产仓储和连接级临时表验证执行审核、修补预算和恢复。"""

import asyncio
import os
import selectors
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateTable

from dataagent.engines.assistant.contracts.sessions import DelegationRequest, TaskInput
from dataagent.engines.assistant.models.budget import AnalysisBudget
from dataagent.engines.assistant.repositories.budget import (
    AnalysisBudgetStore,
    BudgetExceeded,
)
from dataagent.engines.contract.config import cfg
from dataagent.engines.infra.postgres_client_manager import PostgresClientManager
from dataagent.engines.query.contracts import QueryExecutionScope
from dataagent.engines.query.execution.service import QueryExecutionService
from dataagent.engines.query.models.execution import QueryExecution, QueryReview
from dataagent.engines.query.repositories.experience_postgres import (
    QueryExperiencePGRepo,
)


async def _check_reviews(database):
    cid, experience, wrong, right = (uuid4() for _ in range(4))
    scope = QueryExecutionScope(1, cid, "orders", "explorer", "source")
    service = QueryExecutionService.__new__(QueryExecutionService)
    service._postgres = database
    service._identity = SimpleNamespace(
        resolve_query_principal=AsyncMock(
            return_value=SimpleNamespace(
                role_name="reader", authorization_fingerprint="scope"
            )
        )
    )

    def path(key):
        return f"/data/.evidence-{cid}/{key}_{'a' * 64}.csv"

    async with database.session() as session, session.begin():
        for key, sql in (
            (wrong, "SELECT count(*) FROM orders"),
            (right, "SELECT count(DISTINCT order_id) FROM orders"),
        ):
            session.add(
                QueryExecution(
                    id=key,
                    experience_id=experience,
                    raw_sql=sql,
                    purpose="9 月订单数",
                    role_name="reader",
                    authorization_fingerprint="scope",
                    user_id=1,
                    conversation_id=cid,
                    analysis_id="orders",
                    session_id="source",
                    status="succeeded",
                    result_summary={"path": path(key), "sha256": "a" * 64},
                )
            )

    async def examples():
        async with database.session() as session:
            return await QueryExperiencePGRepo(session).approved_examples([experience])

    async def review(key, review_id, passed, replaces=None):
        await service.record_query_review(
            scope,
            key,
            path(key),
            "a" * 64,
            review_id,
            passed=passed,
            metric="订单ID去重",
            data_scope="2026-09",
            reason="206 笔" if passed else "301 行明细重复",
            evidence=["proof"],
            replaces_path=replaces,
        )

    assert not await examples()
    await review(wrong, "reject-v1", False)
    assert not await examples()
    await review(right, "pass-v2", True, path(wrong))
    await review(right, "pass-v2", True, path(wrong))
    execution, decision = (await examples())[experience]
    assert (
        execution.id,
        decision.replaces_id,
        decision.metric,
        decision.data_scope,
    ) == (right, wrong, "订单ID去重", "2026-09")
    async with database.session() as session:
        reviews = list(await session.scalars(select(QueryReview)))
        assert len(reviews) == 2 and any(
            r.execution_id == wrong and not r.passed for r in reviews
        )
    await review(right, "reject-v2", False)
    assert not await examples()


async def _check_budget(database):
    cid = uuid4()
    limits = SimpleNamespace(
        max_delegations=24, max_repairs=3, analysis_seconds=1800, delegation_seconds=600
    )
    store = AnalysisBudgetStore(database, limits)
    request = DelegationRequest(
        analysis_id="orders",
        session_id="base",
        agent_type="explorer",
        message="9月订单",
    )

    def verdict(status):
        return SimpleNamespace(outcome=SimpleNamespace(verdict=status))

    await store.reserve(cid, request, "v1")
    await store.finish(cid, request, "v1", verdict("passed"))
    review = DelegationRequest(
        analysis_id="orders",
        session_id="audit",
        agent_type="reviewer",
        message="复算",
        inputs=[
            TaskInput(
                agent_type="explorer",
                session_id="base",
                delegation_id="v1",
                path="/csv",
            )
        ],
    )
    await store.reserve(cid, review, "reject")
    await store.finish(cid, review, "reject", verdict("needs_revision"))
    for n in range(1, 4):
        if n == 3:
            store = AnalysisBudgetStore(database, limits)
        ident = f"repair-{n}"
        first = await store.reserve(cid, request, ident)
        assert 0 < await store.reserve(cid, request, ident) <= first
        await store.finish(cid, request, ident, verdict("passed"))
    with pytest.raises(BudgetExceeded, match="修补"):
        await store.reserve(cid, request, "repair-4")
    independent = request.model_copy(update={"session_id": "other"})
    await store.reserve(cid, independent, "other")


def test_postgres_harness_reviews_and_budget():
    if not os.getenv("DATAAGENT_TEST_PG_RECEIPT"):
        pytest.skip("requires project PostgreSQL")

    async def run():
        db = PostgresClientManager(cfg.langgraph_postgresql)
        try:
            async with db.engine.connect() as conn:
                async with conn.begin():
                    for model in (AnalysisBudget, QueryExecution, QueryReview):
                        ddl = str(
                            CreateTable(
                                model.__table__, include_foreign_key_constraints=[]
                            ).compile(dialect=conn.dialect)
                        )
                        await conn.exec_driver_sql(
                            ddl.replace("CREATE TABLE", "CREATE TEMP TABLE", 1)
                        )
                database = SimpleNamespace(
                    session=async_sessionmaker(conn, expire_on_commit=False)
                )
                await _check_reviews(database)
                await _check_budget(database)
        finally:
            await db.close()

    if sys.platform == "win32":
        asyncio.run(
            run(),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    else:
        asyncio.run(run())


def test_time_and_delegation_limits_charge_parallel_work_and_resume():
    limits = SimpleNamespace(
        max_delegations=3, max_repairs=3, analysis_seconds=10, delegation_seconds=6
    )
    store = AnalysisBudgetStore(None, limits)
    ledger = {"attempts": {}, "issues": {}}
    request = DelegationRequest(
        analysis_id="orders", session_id="base", agent_type="explorer", message="查询"
    )
    assert store.reserve_locked(ledger, 100, request, "one") == 6
    assert store.reserve_locked(ledger, 102, request, "one") == 4
    assert store.reserve_locked(ledger, 102, request, "two") == 4
    with pytest.raises(BudgetExceeded, match="时间"):
        store.reserve_locked(ledger, 102, request, "three")
    store.finish_locked(ledger, 103, request, "one", None)
    assert store.reserve_locked(ledger, 105, request, "one") == 3
    store.finish_locked(ledger, 108, request, "one", None)
    store.finish_locked(ledger, 106, request, "two", None)
    with pytest.raises(BudgetExceeded, match="时间"):
        store.reserve_locked(ledger, 108, request, "three")
    limits.analysis_seconds = 20
    store.reserve_locked(ledger, 109, request, "three")
    with pytest.raises(BudgetExceeded, match="委派次数"):
        store.reserve_locked(ledger, 109, request, "four")


def test_reviewer_decision_is_bound_to_actual_explorer_output():
    from dataagent.engines.assistant.contracts import DelegationCheckpointRecord
    from tests.assistant.test_delivery_gate import Scenario

    async def verify():
        case = Scenario()
        case.query.record_query_review = AsyncMock()
        source = case.task("explorer", "source", b"206")
        review = case.task("reviewer", "check", b"recomputed 206", [source])
        raw = case.states["orders", "reviewer", "check"]["delegation_records"][
            review.delegation_id
        ]
        record = DelegationCheckpointRecord.model_validate(raw)
        with pytest.raises(ValueError, match="指标口径"):
            await case.gate.record_query_review(record.task, record.result)
        record.result.outcome.metric = "按订单 ID 去重"
        record.result.outcome.data_scope = "2026-09"
        await case.gate.record_query_review(record.task, record.result)
        call = case.query.record_query_review.await_args
        assert call.args[2] == source.path and call.kwargs["passed"]
        assert review.path in call.kwargs["evidence"]
        record.task.inputs[0] = source.model_copy(update={"session_id": "other"})
        with pytest.raises((KeyError, ValueError)):
            await case.gate.record_query_review(record.task, record.result)
        case.query.record_query_review.assert_awaited_once()

    asyncio.run(verify())


def test_exhausted_delegations_end_turn_without_another_model_call():
    from langchain_core.messages import ToolMessage

    from dataagent.engines.agent.common.middleware.message_context import _budget_stop

    failed = ToolMessage(
        name="delegation",
        tool_call_id="one",
        content='{"failure_code":"budget_exhausted","analysis_id":"orders","content":"修补已达上限"}',
    )
    assert "修补已达上限" in _budget_stop([failed]).result[0].content
    other = ToolMessage(
        name="delegation", tool_call_id="two", content='{"status":"completed"}'
    )
    assert _budget_stop([failed, other]) is None
    crashed = failed.model_copy(
        update={
            "content": '{"failure_code":"specialist_type_error","analysis_id":"orders","content":"TypeError"}'
        }
    )
    assert "TypeError" in _budget_stop([crashed]).result[0].content


def test_submitted_html_handoff_requires_matching_evidence_in_current_task():
    import json

    from langchain_core.messages import HumanMessage, ToolMessage

    from dataagent.engines.agent.common.middleware.message_context import (
        _submitted_report,
    )

    start = HumanMessage("生成报告")
    frozen = ToolMessage(
        name="freeze_artifact",
        tool_call_id="f",
        content='{"status":"success","path":"/data/v2.html"}',
    )

    def submit(verdict, path):
        return ToolMessage(
            name="submit_task_result",
            tool_call_id="s",
            content=json.dumps(
                {"verdict": verdict, "checks": {"金额核对": "一致"}, "evidence": [path]}
            ),
        )

    passed = submit("passed", "/data/v2.html")
    result = _submitted_report([start, frozen, passed])
    assert "/data/v2.html" in result.result[0].content
    assert "金额核对" in result.result[0].content
    assert _submitted_report([start, frozen, passed], "reviewer") is None
    signed = passed.model_copy(
        update={
            "content": json.dumps(
                {
                    "verdict": "passed",
                    "checks": {"金额核对": "一致"},
                    "evidence": ["/data/input.png", "/data/v2.html"],
                }
            )
        }
    )
    assert _submitted_report([start, frozen, signed], "reviewer")
    assert _submitted_report([start, frozen, start, passed]) is None
    assert _submitted_report([start, frozen, submit("passed", "/data/v1.html")]) is None
    assert (
        _submitted_report([start, frozen, submit("needs_revision", "/data/v2.html")])
        is None
    )
