"""本体业务边界与角色集成；真实库用例仅写 PostgreSQL 会话临时表。"""

import os
from contextlib import AsyncExitStack
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dataagent.engines.assistant.models.session import AgentSessionKey
from dataagent.engines.common.async_runtime import run_async
from dataagent.engines.contract.config import cfg
from dataagent.engines.identity import IdentityService
from dataagent.engines.infra.doris_client_manager import (
    DorisClientManager,
    DorisQueryClientRegistry,
)
from dataagent.engines.infra.postgres_client_manager import PostgresClientManager
from dataagent.engines.query.contracts import (
    AnalysisQueryResult,
    SemanticMonthRequest,
    SemanticQueryRequest,
)
from dataagent.engines.query.errors import SemanticAuditError
from dataagent.engines.query.ontology.registry import (
    AnalysisOntology,
    OntologyDefinition,
    OntologyExecution,
    OntologyExperience,
    load_definition,
    publish,
    revoke,
)
from dataagent.engines.query.ontology.service import OntologyService


def test_request_and_raw_sql_boundaries():
    request = SemanticMonthRequest(
        operation="trace_recurrent_faults", month="2026-10"
    ).resolve()
    assert request.window_end.isoformat() == "2026-11-01T00:00:00+08:00"
    for overrides in (
        {"sql": "SELECT 1"},
        {"scope_id": "other"},
        {"semantic_validation_status": "passed"},
        {"operation": "other"},
    ):
        with pytest.raises(ValidationError):
            SemanticQueryRequest.model_validate({**request.model_dump(), **overrides})
    assert (
        AnalysisQueryResult.model_fields["semantic_validation_status"].default
        == "not_applied"
    )


@pytest.mark.skipif(
    not os.getenv("DATAAGENT_ONTOLOGY_TEST_USER_ID"), reason="需要显式指定开发试点用户"
)
def test_real_role_and_versioned_execution():
    run_async(run_isolated(int(os.environ["DATAAGENT_ONTOLOGY_TEST_USER_ID"])))


async def run_isolated(user_id):
    request = SemanticMonthRequest(
        operation="trace_recurrent_faults", month="2026-10"
    ).resolve()
    async with AsyncExitStack() as stack:
        pg = PostgresClientManager(cfg.meta_postgresql)
        auth = PostgresClientManager(cfg.auth_postgresql)
        admin = DorisClientManager(cfg.doris)
        clients = DorisQueryClientRegistry(
            cfg.doris.model_copy(update={"database": cfg.query.ontology_database})
        )
        for obj in (pg, auth, admin, clients):
            stack.push_async_callback(obj.close)
        conn = await stack.enter_async_context(pg.engine.connect())
        await conn.begin()
        for model in (
            OntologyDefinition,
            AnalysisOntology,
            OntologyExecution,
            OntologyExperience,
        ):
            name = model.__tablename__
            await conn.exec_driver_sql(
                f"CREATE TEMP TABLE {name} (LIKE public.{name} INCLUDING ALL) ON COMMIT DROP"
            )
        await conn.exec_driver_sql("SET LOCAL search_path=pg_temp")
        sessions = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        settings = cfg.query.model_copy(deep=True)
        settings.learn_from_queries = True
        service = OntologyService(
            IdentityService(auth, admin),
            SimpleNamespace(session=sessions),
            clients,
            settings,
        )
        definition = load_definition()
        async with sessions.begin() as s:
            await publish(s, definition, "integration")
        key = AgentSessionKey(user_id, uuid4(), "pilot", "explorer", "exp")

        def new_key():
            return AgentSessionKey(user_id, uuid4(), "pilot", "explorer", "exp")

        original = await service.bundle(key)
        result = await service.execute(key, request)
        assert result["result"]["customers"] == ["C04", "C07", "C10"]
        assert result["result"]["summary"]["events"] == 6
        assert (
            result["manifest"]["request"]["window_end"] == "2026-11-01T00:00:00+08:00"
        )
        eid = result["manifest"]["execution_id"]
        assert (await service.bundle(key))["verified_templates"]
        denied = await service.execute(
            key, request.model_copy(update={"customer_ids": ("C01",)})
        )
        assert (
            denied["result"]["summary"]["events"] == 0
            and denied["result"]["evidence"] == []
        )
        with pytest.raises(PermissionError):
            await service.read(new_key(), eid)
        # 变更角色映射立即阻止新查数和读证据，恢复后仍需通过数据库实际授权。
        settings.ontology_role_scopes.clear()
        with pytest.raises(PermissionError):
            await service.read(key, eid)
        with pytest.raises(PermissionError):
            await service.execute(key, request)
        settings.ontology_role_scopes = dict(cfg.query.ontology_role_scopes)
        async with sessions.begin() as s:
            await publish(
                s, {**definition, "revision": definition["revision"] + 1}, "integration"
            )
        assert (await service.bundle(key))["revision"] == original["revision"]
        fresh = new_key()
        assert not (await service.bundle(fresh))["verified_templates"]
        # 模拟映射指纹变更；事务仅涉及测试连接内的临时表。
        async with sessions.begin() as s:
            bound = await s.get(
                AnalysisOntology,
                (user_id, fresh.conversation_id, "pilot", "manufacturing_service"),
            )
            bound.dependency_hash = "stale"
        with pytest.raises(ValueError):
            await service.bundle(fresh)
        async with sessions.begin() as s:
            row = await s.get(OntologyExecution, eid)
            row.evidence = {"changed": True}
        with pytest.raises(ValueError, match="摘要"):
            await service.read(key, eid)
        async with sessions.begin() as s:
            await revoke(s, original["revision"], "integration")
        with pytest.raises(ValueError):
            await service.execute(key, request)
        newest = new_key()
        async with sessions.begin() as s:
            before = await s.scalar(
                select(func.count()).select_from(OntologyExperience)
            )
            await s.execute(
                text(
                    "ALTER TABLE ontology_executions ADD CHECK (analysis_id <> 'pilot') NOT VALID"
                )
            )
        with pytest.raises(SemanticAuditError):
            await service.execute(newest, request)
        async with sessions.begin() as s:
            assert (
                await s.scalar(select(func.count()).select_from(OntologyExperience))
                == before
            )
        settings.ontology_enabled = False
        with pytest.raises(PermissionError):
            await service.bundle(newest)
        await conn.rollback()


