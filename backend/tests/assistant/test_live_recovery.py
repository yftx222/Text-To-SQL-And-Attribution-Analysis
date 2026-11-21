"""H09：在独立 API 进程注入中断，通过正式 resume 接口恢复。仅显式启用。"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

SQL = """SELECT COUNT(DISTINCT order_id) AS orders
FROM ecommerce.dwd_trade_order_detail_di
WHERE order_create_time >= '2026-09-01' AND order_create_time < '2026-10-01'"""
CHECK = "按下单时间统计九月去重订单"


async def scripted_stream(self, messages, **kwargs):
    """固定模型决策，所有工具、检查点、SQL 和交付都走应用自身实现。"""
    from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
    from langchain_core.outputs import ChatGenerationChunk

    tools = {t.get("function", t).get("name") for t in kwargs["tools"]}
    turn = messages[
        max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage)) + 1 :
    ]
    last = next((m for m in reversed(turn) if isinstance(m, ToolMessage)), None)
    calls = []
    content = ""
    if last is None:
        if "execute_sql" in tools:
            name, args = "execute_sql", {"sql": SQL, "purpose": CHECK}
        else:
            name, args = (
                "delegation",
                {
                    "analysis_id": "orders",
                    "agent_type": "explorer",
                    "session_id": "source",
                    "message": CHECK,
                    "acceptance": [CHECK],
                },
            )
    else:
        data = json.loads(last.content)
        if last.name == "execute_sql":
            assert data["status"] == "success", data
            name, args = (
                "submit_task_result",
                {
                    "verdict": "passed",
                    "checks": {CHECK: str(data["sample"])},
                    "evidence": [data["path"]],
                },
            )
        elif last.name == "delegation":
            assert data["status"] == "completed", data
            name, args = (
                "publish_artifact",
                {
                    "analysis_id": "orders",
                    "target": {
                        "agent_type": "explorer",
                        "session_id": "source",
                        "delegation_id": data["delegation_id"],
                        "path": data["query_outputs"][0],
                    },
                },
            )
        else:
            assert last.name in {"publish_artifact", "submit_task_result"}, last.name
            if last.name == "publish_artifact":
                assert data["status"] == "verified", data
            content = "九月订单统计已完成。"
    if not content:
        calls = [{"name": name, "args": args, "id": uuid4().hex}]
    yield ChatGenerationChunk(
        message=AIMessageChunk(content=content, tool_calls=calls, id=uuid4().hex)
    )


async def serve(folder, phase, model_stream=scripted_stream):
    import uvicorn

    from dataagent.app.app import app
    from dataagent.engines.agent.specialist.invoker import DelegationExecutor
    from dataagent.engines.common.llm import DataAgentDeepSeekResponses
    from dataagent.engines.query import QueryExecutionService
    from dataagent.engines.query.execution.executor import AnalysisQueryService

    execute = QueryExecutionService.execute
    materialize = AnalysisQueryService.execute
    save = DelegationExecutor._save_delegation_record

    async def query(self, *args, **kwargs):
        if phase == "before_tool":
            os._exit(73)
        result = await execute(self, *args, **kwargs)
        if phase == "after_artifact":
            os._exit(73)
        return result

    async def artifact(self, *args, **kwargs):
        result = await materialize(self, *args, **kwargs)
        with (folder / "versions").open("a", encoding="utf8") as f:
            f.write(json.dumps({"path": result.path, "sha256": result.sha256}) + "\n")
        if phase == "before_audit":
            os._exit(73)
        return result

    async def terminal(agent, config, delegation_id, status, request, result=None):
        await save(agent, config, delegation_id, status, request, result)
        if phase == "after_terminal" and status == "completed":
            os._exit(73)

    QueryExecutionService.execute = query
    AnalysisQueryService.execute = artifact
    DelegationExecutor._save_delegation_record = staticmethod(terminal)
    DataAgentDeepSeekResponses._astream = model_stream
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("API 启动失败")
        await asyncio.sleep(0.1)
    (folder / phase).write_text(str(server.servers[0].sockets[0].getsockname()[1]))
    while not task.done() and not (folder / "stop").exists():
        await asyncio.sleep(0.2)
    server.should_exit = True
    await task


@contextmanager
def api(folder, phase, module="tests.assistant.test_live_recovery"):
    from dotenv import dotenv_values

    log = folder / (phase + ".log")
    with log.open("wb") as f:
        child = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                module,
                str(folder),
                phase,
            ],
            stdout=f,
            stderr=subprocess.STDOUT,
        )
        try:
            ready = folder / phase
            start = time.monotonic()
            while not ready.exists():
                assert child.poll() is None and time.monotonic() - start < 90, (
                    log.read_text(encoding="utf8", errors="replace")[-4000:]
                )
                time.sleep(0.2)
            with httpx.Client(
                base_url="http://127.0.0.1:" + ready.read_text(),
                timeout=100,
                trust_env=False,
            ) as c:
                env = dotenv_values("conf/.env")
                response = c.post(
                    "/api/v1/auth/login",
                    json={
                        "identifier": env["ADMIN_USERNAME"],
                        "password": env["ADMIN_PASSWORD"],
                    },
                )
                response.raise_for_status()
                c.headers["Authorization"] = "Bearer " + response.json()["access_token"]
                yield c, child
        finally:
            if child.poll() is None:
                (folder / "stop").touch()
                child.wait(timeout=100)


async def database_evidence(cid):
    from sqlalchemy import select

    from dataagent.engines.assistant.models.delivery import DeliveryReceipt
    from dataagent.engines.contract.config import cfg
    from dataagent.engines.infra.postgres_client_manager import PostgresClientManager
    from dataagent.engines.query.models.execution import QueryExecution

    async def fetch(config, model, col):
        db = PostgresClientManager(config)
        try:
            async with db.session() as session:
                return list(
                    (
                        await session.scalars(
                            select(col).where(model.conversation_id == UUID(cid))
                        )
                    ).all()
                )
        finally:
            await db.close()

    return (
        await fetch(cfg.meta_postgresql, QueryExecution, QueryExecution.result_summary),
        await fetch(cfg.langgraph_postgresql, DeliveryReceipt, DeliveryReceipt.receipt),
    )


@pytest.mark.skipif(
    os.getenv("DATAAGENT_TEST_LIVE_RECOVERY") != "1", reason="需要本地开发数据服务"
)
@pytest.mark.parametrize(
    "phase,count",
    [
        ("before_tool", 1),
        ("before_audit", 2),
        ("after_artifact", 2),
        ("after_terminal", 1),
    ],
)
def test_resume_to_single_delivery(tmp_path, phase, count):
    from dataagent.engines.common.async_runtime import run_async

    with api(tmp_path, phase) as (c, child):
        new = c.post(
            "/api/v1/chat/create",
            json={"is_draft": True, "initial_message": "H09 " + phase},
        )
        new.raise_for_status()
        cid = new.json()["conversation_id"]
        with pytest.raises(httpx.TransportError):
            c.post(
                "/api/v1/chat/stream",
                json={
                    "conversation_id": cid,
                    "message": {
                        "parts": [{"type": "text", "text": "导出九月去重订单数 CSV"}],
                        "attachments": [],
                    },
                },
            )
        assert child.wait(timeout=10) == 73
        snapshot = tmp_path / "versions"
        before = snapshot.read_text().splitlines() if snapshot.exists() else []
        assert len(before) == (0 if phase == "before_tool" else 1)
    with api(tmp_path, "resume") as (c, _):
        resumed = c.post(f"/api/v1/chat/{cid}/resume")
        resumed.raise_for_status()
        events = [
            json.loads(s[5:])
            for s in resumed.text.splitlines()
            if s.startswith("data:")
        ]
        assert events[-1]["type"] == "done" and not any(
            e["type"] == "error" for e in events
        ), str(events)[-2000:]
        hist = c.get("/api/v1/chat/ls/" + cid).json()["messages"]
        receipts = [
            json.loads(p["content"])
            for m in hist
            for p in m["parts"]
            if p["type"] == "tool_result" and p["name"] == "publish_artifact"
        ]
        assert len(receipts) == 1 and receipts[0]["status"] == "verified", str(hist)[
            -1800:
        ]
        versions = [
            json.loads(s) for s in (tmp_path / "versions").read_text().splitlines()
        ]
        paths = [v["path"] for v in versions]
        assert len(paths) == len(set(paths)) == count
        audits, stored = run_async(database_evidence(cid))
        audited_paths = paths[1:] if phase == "before_audit" else paths
        assert sorted(a["path"] for a in audits) == sorted(audited_paths)
        assert stored == receipts and stored[0]["target"]["path"] == paths[-1]
        streamed = [
            a["f_path"]
            for e in events
            if e["type"] == "message"
            for a in e["message"].get("attachments") or []
            if a.get("delivery_status") == "verified"
        ]
        assert streamed == [stored[0]["f_path"]]
        assert (tmp_path / "versions").read_text().splitlines()[: len(before)] == before
        for v in versions:
            fpath = "evidence/" + v["path"].rsplit("/", 1)[1]
            file = c.get(
                "/api/v1/chat/attachment/get",
                params={"conversation_id": cid, "f_path": fpath},
            )
            file.raise_for_status()
            assert hashlib.sha256(file.content).hexdigest() == v["sha256"]
            assert file.content.decode("utf-8-sig").splitlines() == ["orders", "206"]
        print(
            phase,
            cid,
            "queries",
            len(paths),
            "verified_receipt",
            stored[0]["receipt_id"],
        )


if __name__ == "__main__":
    from dataagent.engines.common.async_runtime import run_async

    run_async(serve(Path(sys.argv[1]), sys.argv[2]))
