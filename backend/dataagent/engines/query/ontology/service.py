"""售后试点的受控执行。仅由显式开启的角色进入，读取与新查询都重新授权。"""

from contextlib import aclosing
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from dataagent.engines.identity.contracts import AssetIdentity
from dataagent.engines.metadata.contracts import (
    QueryCatalogColumn,
    QueryCatalogSnapshot,
)
from dataagent.engines.query.contracts import (
    ALL_OUTPUTS,
    QueryExecutionLimits,
    QueryExecutionOptions,
)
from dataagent.engines.query.errors import SemanticAuditError
from dataagent.engines.query.execution.guard import QueryGuardService
from dataagent.engines.query.execution.recorder import build_sql_template
from dataagent.engines.query.ontology.recurrence import (
    checked_query,
    evidence_csv,
    summarize,
)
from dataagent.engines.query.ontology.registry import (
    OntologyExecution,
    OntologyExperience,
    binding,
    digest,
    load_definition,
)
from dataagent.engines.query.repositories.doris import DorisQueryRepository


class OntologyService:
    def __init__(self, identity, meta, clients, config):
        self.identity = identity
        self.meta = meta
        self.clients = clients
        self.config = config

    async def _authorized(self, key):
        if not self.config.ontology_enabled:
            raise PermissionError("语义功能已停用")
        principal = await self.identity.resolve_query_principal(key.user_id)
        policy = await self.identity.scoped_asset_policy(
            key.user_id, principal, self.config.ontology_database
        )
        scope = self.config.ontology_role_scopes.get(principal.role_name)
        if not scope or policy.role_name != principal.role_name:
            raise PermissionError("当前角色没有启用售后查询")
        client = await self.clients.get_or_create(
            principal.role_name, principal.query_user, principal.password
        )
        sources = load_definition()["sources"]
        for spec in sources.values():
            for col in spec["columns"]:
                if not policy.allows(
                    AssetIdentity(
                        self.config.data_source,
                        self.config.ontology_database,
                        spec["table"],
                        col,
                    )
                ):
                    raise PermissionError("缺少完整的售后来源访问权限")
        # 此连接使用当前角色；目录变化或列改型使绑定失效。
        async with client.engine.connect() as conn:
            cols = (
                await conn.execute(
                    text(
                        "SELECT TABLE_NAME,COLUMN_NAME,DATA_TYPE FROM information_schema.columns WHERE TABLE_SCHEMA=:db"
                    ),
                    {"db": self.config.ontology_database},
                )
            ).all()
            classification = (
                await conn.execute(
                    text(
                        "SELECT raw_code,category_id FROM mfg_fault_category WHERE scope_id=:scope ORDER BY raw_code"
                    ),
                    {"scope": scope},
                )
            ).all()
        types = {(t, c): v.lower() for t, c, v in cols}
        dependencies = {}
        for spec in sources.values():
            for col, kind in spec["columns"].items():
                actual = types.get((spec["table"], col))
                if actual not in (
                    {"char", "varchar", "string", "text"}
                    if kind == "text"
                    else {"double"}
                    if kind == "real"
                    else {"bigint"}
                ):
                    raise ValueError("物理字段不完整或类型已改变")
                dependencies[spec["table"] + "." + col] = actual
        contract = {
            "physical_columns": dependencies,
            "access_hash": policy.authorization_fingerprint,
            "classification_hash": digest([list(x) for x in classification]),
            "database": self.config.ontology_database,
            "scope": scope,
        }

        async def catalog():
            return QueryCatalogSnapshot(
                tuple(s["table"] for s in sources.values()),
                tuple(QueryCatalogColumn(t, c, v) for t, c, v in cols),
            )

        return (
            principal,
            scope,
            client,
            contract,
            QueryGuardService(catalog, current_database=self.config.ontology_database),
        )

    async def bundle(self, key):
        principal, _, _, deps, _ = await self._authorized(key)
        async with self.meta.session.begin() as session:
            row = await binding(
                session, key.user_id, key.conversation_id, key.analysis_id, digest(deps)
            )
            contract_hash = digest([row.content_hash, deps])
            experiences = (
                await session.execute(
                    select(
                        OntologyExperience.template_hash,
                        OntologyExecution.sql,
                        OntologyExecution.manifest,
                        OntologyExecution.evidence,
                    )
                    .join(
                        OntologyExecution,
                        OntologyExecution.id == OntologyExperience.execution_id,
                    )
                    .where(
                        OntologyExperience.role_name == principal.role_name,
                        OntologyExperience.access_hash == deps["access_hash"],
                        OntologyExperience.operation == row.definition["operation"],
                        OntologyExperience.contract_hash == contract_hash,
                    )
                    .order_by(OntologyExperience.recorded_at.desc())
                    .limit(3)
                )
            ).all()
            # 仅返回可重新编译的操作结构；历史用户的对象和原始数值不会进入模型。
            templates = [
                fp
                for fp, sql, m, e in experiences
                if m["semantic_validation_status"] == "passed"
                and m["audit_status"] == "recorded"
                and digest(e) == m["result_sha256"]
                and digest(sql) == m["sql_hash"]
            ]
            return {
                "verified_templates": templates,
                "key": row.key,
                "revision": row.revision,
                "content_hash": row.content_hash,
                "semantic_contract_hash": digest([row.content_hash, deps]),
                "operation": row.definition["operation"],
                "rule": row.definition["rule"],
                "timezone": row.definition["timezone"],
                "time_window": "完整月份用 month；自定义窗口包含开始、不包含结束",
                "rule_summary": row.definition["rule_summary"],
                "product_scope": row.definition["dataset_scope"],
                "products": row.definition["products"],
                "sources": row.definition["sources"],
                "relations": row.definition["relations"],
                "source_kind": row.definition["source_kind"],
            }

    async def execute(self, key, request):
        principal, scope, client, deps, guard = await self._authorized(key)
        async with self.meta.session.begin() as session:
            row = await binding(
                session, key.user_id, key.conversation_id, key.analysis_id, digest(deps)
            )
            fingerprint = row.content_hash
            version = row.revision
        contract = digest([fingerprint, deps])
        sql = await checked_query(request, scope, guard)
        limits = QueryExecutionLimits(
            workload_group=principal.workload_group,
            timeout_seconds=self.config.timeout_seconds,
            memory_limit_bytes=self.config.memory_limit_bytes,
        )
        options = QueryExecutionOptions(
            batch_size=self.config.batch_size, sample_rows=0
        )
        started = datetime.now(UTC).isoformat()
        rows = []
        async with aclosing(
            DorisQueryRepository(client).stream(sql, limits, options)
        ) as batches:
            async for batch in batches:
                if len(rows) + len(batch.rows) > self.config.ontology_max_evidence_rows:
                    raise ValueError("证据超过试点单次上限，请缩短时间窗或指定设备")
                rows.extend(
                    dict(zip(batch.column_names, r, strict=True)) for r in batch.rows
                )
        evidence = summarize(request.model_copy(update={"outputs": ALL_OUTPUTS}), rows)
        requested_coverage = {k: evidence["coverage"][k] for k in request.outputs}
        execution_id = uuid4()
        manifest = {
            "execution_id": str(execution_id),
            "query_mode": "semantic",
            "operation": request.operation,
            "analysis_id": key.analysis_id,
            "ontology_key": "manufacturing_service",
            "ontology_revision": version,
            "ontology_content_hash": fingerprint,
            "semantic_contract_hash": contract,
            "dependencies": deps,
            "authorization_fingerprint": principal.authorization_fingerprint,
            "request": request.model_dump(mode="json"),
            "source_read_at": started,
            "source_snapshot_id": None,
            "source_snapshot_supported": False,
            "sql_hash": digest(sql),
            "plan_hash": digest([contract, request.model_dump(mode="json")]),
            "result_sha256": digest(evidence),
            "csv_sha256": sha256(evidence_csv(evidence["evidence"])).hexdigest(),
            "csv_url": f"/api/v1/chat/{key.conversation_id}/ontology/{key.analysis_id}/{execution_id}/evidence.csv",
            "result_row_count": len(rows),
            "evidence_url": f"/api/v1/chat/{key.conversation_id}/ontology/{key.analysis_id}/{execution_id}/evidence",
            "coverage": requested_coverage,
            "unknown_reasons": evidence["unknown_reasons"],
            "semantic_validation_status": "passed"
            if all(requested_coverage.values())
            else "incomplete",
            "audit_status": "recorded",
            "source_kind": "synthetic_pilot",
        }
        latest = await self.bundle(key)
        if latest["semantic_contract_hash"] != contract:
            raise ValueError("执行期间定义或来源已变化")
        try:
            async with self.meta.session.begin() as session:
                final = await binding(
                    session,
                    key.user_id,
                    key.conversation_id,
                    key.analysis_id,
                    digest(deps),
                )
                if final.content_hash != fingerprint:
                    raise ValueError("执行期间业务版本已变化")
                session.add(
                    OntologyExecution(
                        id=execution_id,
                        user_id=key.user_id,
                        conversation_id=key.conversation_id,
                        analysis_id=key.analysis_id,
                        manifest=manifest,
                        evidence=evidence,
                        sql=sql,
                    )
                )
                if (
                    self.config.learn_from_queries
                    and manifest["semantic_validation_status"] == "passed"
                ):
                    entry = insert(OntologyExperience).values(
                        role_name=principal.role_name,
                        access_hash=deps["access_hash"],
                        operation=request.operation,
                        contract_hash=contract,
                        template_hash=build_sql_template(sql)[1],
                        execution_id=execution_id,
                    )
                    await session.execute(
                        entry.on_conflict_do_update(
                            index_elements=list(
                                OntologyExperience.__table__.primary_key
                            ),
                            set_={
                                "execution_id": execution_id,
                                "recorded_at": func.now(),
                            },
                        )
                    )
        except SQLAlchemyError as exc:
            raise SemanticAuditError("执行记录事务未提交") from exc
        result = await self.read(key, execution_id)
        result["result"] = {
            k: v
            for k, v in result["result"].items()
            if k in request.outputs or k in {"coverage", "complete", "unknown_reasons"}
        }
        result["result"].update(
            coverage=requested_coverage, complete=all(requested_coverage.values())
        )
        return result

    async def read(self, key, execution_id: UUID):
        async with self.meta.session() as session:
            row = await session.get(OntologyExecution, execution_id)
            if not row or (row.user_id, row.conversation_id, row.analysis_id) != (
                key.user_id,
                key.conversation_id,
                key.analysis_id,
            ):
                raise PermissionError("此分析没有可读执行")
            m, evidence, sql = row.manifest, row.evidence, row.sql
        bundle = await self.bundle(key)
        principal = await self.identity.resolve_query_principal(key.user_id)
        if (
            m["authorization_fingerprint"] != principal.authorization_fingerprint
            or m["semantic_contract_hash"] != bundle["semantic_contract_hash"]
        ):
            raise PermissionError("来源授权或语义依赖已变化")
        if digest(evidence) != m["result_sha256"] or digest(sql) != m["sql_hash"]:
            raise ValueError("证据与权威摘要不一致")
        return {"manifest": m, "result": evidence}

    async def download_csv(self, key, execution_id):
        payload = await self.read(key, execution_id)
        data = evidence_csv(payload["result"]["evidence"])
        if sha256(data).hexdigest() != payload["manifest"].get("csv_sha256"):
            raise ValueError("没有可验证的 CSV 版本")
        return data
