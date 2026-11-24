"""Request-scoped DataAgent observations, exported with the installed Trace SDK wheel."""

import asyncio
import hashlib
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from uuid import UUID

from fastapi import Request
from pydantic import BaseModel
from trace_sdk.client import TraceSession

from dataagent.app.dependencies import AnalysisUserDep
from dataagent.engines.common.observability import evaluation

START = {
    "chain": "AGENT_STEP",
    "model_start": "MODEL_CALL",
    "tool_start": "TOOL_CALL",
    "sql_start": "TOOL_CALL",
}
END = {
    "chain_end",
    "chain_error",
    "model_end",
    "model_error",
    "tool_end",
    "tool_error",
    "sql_end",
}
SPAN_LIMIT = 2048


def label(value, fallback):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[\w.:-]{1,100}", value)
        else fallback
    )


def ref(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16] if value else None


class Turn:
    def __init__(self, index, capacity):
        self.capacity = capacity
        self.index = index
        self.spans = []
        self.by_run = {}
        self.partial = False
        self.lock = Lock()

    def write(self, kind, **f):
        with self.lock:
            now = datetime.now(UTC).isoformat()
            run = str(f.get("run", ""))
            if kind in END:
                span = self.by_run.get(run)
                if span:
                    span["ended_at"] = now
                    failed = kind.endswith("error") or f.get("outcome") == "failed"
                    span["status"] = "FAILED" if failed else "SUCCEEDED"
                    for k in ("input_tokens", "output_tokens"):
                        v = (f.get("usage") or {}).get(k)
                        if type(v) is int and v >= 0:
                            span["attributes"][k] = v
                    if span["name"] == "publish_artifact" and f.get("delivery") in {
                        "verified",
                        "needs_revision",
                    }:
                        span["attributes"]["delivery_status"] = f["delivery"]
                return
            if len(self.spans) >= self.capacity:
                self.partial = True
                return
            event_type = START.get(kind)
            attrs = {"turn": self.index}
            if event_type:
                name = label(
                    f.get("name") or f.get("model"),
                    {
                        "sql_start": "execute_sql",
                        "chain": "chain",
                        "model_start": "model",
                        "tool_start": "tool",
                    }[kind],
                )
                if kind == "sql_start":
                    name = "doris.query"
                for attr in ("role", "model", "node"):
                    if f.get(attr):
                        attrs[attr] = label(f[attr], "unknown")
                for key in ("session", "delegation"):
                    if f.get(key):
                        attrs[key + "_ref"] = ref(f[key])
                if isinstance(f.get("step"), int):
                    attrs["step"] = f["step"]
            elif kind in {"recall_context", "recall_channel"}:
                event_type, name = "RETRIEVAL", kind
                attrs["recall_ref"] = ref(f.get("recall_id"))
                if kind == "recall_context":
                    attrs["column_count"] = len(f.get("columns") or ())
                    attrs["metric_count"] = len(f.get("metrics") or ())
            elif kind == "model_http":
                event_type, name = "CUSTOM", kind
                attrs["outcome"] = label(f.get("outcome"), "unknown")
            else:
                return
            span = {
                "event_type": event_type,
                "name": name,
                "started_at": now,
                "attributes": attrs,
                "status": "STARTED" if kind in START else "SUCCEEDED",
                "source": run,
                "parent": str(f.get("parent") or ""),
            }
            self.spans.append(span)
            if run:
                self.by_run[run] = span


@dataclass
class Attempt:
    turns: dict = field(default_factory=dict)
    partial: bool = False
    expiry: asyncio.TimerHandle | None = None


class Finish(BaseModel):
    conversation_id: UUID
    complete: bool


class EvaluationPlatformTraceSink:
    def __init__(self, base_url, username):
        self.base_url = base_url
        self.username = username
        self.attempts = {}

    def binding(self, headers, user, cid):
        if user.username != self.username or user.is_admin:
            return None
        try:
            tid = UUID(headers["x-trace-id"])
            attempt, secret = headers["x-trace-write"].split(".")
            if not re.fullmatch(r"[\w-]{43}", secret):
                return None
            turn = int(headers["x-eval-turn"])
            if 1 <= turn <= 32:
                return (user.id, cid, tid, UUID(attempt)), turn
        except (KeyError, ValueError):
            pass
        return None

    @contextmanager
    def capture(self, headers, user, cid):
        binding = self.binding(headers, user, cid)
        if not binding:
            yield
            return
        key, index = binding
        attempt = self.attempts.get(key)
        if attempt is None:
            if len(self.attempts) >= 32:
                yield
                return
            attempt = self.attempts[key] = Attempt()
        if attempt.expiry:
            attempt.expiry.cancel()
        attempt.expiry = asyncio.get_running_loop().call_later(
            3600, self.attempts.pop, key, None
        )
        remaining = SPAN_LIMIT - sum(len(t.spans) for t in attempt.turns.values())
        if index in attempt.turns or remaining <= 0:
            attempt.partial = True
            yield
            return
        turn = attempt.turns[index] = Turn(index, remaining)
        local = evaluation.observer.get()
        sinks = (turn,) if local is None else (*local.sinks, turn)
        token = evaluation.observer.set(evaluation.Observer(*sinks))
        try:
            yield
        finally:
            evaluation.observer.reset(token)

    async def finish(self, body: Finish, request: Request, user: AnalysisUserDep):
        binding = self.binding(request.headers, user, body.conversation_id)
        if not binding:
            return {"status": "MISSING"}
        key, last = binding
        attempt = self.attempts.pop(key, None)
        if not attempt:
            return {"status": "MISSING"}
        attempt.expiry.cancel()
        session = TraceSession.from_headers(
            self.base_url, request.headers, timeout=5, capacity=128
        )
        if session is None:
            return {"status": "MISSING"}
        complete = (
            body.complete
            and not attempt.partial
            and set(attempt.turns) == set(range(1, last + 1))
        )
        try:
            count = 0
            for index, turn in sorted(attempt.turns.items()):
                keys = {}
                complete &= not turn.partial and bool(turn.spans)
                for span in turn.spans:
                    fields = {
                        k: v for k, v in span.items() if k not in {"source", "parent"}
                    }
                    if span["parent"] in keys:
                        fields["parent_event_key"] = keys[span["parent"]]
                    if fields["status"] == "STARTED":
                        complete = False
                    event = session.add(**fields)
                    if span["source"] and event:
                        keys[span["source"]] = event
                    count += 1
                    if count % 60 == 0:
                        await session.flush()
            if body.complete:
                session.add("FINAL_OUTPUT", "planner.final", attributes={"turns": last})
        except Exception:  # noqa: BLE001 - seal an interrupted export as partial
            complete = False
        finally:
            await session.finish(complete=complete)
        return {"status": session.status}
