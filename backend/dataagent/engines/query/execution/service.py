"""只读查询完整用例编排。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from dataagent.engines.contract.config import QueryConfig
from dataagent.engines.identity import IdentityService
from dataagent.engines.infra.doris_client_manager import DorisQueryClientRegistry
from dataagent.engines.infra.postgres_client_manager import PostgresClientManager
from dataagent.engines.metadata import MetadataReader
from dataagent.engines.query.contracts import (
    AnalysisQueryResult,
    QueryExecutionLimits,
    QueryExecutionOptions,
    QueryExecutionScope,
    QueryExecutionStatus,
    QueryValidationResult,
)
from dataagent.engines.query.errors import QueryRejectedError, classify_query_error
from dataagent.engines.query.execution.executor import AnalysisQueryService
from dataagent.engines.query.execution.guard import QueryGuardService
from dataagent.engines.query.execution.recorder import (
    QueryExecutionContext,
    QueryExecutionRecorder,
)
from dataagent.engines.query.experiences.scheduler import (
    CeleryQueryExperienceIndexScheduler,
)
from dataagent.engines.query.models.execution import QueryExecution, QueryReview
from dataagent.engines.query.repositories.doris import DorisQueryRepository
from dataagent.engines.query.repositories.execution_postgres import QueryExecutionPGRepo
from dataagent.engines.query.repositories.experience_postgres import (
    QueryExperiencePGRepo,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dataagent.engines.sandbox import DockerSandboxManager


class QueryExecutionService:
    """使用短数据库会话解析身份、校验 SQL 和记录历史；Doris 查询在会话释放后执行。"""

    def __init__(
        self,
        *,
        identity: IdentityService,
        metadata: MetadataReader,
        postgres: PostgresClientManager,
        query_clients: DorisQueryClientRegistry,
        artifact_store: DockerSandboxManager,
        config: QueryConfig,
        database_name: str,
        index_scheduler: CeleryQueryExperienceIndexScheduler,
    ) -> None:
        """绑定查询用例所需的身份、目录、查询客户端和产物存储。"""
        self._config = config
        self._database_name = database_name
        self._index_scheduler = index_scheduler
        self._identity = identity
        self._metadata = metadata
        self._postgres = postgres
        self._query_clients = query_clients
        self._artifact_store = artifact_store
        self._guard = QueryGuardService(
            metadata.query_catalog, current_database=self._database_name
        )
        self._options = QueryExecutionOptions(
            batch_size=self._config.batch_size,
            sample_rows=self._config.sample_rows,
        )

    async def execute(
        self,
        session_key: QueryExecutionScope,
        sql: str,
        *,
        purpose: str,
        tool_call_id: str | None,
    ) -> AnalysisQueryResult:
        """执行一次只读查询并记录成功或失败事实。"""
        context: QueryExecutionContext | None = None
        validation: QueryValidationResult | None = None
        try:
            principal = await self._identity.resolve_query_principal(
                session_key.user_id
            )
            context = QueryExecutionContext(
                session_key=session_key,
                role_name=principal.role_name,
                authorization_fingerprint=principal.authorization_fingerprint,
                purpose=purpose,
                tool_call_id=tool_call_id,
            )
            validation = await self._guard.check(sql)
            if not validation.valid or validation.normalized_sql is None:
                raise QueryRejectedError(validation)
            normalized_sql = validation.normalized_sql
            connection_provider = await self._query_clients.get_or_create(
                principal.role_name,
                principal.query_user,
                principal.password,
            )
            result = await AnalysisQueryService(
                DorisQueryRepository(connection_provider),
                self._artifact_store,
                QueryExecutionLimits(
                    workload_group=principal.workload_group,
                    timeout_seconds=self._config.timeout_seconds,
                    memory_limit_bytes=self._config.memory_limit_bytes,
                ),
                self._options,
            ).execute(
                session_key,
                normalized_sql,
                purpose=purpose,
            )
        except Exception as exc:
            status, error_code = classify_query_error(exc)
            await self._record_failure_safely(
                context,
                raw_sql=sql,
                status=status,
                error_code=error_code,
                error_detail=str(exc).strip() or "异常未提供详情",
                validation=exc.result
                if isinstance(exc, QueryRejectedError)
                else validation,
            )
            raise
        return await self._record_success_safely(
            context,
            raw_sql=sql,
            normalized_sql=normalized_sql,
            validation=validation,
            result=result,
        )

    async def _record_success_safely(
        self,
        context: QueryExecutionContext,
        *,
        raw_sql: str,
        normalized_sql: str,
        validation: QueryValidationResult,
        result: AnalysisQueryResult,
    ) -> AnalysisQueryResult:
        """记录成功查询，审计写入失败时记录日志并保留查询结果。"""
        try:
            async with self._postgres.session() as session:
                index_task = await self._recorder(session).record_success(
                    context,
                    raw_sql=raw_sql,
                    normalized_sql=normalized_sql,
                    validation=validation,
                    result=result,
                )
        except Exception:  # noqa: BLE001
            logger.exception("记录成功查询历史失败")
            return result.model_copy(update={"audit_status": "missing"})
        index_status = "not_requested"
        if index_task is not None:
            index_status = (
                "queued"
                if self._index_scheduler.enqueue(*index_task)
                else "retry_pending"
            )
        return result.model_copy(
            update={"audit_status": "committed", "index_status": index_status}
        )

    async def _record_failure_safely(
        self,
        context: QueryExecutionContext | None,
        *,
        raw_sql: str,
        status: QueryExecutionStatus,
        error_code: str,
        error_detail: str,
        validation: QueryValidationResult | None = None,
    ) -> None:
        """记录失败查询，审计写入失败时记录日志并保留查询原始错误。"""
        if context is None:
            return
        try:
            async with self._postgres.session() as session:
                await self._recorder(session).record_failure(
                    context,
                    raw_sql=raw_sql,
                    status=status,
                    error_code=error_code,
                    error_detail=error_detail,
                    validation=validation,
                )
        except Exception:  # noqa: BLE001
            logger.exception("记录失败查询历史失败")

    async def delivery_source_issue(
        self, scope: QueryExecutionScope, execution_id: UUID, path: str, digest: str
    ) -> str | None:
        """核对源文件的审计归属，并按当前数据库身份重新检查授权指纹。"""
        async with self._postgres.session() as session:
            row = await session.get(QueryExecution, execution_id)
            if not row or row.status != "succeeded" or not row.result_summary:
                return "缺少已提交的成功查询审计。请核对本次 execute_sql 回执中的原始 CSV 路径和审计状态；若该次审计未提交，请重新执行对应查询后再发布。"
            if (row.user_id, row.conversation_id, row.analysis_id, row.session_id) != (
                scope.user_id,
                scope.conversation_id,
                scope.analysis_id,
                scope.session_id,
            ):
                return "查询审计不属于当前用户、分析或 Session"
            if (
                row.result_summary.get("path") != path
                or row.result_summary.get("sha256") != digest
            ):
                return "查询审计记录的文件版本与交付文件不一致"
            role, fingerprint = row.role_name, row.authorization_fingerprint
        principal = await self._identity.resolve_query_principal(scope.user_id)
        if (role, fingerprint) != (
            principal.role_name,
            principal.authorization_fingerprint,
        ):
            return "当前查询角色或授权范围已变化，需要重新验证"
        return None

    async def record_query_review(
        self,
        scope: QueryExecutionScope,
        execution_id: UUID,
        path: str,
        digest: str,
        review_id: str,
        *,
        passed: bool,
        metric: str,
        data_scope: str,
        reason: str,
        evidence: list[str],
        replaces_path: str | None,
    ):
        issue = await self.delivery_source_issue(scope, execution_id, path, digest)
        if issue:
            raise ValueError(issue)
        async with self._postgres.session() as session, session.begin():
            previous = None
            if replaces_path:
                previous = await session.scalar(
                    select(QueryExecution.id)
                    .join(QueryReview, QueryExecution.id == QueryReview.execution_id)
                    .where(
                        QueryExecution.user_id == scope.user_id,
                        QueryExecution.conversation_id == scope.conversation_id,
                        QueryExecution.analysis_id == scope.analysis_id,
                        QueryExecution.session_id == scope.session_id,
                        QueryExecution.id != execution_id,
                        QueryExecution.result_summary["path"].as_string()
                        == replaces_path,
                        QueryReview.passed.is_(False),
                    )
                    .limit(1)
                )
                if previous is None:
                    raise ValueError("修补来源必须是同一任务中已被否决的 SQL 版本")
            await session.execute(
                insert(QueryReview)
                .values(
                    execution_id=execution_id,
                    review_id=review_id,
                    passed=passed,
                    metric=metric,
                    data_scope=data_scope,
                    reason=reason,
                    evidence=evidence,
                    replaces_id=previous,
                )
                .on_conflict_do_nothing(
                    index_elements=[QueryReview.execution_id, QueryReview.review_id]
                )
            )

    def _recorder(self, session: AsyncSession) -> QueryExecutionRecorder:
        """使用本次查询会话组装执行记录与经验聚合能力。"""
        return QueryExecutionRecorder(
            execution_repo=QueryExecutionPGRepo(session),
            experience_repo=QueryExperiencePGRepo(session),
            metadata=self._metadata,
            learn_from_queries=self._config.learn_from_queries,
            data_source=self._config.data_source,
            database_name=self._database_name,
        )
