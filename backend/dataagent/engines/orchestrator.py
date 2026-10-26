"""Conversation 级 Agent 运行时装配。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from dataagent.engines.agent.common.filesystem import analyst_skill_mount
from dataagent.engines.agent.common.graph import create_agent
from dataagent.engines.agent.common.shell_jobs import ShellJobRuntime
from dataagent.engines.agent.explorer.tools.execute_sql import create_execute_sql_tool
from dataagent.engines.agent.explorer.tools.mcp import get_mcp_tools
from dataagent.engines.agent.explorer.tools.semantic_recall import (
    create_semantic_recall_tools,
)
from dataagent.engines.agent.planner.tools.delegation import create_delegation_tools
from dataagent.engines.agent.specialist.invoker import (
    DelegationExecutor,
    SpecialistAgentRun,
)
from dataagent.engines.agent.specialist.state import SpecialistAgentState
from dataagent.engines.assistant.execution.activity import SessionActivity
from dataagent.engines.assistant.models.session import AgentSessionKey
from dataagent.engines.assistant.repositories.checkpoint import PostgresCheckpointStore
from dataagent.engines.assistant.repositories.session import SessionCheckpointRepository
from dataagent.engines.assistant.sessions.control import SessionControl
from dataagent.engines.assistant.sessions.management import AgentSessionService
from dataagent.engines.common.llm import create_configured_model
from dataagent.engines.contract import config as app_config
from dataagent.engines.contract.analysis import AGENT_TYPES, AgentType
from dataagent.engines.infra.postgres_advisory_locks import PostgresAdvisoryLocks
from dataagent.engines.prompts import load_prompt
from dataagent.engines.query import QueryExecutionService
from dataagent.engines.sandbox import DockerSandboxManager
from dataagent.engines.sandbox.contracts import SandboxSessionScope

if TYPE_CHECKING:
    from dataagent.engines.assistant.recall.service import SemanticRecallService


@dataclass(slots=True)
class ConversationAgentRuntime:
    """一个用户会话内的 Agent 运行时资源。"""

    planner: CompiledStateGraph
    shell_jobs: ShellJobRuntime


@dataclass(frozen=True, slots=True)
class _SharedAgentResources:
    """跨 Conversation 复用的模型和 MCP 工具。"""

    planner_model: BaseChatModel
    specialist_models: dict[AgentType, BaseChatModel]
    mcp_tools: tuple[BaseTool, ...]


class ConversationAgentRuntimeFactory:
    """初始化共享能力并装配 Conversation 级 Agent 运行时。"""

    def __init__(
        self,
        persistence: PostgresCheckpointStore,
        locks: PostgresAdvisoryLocks,
        sandbox: DockerSandboxManager,
        recall: SemanticRecallService,
        query: QueryExecutionService,
        activity: SessionActivity,
    ) -> None:
        """保存运行时依赖，共享资源由 init 创建。"""
        self._activity = activity
        self._recall = recall
        self._query = query
        self._persistence = persistence
        self._locks = locks
        self._sandbox = sandbox
        self._init_lock = asyncio.Lock()
        self._resources: _SharedAgentResources | None = None
        self._model_contexts = AsyncExitStack()

    async def init(self) -> None:
        """初始化所有 Conversation 共享的模型和专业 Agent 能力。"""
        if self._resources is not None:
            return
        async with self._init_lock:
            if self._resources is not None:
                return

            active_model_name = app_config.cfg.lm_config.active
            specialist_model_names: dict[AgentType, str] = {
                agent_type: (
                    active_model_name
                    if app_config.cfg.agent.specialists[agent_type].model == "default"
                    else app_config.cfg.agent.specialists[agent_type].model
                )
                for agent_type in AGENT_TYPES
            }
            configured_names = {active_model_name, *specialist_model_names.values()}
            async with AsyncExitStack() as stack:
                models = {
                    model_name: await stack.enter_async_context(
                        create_configured_model(model_name)
                    )
                    for model_name in configured_names
                }
                specialist_models: dict[AgentType, BaseChatModel] = {
                    agent_type: models[model_name]
                    for agent_type, model_name in specialist_model_names.items()
                }
                self._resources = _SharedAgentResources(
                    planner_model=models[active_model_name],
                    specialist_models=specialist_models,
                    mcp_tools=tuple(await get_mcp_tools()),
                )
                self._model_contexts = stack.pop_all()

    async def create(
        self,
        user_id: int,
        conversation_id: UUID,
    ) -> ConversationAgentRuntime:
        """装配一个隔离的 Conversation Agent 运行时。"""
        await self.init()
        resources = self._resources
        if resources is None:
            raise RuntimeError("Agent 运行时工厂尚未初始化")

        conversation_backend = await self._sandbox.get_backend(
            user_id,
            conversation_id,
        )
        checkpointer = self._persistence.checkpointer
        orchestration = app_config.cfg.agent.orchestration
        session_store = SessionCheckpointRepository(
            user_id=user_id,
            conversation_id=conversation_id,
            persistence=self._persistence,
            checkpointer=checkpointer,
        )
        control = SessionControl(session_store, self._locks, user_id, conversation_id)
        session_service = AgentSessionService(
            session_store=session_store,
            control=control,
            activity=self._activity,
            sandbox=self._sandbox,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        delegation = DelegationExecutor(
            build_agent=self.create_specialist,
            control=control,
            activity=self._activity,
            session_store=session_store,
            user_id=user_id,
            conversation_id=conversation_id,
            max_parallel_sessions=orchestration.max_parallel_sessions,
            max_sessions=orchestration.max_sessions,
        )
        shell_jobs = ShellJobRuntime(conversation_backend.shell_jobs)
        planner = create_agent(
            name="planner",
            system_prompt=load_prompt("agents/planner"),
            filesystem_tools=["read_file"],
            model=resources.planner_model,
            tools=create_delegation_tools(session_service, delegation),
            backend=conversation_backend,
            checkpointer=checkpointer,
            shell_jobs=shell_jobs,
        )
        return ConversationAgentRuntime(
            planner=planner,
            shell_jobs=shell_jobs,
        )

    async def close(self) -> None:
        """释放共享 Agent 配置及其模型客户端。"""
        self._resources = None
        await self._model_contexts.aclose()

    async def create_specialist(
        self, session_key: AgentSessionKey
    ) -> SpecialistAgentRun:
        """取得 Session 资源，现场组装本次委派的专业 Agent。"""
        await self.init()
        resources = self._resources
        if resources is None:
            raise RuntimeError("Agent 运行时工厂尚未初始化")

        backend = await self._sandbox.get_backend(
            session_key.user_id,
            session_key.conversation_id,
            scope=SandboxSessionScope(
                session_key.analysis_id,
                session_key.agent_type,
                session_key.session_id,
            ),
        )
        shell_jobs = ShellJobRuntime(backend.shell_jobs)
        agent_type = session_key.agent_type
        tools: list[BaseTool] = []
        if agent_type == "explorer":
            tools = [
                *create_semantic_recall_tools(self._recall),
                create_execute_sql_tool(self._query),
                *resources.mcp_tools,
            ]
        agent = create_agent(
            name=agent_type,
            system_prompt=load_prompt(f"agents/{agent_type}"),
            model=resources.specialist_models[agent_type],
            tools=tools,
            backend=backend,
            checkpointer=self._persistence.checkpointer,
            shell_jobs=shell_jobs,
            filesystem_tools=["read_file", "write_file", "edit_file"],
            state_schema=SpecialistAgentState,
            skill_mount=analyst_skill_mount() if agent_type == "analyst" else None,
        )
        return SpecialistAgentRun(agent=agent, shell_jobs=shell_jobs)
