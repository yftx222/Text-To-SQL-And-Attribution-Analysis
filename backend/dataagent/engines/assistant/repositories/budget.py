"""事务内预留专家执行时间；终态返还未用秒数，恢复复用原预留。"""

from contextlib import asynccontextmanager
from copy import deepcopy

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from dataagent.engines.assistant.models.budget import AnalysisBudget


class BudgetExceeded(ValueError):
    """当前分析已耗尽配置额度。"""


class AnalysisBudgetStore:
    def __init__(self, postgres, limits):
        self.postgres = postgres
        self.limits = limits

    @asynccontextmanager
    async def _locked(self, conversation, analysis):
        async with self.postgres.session() as session, session.begin():
            await session.execute(
                insert(AnalysisBudget)
                .values(
                    conversation_id=conversation,
                    analysis_id=analysis,
                    ledger={"attempts": {}, "issues": {}},
                )
                .on_conflict_do_nothing()
            )
            row = (
                await session.scalars(
                    select(AnalysisBudget)
                    .where(
                        AnalysisBudget.conversation_id == conversation,
                        AnalysisBudget.analysis_id == analysis,
                    )
                    .with_for_update()
                )
            ).one()
            ledger = deepcopy(row.ledger)
            now = (await session.scalar(select(func.clock_timestamp()))).timestamp()
            yield ledger, now
            row.ledger = ledger

    async def reserve(self, conversation, request, ident):
        async with self._locked(conversation, request.analysis_id) as (ledger, now):
            return self.reserve_locked(ledger, now, request, ident)

    def reserve_locked(self, ledger, now, request, ident):
        attempts = ledger["attempts"]
        slot = f"{request.agent_type}/{request.session_id}"
        previous = attempts.get(ident)
        charged = 0
        if previous:
            if previous["slot"] != slot or previous.get("terminal"):
                raise BudgetExceeded("本次委派已经结算")
            if not previous["closed"]:
                remaining = previous["deadline"] - now
                if remaining <= 0:
                    raise BudgetExceeded("本次委派的预留时间已耗尽")
                return remaining
            charged = previous["seconds"]
        elif len(attempts) >= self.limits.max_delegations:
            raise BudgetExceeded("分析累计委派次数已达上限")
        remaining = self.limits.analysis_seconds - sum(
            item["seconds"] for item in attempts.values()
        )
        seconds = min(remaining, self.limits.delegation_seconds - charged)
        if seconds <= 0:
            raise BudgetExceeded("分析或本次委派的可用执行时间已耗尽")
        issue = ledger["issues"].get(slot)
        if issue and previous is None:
            if issue["repairs"] >= self.limits.max_repairs:
                raise BudgetExceeded("同一问题的修补次数已达上限")
            issue["repairs"] += 1
        attempts[ident] = {
            "slot": slot,
            "start": now,
            "deadline": now + seconds,
            "seconds": charged + seconds,
            "charged": charged,
            "closed": False,
        }
        return seconds

    async def finish(self, conversation, request, ident, result):
        async with self._locked(conversation, request.analysis_id) as (ledger, now):
            self.finish_locked(ledger, now, request, ident, result)

    @staticmethod
    def finish_locked(ledger, now, request, ident, result):
        attempt = ledger["attempts"].get(ident)
        if not attempt or attempt["closed"]:
            return
        attempt["seconds"] = min(
            attempt["seconds"], attempt["charged"] + max(0, now - attempt["start"])
        )
        attempt["terminal"] = result is not None
        attempt["closed"] = True
        outcome = result.outcome if result else None
        if outcome is None:
            return
        reviewed = request.agent_type == "reviewer" and request.inputs
        target = request.inputs[0] if reviewed else request
        slot = f"{target.agent_type}/{target.session_id}"
        if reviewed:
            newer = any(
                item["slot"] == slot
                and item["start"]
                > ledger["attempts"].get(target.delegation_id, {}).get("start", 0)
                for item in ledger["attempts"].values()
            )
            if newer:
                return
        issues = ledger["issues"]
        if outcome.verdict != "passed":
            issue = issues.setdefault(slot, {"repairs": 0, "requires_review": False})
            issue["requires_review"] |= bool(reviewed)
        elif reviewed or not issues.get(slot, {}).get("requires_review"):
            issues.pop(slot, None)
