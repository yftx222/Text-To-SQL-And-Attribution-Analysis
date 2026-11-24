"""Project visible conversation history and recorded Planner tool receipts."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from aiops_quality_bridge import Reply, install
from dataagent.app.dependencies import CurrentUserDep
from dataagent.app.dependencies.assistant import ConversationPGRepoDep
from dataagent.app.dependencies.chat import ConversationServiceDep
from dataagent.engines.assistant.models.conversation import Conversation
from dataagent.app.services.conversations.service import ConversationService
from dataagent.engines.assistant.repositories.conversation import ConversationPGRepo


def mount(app, committed_loader=None):
    async def committed(cid, _message_id):
        resources = app.state.resources
        async with resources.assistant.session() as session:
            repo = ConversationPGRepo(session)
            service = ConversationService(
                repo, resources.runs, resources.agent_state, resources.sandbox, resources.conversations
            )
            return await source(repo, service)(cid, None, True)

    install(app, "dataagent", source, owner, committed_loader or committed)


def owner(user: CurrentUserDep):
    return user.id


def project(history, *, require_evidence=True):
    rows = []
    pending = {}
    calls = {}
    for m in history:
        if m.role == "user":
            pending.clear()
            calls.clear()
        if m.role == "system":
            continue
        for part in m.parts:
            if part.type == "tool_call" and m.agent_run_id and m.created_at:
                attributes = {}
                if part.name == "deliver_query_values" and isinstance(part.args.get("columns"), list):
                    columns = [col for col in part.args["columns"] if isinstance(col, dict)]
                    attributes = dict(
                        selected_column_count=len(columns),
                        selected_percent_unit=any(col.get("unit") == "%" for col in columns),
                        selected_device_count_unit=any(col.get("unit") == "台" for col in columns),
                    )
                step = ["TOOL_CALL", part.name, "STARTED", m.created_at, attributes]
                pending.setdefault(m.agent_run_id, []).append(step)
                calls[part.tool_call_id] = step
            elif part.type == "tool_result" and part.tool_call_id in calls:
                calls[part.tool_call_id][2] = {"success": "SUCCEEDED", "error": "FAILED"}.get(
                    part.status, "UNKNOWN"
                )
        if m.role not in {"user", "assistant"}:
            continue
        text = "\n".join(p.text for p in m.parts if p.type == "text")
        if not text and m.role == "assistant":
            continue
        if not m.message_id or require_evidence and (not m.created_at or m.created_at.tzinfo is None):
            if not require_evidence:
                continue
            raise HTTPException(409, "Conversation message is missing its identity or timestamp")
        row = Reply(m.message_id, m.role, text or "[非文本消息]", m.created_at, m.agent_run_id)
        if m.role == "assistant" and row.run and row.at:
            for step in pending.get(row.run, []):
                row.event(*step)
            row.event("FINAL_OUTPUT", "planner.reply", "SUCCEEDED", row.at)
        rows.append(row)
    return rows


def source(repository: ConversationPGRepoDep, conversations: ConversationServiceDep):
    async def load(cid, user, steps):
        try:
            key = UUID(cid)
        except ValueError:
            raise HTTPException(404, "Conversation not found") from None
        query = select(Conversation.user_id).where(
            Conversation.id == key, Conversation.deletion_requested_at.is_(None)
        )
        if user is not None:
            query = query.where(Conversation.user_id == user)
        owner_id = await repository.session.scalar(query)
        if owner_id is None:
            raise HTTPException(404, "Conversation not found")
        history = await conversations.messages(owner_id, key)
        return project(history.messages, require_evidence=user is None)

    return load
