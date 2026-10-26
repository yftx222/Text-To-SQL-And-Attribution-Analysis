"""后台清理任务专用的会话资源生命周期。"""

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from dataagent.engines.agent.common.filesystem import analyst_skill_mount
from dataagent.engines.assistant.conversations.lifecycle import (
    ConversationLifecycleService,
)
from dataagent.engines.assistant.execution.runtime_cache import AgentManager
from dataagent.engines.assistant.repositories.checkpoint import PostgresCheckpointStore
from dataagent.engines.assistant.repositories.conversation_tombstone import (
    ConversationTombstoneStore,
)
from dataagent.engines.contract.config import cfg
from dataagent.engines.infra.postgres_advisory_locks import PostgresAdvisoryLocks
from dataagent.engines.infra.postgres_client_manager import PostgresClientManager
from dataagent.engines.sandbox import DockerSandboxManager


@dataclass(frozen=True, slots=True)
class ConversationLifecycleResources:
    """清理任务使用的会话服务与沙箱。"""

    conversations: ConversationLifecycleService
    sandbox: DockerSandboxManager


@asynccontextmanager
async def conversation_lifecycle_resources() -> AsyncGenerator[
    ConversationLifecycleResources
]:
    """为一次后台清理创建隔离资源，并在任何退出路径尝试全部清理。"""
    async with AsyncExitStack() as stack:
        persistence = PostgresCheckpointStore(cfg.langgraph_postgresql)
        stack.push_async_callback(persistence.close)
        locks = PostgresAdvisoryLocks(cfg.langgraph_postgresql)
        stack.push_async_callback(locks.close)
        assistant_postgres = PostgresClientManager(cfg.langgraph_postgresql)
        stack.push_async_callback(assistant_postgres.close)
        sandbox = DockerSandboxManager(
            cfg.sandbox,
            readonly_mounts=(analyst_skill_mount(),),
        )
        stack.push_async_callback(sandbox.disconnect)
        agents = AgentManager(
            persistence, ConversationTombstoneStore(assistant_postgres), locks
        )
        stack.push_async_callback(agents.close)
        service = ConversationLifecycleService(
            assistant_postgres,
            locks,
            agents,
            sandbox,
            cfg.lifecycle,
        )
        await persistence.init()
        await locks.init()
        await sandbox.init(start_cleanup=False)
        yield ConversationLifecycleResources(service, sandbox)