def test_full_delta_and_revoked_bundle():
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from dataagent.engines.agent.explorer.tools.semantic_recall import (
        create_semantic_recall_tools,
    )
    from dataagent.engines.assistant.contracts.recall import (
        SemanticRecallRecord,
        SemanticRecallUpdate,
    )
    from dataagent.engines.metadata.contracts import SemanticResourceRecallResponse

    now = datetime.now(UTC)
    response = SemanticResourceRecallResponse.model_construct(
        status="success",
        tables=[],
        columns=[],
        values=[],
        metrics=[],
        failures=[],
        warnings=[],
        truncated=False,
    )
    record = SemanticRecallRecord.model_construct(
        user_id=1,
        conversation_id=uuid4(),
        query="故障复发",
        request=None,
        response=response,
        query_experiences=[],
        query_experiences_retrieved_at=now,
        query_experience_role_name=None,
        query_experience_authorization_fingerprint=None,
        source_queries=[],
        created_at=now,
        updated_at=now,
    )

    async def check():
        recall = SimpleNamespace(recall_context=AsyncMock())
        ontology = SimpleNamespace(
            bundle=AsyncMock(
                side_effect=[
                    {"revision": 2},
                    {"revision": 2},
                    PermissionError("已撤权"),
                ]
            )
        )
        tool = create_semantic_recall_tools(
            recall, ontology=ontology, session_key=object()
        )[0]
        outputs = []
        for previous in (None, record, record):
            recall.recall_context.return_value = SemanticRecallUpdate(
                record=record, previous=previous, recalled=response
            )
            outputs.append(
                await tool.coroutine(
                    config={
                        "configurable": {
                            "user_id": 1,
                            "conversation_id": str(record.conversation_id),
                        }
                    },
                    query="故障复发",
                    resource_types=["column"],
                    terms=["设备"],
                )
            )
        assert [x["mode"] for x in outputs] == ["full", "delta", "delta"]
        assert [x["ontology"] for x in outputs] == [
            {"revision": 2},
            {"revision": 2},
            None,
        ]

    run_async(check())
