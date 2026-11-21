"""正式交付只接受当前分析下、可从专家检查点追溯的固定证据。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage
from loguru import logger

from dataagent.engines.assistant.contracts import DelegationCheckpointRecord
from dataagent.engines.assistant.contracts.base import Identifier
from dataagent.engines.assistant.contracts.sessions import DelegationRequest, TaskInput
from dataagent.engines.assistant.models.session import AgentSessionKey
from dataagent.engines.assistant.sessions.checkpoint_view import (
    SpecialistCheckpointView,
)
from dataagent.engines.query.contracts import QueryExecutionScope
from dataagent.engines.sandbox import resolve_artifact_path
from dataagent.engines.sandbox.errors import SandboxPathError

if TYPE_CHECKING:
    from dataagent.engines.assistant.repositories.delivery import DeliveryReceiptStore
    from dataagent.engines.assistant.repositories.session import (
        SessionCheckpointRepository,
    )
    from dataagent.engines.query import QueryExecutionService
    from dataagent.engines.sandbox import DockerSandboxManager


type SessionSlot = tuple[str, str]
type TaskKey = tuple[str, str, str]
type EvidenceKey = tuple[str, str, str, str]


@dataclass
class AnalysisTasks:
    records: dict[TaskKey, DelegationCheckpointRecord]
    latest: dict[SessionSlot, str]

    def reviews_for(self, ref: TaskInput) -> list[DelegationCheckpointRecord]:
        effective = []
        # Session 的记录按首次委派顺序追加；写入终态时仅更新原键。
        for (role, _, _), record in self.records.items():
            task = record.task
            if role != "reviewer" or not task or task.inputs[:1] != [ref]:
                continue
            if ref.agent_type == "analyst":
                effective = [old for old in effective if not _covers_review(task, old)]
            effective.append(record)
        return effective


def _covers_review(new: DelegationRequest, old: DelegationCheckpointRecord) -> bool:
    """同一报告补交复核可覆盖原范围；已指出的业务缺陷仍需修订报告版本。"""
    previous = old.task
    outcome = old.result.outcome if old.result else None
    return (
        previous is not None
        and new.session_id == previous.session_id
        and (outcome is None or outcome.verdict == "passed")
        and set(previous.acceptance).issubset(new.acceptance)
        and all(source in new.inputs for source in previous.inputs)
    )


class DeliveryBlocked(ValueError):
    pass


class DeliveryGate:
    def __init__(
        self,
        user_id: int,
        conversation_id: UUID,
        store: SessionCheckpointRepository,
        files: DockerSandboxManager,
        query: QueryExecutionService,
        receipts: DeliveryReceiptStore,
    ):
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.store = store
        self.files = files
        self.query = query
        self.receipts = receipts

    async def evaluate(self, analysis_id: Identifier, target: TaskInput) -> dict:
        try:
            relative = resolve_artifact_path(
                target.path, self.conversation_id
            ).relative_path
            if not relative.startswith("evidence/"):
                raise DeliveryBlocked(
                    "请从本轮委派的 outputs 与 outcome.evidence 中选择同一固定路径；Explorer 的 CSV 使用 query_outputs 中的原始版本。"
                )
            tasks = await self._load_tasks(analysis_id)
            visited: set[EvidenceKey] = set()
            if target.agent_type == "reviewer":
                raise DeliveryBlocked("请选择查询或分析产物")
            reviews: set[str] = set()
            await self._verify_tree(analysis_id, target, tasks, visited, set(), reviews)
            receipt_id = uuid5(
                self.conversation_id, analysis_id + "/" + target.model_dump_json()
            )
            receipt = {
                "status": "verified",
                "receipt_id": str(receipt_id),
                "analysis_id": analysis_id,
                "target": target.model_dump(),
                "f_path": relative,
                "reviews": sorted(reviews),
                "sources": [
                    TaskInput(
                        agent_type=role,
                        session_id=session,
                        delegation_id=ident,
                        path=path,
                    ).model_dump()
                    for role, session, ident, path in sorted(visited)
                ],
            }
            return await self.receipts.save(receipt_id, self.conversation_id, receipt)
        except DeliveryBlocked as exc:
            return {"status": "needs_revision", "reason": str(exc)}
        except SandboxPathError:
            return {
                "status": "needs_revision",
                "reason": "产物路径无效，请从本次委派结果原样复制固定版本的完整路径。",
            }
        except Exception:  # noqa: BLE001 — 读取或提交失败时禁止确认交付。
            logger.exception("交付证据核验失败")
            return {
                "status": "blocked",
                "reason": "无法读取任务、审计或固定证据，请检查后再试",
            }

    async def check_report_sources(self, request: DelegationRequest):
        """在生成或复核 HTML 前指出不能作为正式查询依据的 Explorer 输入。"""
        sources = [ref for ref in request.inputs if ref.agent_type == "explorer"]
        if not sources:
            return None
        tasks = await self._load_tasks(request.analysis_id)
        visited = set()
        for ref in sources:
            record = tasks.records.get(
                (ref.agent_type, ref.session_id, ref.delegation_id)
            )
            if record and record.task and not record.task.acceptance:
                return {
                    "status": "needs_revision",
                    "reason": "上游 Explorer 委派未填写 acceptance。",
                    "invalid_input": ref.model_dump(mode="json"),
                    "action": "回到该 Explorer Session，用 report_delivery=true 和具体 acceptance 发起委派并提交原始查询证据，再将新 query_inputs 交给下游。本轮任意 CSV 都缺少验收。",
                }
            try:
                await self._verify_tree(
                    request.analysis_id, ref, tasks, visited, set(), set()
                )
            except DeliveryBlocked as exc:
                return {
                    "status": "needs_revision",
                    "reason": str(exc),
                    "invalid_input": ref.model_dump(mode="json"),
                    "action": "根据 Explorer 返回的 query_inputs，选择与本题对应的原始 CSV 作为 inputs；口径说明可写在 message 中。",
                }
        return None

    async def _load_tasks(self, analysis_id: str) -> AnalysisTasks:
        records = {}
        latest = {}
        for ns in await self.store.list_namespaces(analysis_id):
            parts = ns.split("/")
            if len(parts) != 4 or parts[:2] != ["subagents", analysis_id]:
                continue
            _, _, role, session = parts
            key = AgentSessionKey(
                self.user_id, self.conversation_id, analysis_id, role, session
            )
            state = (await self.store.read_state(key)).values
            latest[role, session] = state.get("current_delegation_id")
            view = SpecialistCheckpointView(state)
            for ident in state.get("delegation_records", {}):
                record = view.delegation_record(ident)
                if record:
                    records[role, session, ident] = record

        return AnalysisTasks(records, latest)

    async def _verify_tree(
        self,
        analysis_id: str,
        ref: TaskInput,
        tasks: AnalysisTasks,
        visited: set[EvidenceKey],
        ancestors: set[TaskKey],
        reviews: set[str],
    ) -> None:
        key = (ref.agent_type, ref.session_id, ref.delegation_id)
        if tasks.latest.get(key[:2]) != key[2]:
            raise DeliveryBlocked("输入已过期或不属于当前分析：" + ref.path)
        record = tasks.records.get(key)
        if (
            not record
            or not record.task
            or not record.result
            or record.status != "completed"
        ):
            raise DeliveryBlocked("缺少已完成的任务记录")
        if key in ancestors:
            raise DeliveryBlocked("任务输入存在循环依赖")
        result, task = record.result, record.task
        if (task.analysis_id, task.agent_type, task.session_id) != (
            analysis_id,
            *key[:2],
        ):
            raise DeliveryBlocked("任务归属不匹配")
        if (
            not task.acceptance
            or not result.outcome
            or result.outcome.verdict != "passed"
        ):
            raise DeliveryBlocked(
                result.outcome.repair_target
                if result.outcome and result.outcome.repair_target
                else "缺少验收条件或业务通过判定"
            )
        missing = [
            item for item in task.acceptance if item not in result.outcome.checks
        ]
        if missing:
            raise DeliveryBlocked(
                "请在原 Session 补交 checks，缺少验收条件原文：" + "；".join(missing)
            )
        if ref.path not in result.outputs or ref.path not in result.outcome.evidence:
            sources = (
                result.query_outputs if ref.agent_type == "explorer" else result.outputs
            )
            available = [path for path in sources if path in result.outcome.evidence]
            detail = (
                ("；可按用途选择本轮已登记的证据：" + "、".join(available))
                if available
                else "；请在原 Session 用实际工具返回的路径补交 evidence"
            )
            raise DeliveryBlocked("指定版本缺少本轮工具产物或验收证据" + detail)
        if (*key, ref.path) in visited:
            return
        await self._verify_bytes(ref.path)
        if ref.agent_type == "explorer":
            name = PurePosixPath(ref.path)
            version, _, digest = name.stem.partition("_")
            scope = QueryExecutionScope(
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                analysis_id=analysis_id,
                agent_type="explorer",
                session_id=ref.session_id,
            )
            if name.suffix != ".csv":
                raise DeliveryBlocked("查询交付需要 execute_sql 返回的 CSV 版本")
            if result.query_outputs and ref.path not in result.query_outputs:
                originals = [
                    path
                    for path in result.query_outputs
                    if path in result.outcome.evidence
                ]
                reason = "所选 CSV 是另存版本。请使用本次 execute_sql 返回并列入验收证据的原始 CSV"
                if originals:
                    reason += "，按目标口径从以下已有版本选择：" + "、".join(originals)
                raise DeliveryBlocked(reason)
            issue = await self.query.delivery_source_issue(
                scope, UUID(version), ref.path, digest
            )
            if issue:
                raise DeliveryBlocked(issue)
        elif not task.inputs:
            raise DeliveryBlocked("分析产物缺少上游输入")
        dependencies = list(task.inputs)
        bound_reviews = tasks.reviews_for(ref)
        if ref.agent_type == "analyst" and not bound_reviews:
            raise DeliveryBlocked("当前报告版本缺少独立复核")
        for review in bound_reviews:
            await self._review(review, ref)
            dependencies.extend(review.task.inputs[1:])
            reviews.add(review.delegation_id)
        for source in dependencies:
            await self._verify_tree(
                analysis_id, source, tasks, visited, ancestors | {key}, reviews
            )
        visited.add((*key, ref.path))

    async def _review(
        self, record: DelegationCheckpointRecord, target: TaskInput
    ) -> None:
        result = record.result
        outcome = result.outcome if result else None
        if record.status != "completed" or not outcome or outcome.verdict != "passed":
            raise DeliveryBlocked(
                outcome.repair_target
                if outcome and outcome.repair_target
                else "复核尚未通过"
            )
        if not record.task.acceptance:
            raise DeliveryBlocked("请为 Reviewer 委派填写验收条件")
        missing = [
            item for item in record.task.acceptance if item not in outcome.checks
        ]
        if missing:
            raise DeliveryBlocked(
                "请在原 Reviewer Session 补交 checks，缺少验收条件原文："
                + "；".join(missing)
            )
        if target.path not in outcome.evidence:
            raise DeliveryBlocked(
                "请在原 Reviewer Session 沿用本次 inputs 和 acceptance 补交复核。"
                "evidence 用单独一项填写复核目标的完整路径，并附本轮固定的独立核对文件："
                + target.path
            )
        declared = {source.path for source in record.task.inputs}
        if set(outcome.evidence).difference(declared, result.outputs):
            raise DeliveryBlocked("复核引用了未登记的证据")
        independent = set(outcome.evidence).intersection(result.outputs) - {target.path}
        if not independent:
            raise DeliveryBlocked("缺少 Reviewer 本轮生成的固定核对证据")
        for path in independent:
            await self._verify_bytes(path)

    async def record_query_review(self, request, result):
        if (
            request.agent_type != "reviewer"
            or not request.inputs
            or request.inputs[0].agent_type != "explorer"
        ):
            return
        target = request.inputs[0]
        name = PurePosixPath(target.path)
        if name.suffix != ".csv" or result.outcome is None:
            return
        key = AgentSessionKey(
            self.user_id,
            self.conversation_id,
            request.analysis_id,
            "explorer",
            target.session_id,
        )
        state = (await self.store.read_state(key)).values
        record = SpecialistCheckpointView(state).delegation_record(target.delegation_id)
        if (
            not record
            or record.status != "completed"
            or not record.result
            or target.path not in record.result.outputs
        ):
            raise DeliveryBlocked("SQL 复核目标不在源委派实际产物中")
        if target.path not in record.result.query_outputs:
            return
        verdict = result.outcome
        passed = verdict.verdict == "passed"
        if not passed and target.path not in verdict.evidence:
            return  # 技术阻断或未检查该 SQL 时保留候选状态。
        if passed:
            if state.get("current_delegation_id") != target.delegation_id:
                raise DeliveryBlocked("待审核 SQL 版本已过期")
            if not verdict.metric.strip() or not verdict.data_scope.strip():
                raise DeliveryBlocked("SQL 经验审核需要指标口径和数据范围")
            await self._review(
                DelegationCheckpointRecord(
                    delegation_id=result.delegation_id,
                    status="completed",
                    task=request,
                    result=result,
                ),
                target,
            )
        await self._verify_bytes(target.path)
        version, _, digest = name.stem.partition("_")
        scope = QueryExecutionScope(
            self.user_id,
            self.conversation_id,
            request.analysis_id,
            "explorer",
            target.session_id,
        )
        await self.query.record_query_review(
            scope,
            UUID(version),
            target.path,
            digest,
            result.delegation_id,
            passed=passed,
            metric=verdict.metric,
            data_scope=verdict.data_scope,
            reason=str(verdict.checks) if passed else verdict.repair_target,
            evidence=verdict.evidence,
            replaces_path=verdict.replaces_path,
        )

    async def _verify_bytes(self, path: str) -> None:
        relative = resolve_artifact_path(path, self.conversation_id).relative_path
        if not relative.startswith("evidence/"):
            raise DeliveryBlocked("文件尚未固定版本")
        # download_file 校验真实归档字节的摘要、权限和会话归属。
        try:
            await self.files.download_file(self.user_id, self.conversation_id, relative)
        except (FileNotFoundError, OSError) as exc:
            raise DeliveryBlocked(f"证据文件不存在或摘要校验失败：{relative}") from exc


def create_delivery_tool(gate: DeliveryGate):
    @tool
    async def publish_artifact(
        runtime: ToolRuntime, analysis_id: Identifier, target: TaskInput
    ) -> dict:
        """核对当前任务依赖、查询审计和复核证据，正式交付指定固定版本。"""
        messages = runtime.state.get("messages", [])
        last = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if last and len(last.tool_calls) > 1:
            return {"status": "blocked", "reason": "请在其他工具返回后单独提交正式交付"}
        return await gate.evaluate(analysis_id, target)

    return publish_artifact
