"""用独立复算、两个报告版本和真实文件字节验证正式交付边界。"""

import hashlib
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from uuid import uuid4

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage

from dataagent.engines.agent.planner.tools.delivery import (
    DeliveryGate,
    create_delivery_tool,
)
from dataagent.engines.agent.specialist.outcome import submit_task_result, task_outputs
from dataagent.engines.assistant.contracts.sessions import DelegationRequest, TaskInput
from dataagent.engines.assistant.messages.projection import langchain_message_to_schema


class Scenario:
    def __init__(self):
        self.cid = uuid4()
        self.states = {}
        self.files = {}
        self.query = SimpleNamespace(delivery_source_issue=AsyncMock(return_value=None))
        self.gate = DeliveryGate(1, self.cid, self, self, self.query, self)
        self.tool = create_delivery_tool(self.gate)

    async def save(self, ident, cid, receipt):
        return receipt

    async def list_namespaces(self, analysis):
        return [f"subagents/{a}/{r}/{s}" for a, r, s in self.states if a == analysis]

    async def read_state(self, key):
        return SimpleNamespace(
            values=self.states[key.analysis_id, key.agent_type, key.session_id]
        )

    async def download_file(self, user_id, cid, path):
        content = self.files[path]
        assert user_id == 1 and cid == self.cid
        assert hashlib.sha256(content).hexdigest() in path
        return content

    def task(self, role, session, content, inputs=(), verdict="passed", reason=""):
        ident = uuid4().hex
        suffix = "csv" if role == "explorer" else "html"
        name = f"{uuid4()}_{hashlib.sha256(content).hexdigest()}.{suffix}"
        path = f"/data/.evidence-{self.cid}/{name}"
        self.files["evidence/" + name] = content
        ref = TaskInput(
            agent_type=role, session_id=session, delegation_id=ident, path=path
        )
        task = DelegationRequest(
            analysis_id="orders",
            agent_type=role,
            session_id=session,
            message="9月订单",
            inputs=list(inputs),
            acceptance=["按订单ID去重"],
        )
        evidence = [path] + ([inputs[0].path] if inputs and role == "reviewer" else [])
        decision = submit_task_result.invoke(
            {
                "verdict": verdict,
                "checks": {"按订单ID去重": content.decode()},
                "evidence": evidence,
                "repair_target": reason,
            }
        )
        messages = [
            ToolMessage(
                name="execute_sql" if role == "explorer" else "freeze_artifact",
                content=json.dumps({"status": "success", "path": path}),
                tool_call_id="a",
            ),
            ToolMessage(
                name="submit_task_result",
                content=json.dumps(decision),
                tool_call_id="b",
            ),
        ]
        outcome, outputs, query_outputs = task_outputs(messages)
        state = self.states.setdefault(
            ("orders", role, session), {"delegation_records": {}}
        )
        state["current_delegation_id"] = ident
        state["delegation_records"][ident] = {
            "delegation_id": ident,
            "status": "completed",
            "task": task.model_dump(),
            "result": {
                "status": "completed",
                "analysis_id": "orders",
                "agent_type": role,
                "session_id": session,
                "delegation_id": ident,
                "content": "完成",
                "outcome": outcome.model_dump(),
                "outputs": outputs,
                "query_outputs": query_outputs,
            },
        }
        return ref

    async def publish(self, target, analysis="orders"):
        runtime = ToolRuntime(
            state={"messages": []},
            context=None,
            config={},
            stream_writer=lambda _: None,
            tool_call_id="publish",
            store=None,
        )
        return await self.tool.ainvoke(
            {"runtime": runtime, "analysis_id": analysis, "target": target.model_dump()}
        )


class DeliveryTests(IsolatedAsyncioTestCase):
    async def test_report_sources_checked_before_analysis(self):
        case = Scenario()
        source = case.task("explorer", "source", b"206")
        request = DelegationRequest(
            analysis_id="orders",
            agent_type="analyst",
            session_id="report",
            message="生成简报",
            inputs=[source],
        )
        assert await case.gate.check_report_sources(request) is None
        record = case.states["orders", "explorer", "source"]["delegation_records"][
            source.delegation_id
        ]
        record["result"]["query_outputs"] = ["/data/another.csv"]
        issue = await case.gate.check_report_sources(request)
        assert issue["invalid_input"]["path"] == source.path
        assert issue["status"] == "needs_revision"

    async def test_correct_review_evidence_on_same_report(self):
        case = Scenario()
        source = case.task("explorer", "source", b"206")
        report = case.task("analyst", "report", b"206 orders", [source])
        original = case.task("reviewer", "audit", b"206 verified", [report, source])
        records = case.states["orders", "reviewer", "audit"]["delegation_records"]
        first = records[original.delegation_id]["result"]
        first["outcome"]["evidence"] = [original.path, "报告：" + report.path]
        assert "复核目标" in (await case.publish(report))["reason"]

        correction = case.task("reviewer", "audit", b"recheck 206", [report, source])
        corrected = records[correction.delegation_id]["result"]
        corrected["outcome"]["evidence"] = [report.path]
        assert "本轮生成的固定核对证据" in (await case.publish(report))["reason"]
        corrected["outcome"]["evidence"].append(correction.path)
        receipt = await case.publish(report)
        assert receipt["status"] == "verified"
        assert receipt["reviews"] == [correction.delegation_id]
        assert await case.publish(report) == receipt
        assert await case.download_file(1, case.cid, receipt["f_path"]) == b"206 orders"
        assert original.delegation_id in records

        newer = case.task("analyst", "report", b"revised report", [source])
        assert "缺少独立复核" in (await case.publish(newer))["reason"]

    async def test_review_correction_keeps_other_requirements_and_objections(self):
        case = Scenario()
        source = case.task("explorer", "source", b"206")
        report = case.task("analyst", "report", b"206 orders", [source])
        old = case.task("reviewer", "audit", b"checked", [report, source])
        records = case.states["orders", "reviewer", "audit"]["delegation_records"]
        records[old.delegation_id]["task"]["acceptance"].append("核对截止日期")
        case.task("reviewer", "other", b"independent", [report, source])
        case.task("reviewer", "audit", b"partial", [report, source])
        assert "核对截止日期" in (await case.publish(report))["reason"]
        narrow = case.task("reviewer", "audit", b"missing source", [report])
        records[narrow.delegation_id]["task"]["acceptance"].append("核对截止日期")
        records[narrow.delegation_id]["result"]["outcome"]["checks"]["核对截止日期"] = (
            "已核对"
        )
        assert "核对截止日期" in (await case.publish(report))["reason"]

        complete = case.task("reviewer", "audit", b"full evidence", [report, source])
        records[complete.delegation_id]["task"]["acceptance"].append("核对截止日期")
        records[complete.delegation_id]["result"]["outcome"]["checks"][
            "核对截止日期"
        ] = "已核对"
        assert len((await case.publish(report))["reviews"]) == 2
        case.task(
            "reviewer", "audit", b"error", [report], "needs_revision", "报告数字错误"
        )
        case.task("reviewer", "audit", b"checked again", [report])
        assert "报告数字错误" in (await case.publish(report))["reason"]

    async def test_repair_names_missing_check_and_existing_source(self):
        case = Scenario()
        source = case.task("explorer", "sales", b"206")
        record = case.states["orders", "explorer", "sales"]["delegation_records"][
            source.delegation_id
        ]
        record["task"]["acceptance"].append("限定九月")
        rejected = await case.publish(source)
        assert rejected["status"] == "needs_revision"
        assert "限定九月" in rejected["reason"]
        record["result"]["outcome"]["checks"]["限定九月"] = "按下单时间过滤"
        wrong = source.model_copy(
            update={"path": case.task("explorer", "other", b"other").path}
        )
        assert source.path in (await case.publish(wrong))["reason"]
        assert (await case.publish(source))["status"] == "verified"

    async def test_copied_csv_points_to_original_without_rerunning_query(self):
        case = Scenario()
        original = case.task("explorer", "sales", b"206")
        copy = original.model_copy(
            update={
                "path": original.path.replace(
                    original.path.split("/")[-1][:36], str(uuid4())
                )
            }
        )
        case.files["evidence/" + copy.path.split("/")[-1]] = b"206"
        result = case.states["orders", "explorer", "sales"]["delegation_records"][
            original.delegation_id
        ]["result"]
        result["outputs"].append(copy.path)
        result["outcome"]["evidence"].append(copy.path)
        rejection = await case.publish(copy)
        assert rejection["status"] == "needs_revision"
        assert original.path in rejection["reason"]
        assert (await case.publish(original))["status"] == "verified"

    async def test_completed_delegation_retains_repair_verdict_and_inputs(self):
        from tests.assistant.test_dynamic_subagents import (
            _CONVERSATION_ID,
            _FakeAgent,
            _request,
            _service,
            build_planner_config,
        )

        verdict = submit_task_result.invoke(
            {
                "verdict": "needs_revision",
                "checks": {"复算": "数量口径错误"},
                "evidence": [],
                "repair_target": "按订单 ID 去重",
            }
        )
        graph = _FakeAgent(
            stream_messages=[
                ToolMessage(
                    name="submit_task_result",
                    tool_call_id="check",
                    content=json.dumps(verdict),
                ),
                AIMessage(content="检查结束，需要修补上游报告"),
            ]
        )
        api = _service(graph)
        ref = TaskInput(
            agent_type="analyst",
            session_id="report",
            delegation_id="draft",
            path="/data/sample",
        )
        request = _request("audit", agent_type="reviewer").model_copy(
            update={"inputs": [ref], "acceptance": ["复算"]}
        )
        result = await api.executor.execute_delegation(
            request, build_planner_config(12, _CONVERSATION_ID), delegation_id="review1"
        )
        assert (
            result.status == "completed" and result.outcome.verdict == "needs_revision"
        )
        record = graph.checkpoints["subagents/sales-decline/reviewer/audit"][
            "channel_values"
        ]["delegation_records"]["review1"]
        assert record["task"]["inputs"] == [ref.model_dump()]
        assert record["result"]["outcome"]["repair_target"] == "按订单 ID 去重"

    async def test_september_report_repair_and_replay(self):
        case = Scenario()
        ids = list(range(206)) + list(range(95))
        source = case.task("explorer", "source", "\n".join(map(str, ids)).encode())
        wrong = case.task("analyst", "report", str(len(ids)).encode(), [source])
        assert (await case.publish(wrong))["status"] == "needs_revision"
        distinct = len(set(ids))
        case.task(
            "reviewer",
            "audit",
            str(distinct).encode(),
            [wrong, source],
            "needs_revision",
            "301 是明细行数，按订单 ID 去重为 206",
        )
        assert "301" in (await case.publish(wrong))["reason"]
        repaired = case.task("analyst", "report", str(distinct).encode(), [source])
        assert "输入已过期" in (await case.publish(wrong))["reason"]
        assert "缺少独立复核" in (await case.publish(repaired))["reason"]
        case.task("reviewer", "audit", b"206 verified", [repaired, source])
        receipt = await case.publish(repaired)
        assert receipt["status"] == "verified"
        assert await case.publish(repaired) == receipt
        assert (await case.publish(repaired, "other"))["status"] != "verified"
        case.files[receipt["f_path"]] = b"changed bytes"
        assert (await case.publish(repaired))["status"] == "blocked"

    async def test_dependency_refresh_audit_access_and_unrelated_work(self):
        case = Scenario()
        source = case.task("explorer", "source", b"base")
        unrelated = case.task("explorer", "other", b"other")
        report = case.task("analyst", "report", b"ok", [source])
        case.task("reviewer", "audit", b"checked", [report, source])
        assert (await case.publish(report))["status"] == "verified"
        case.query.delivery_source_issue.return_value = "缺少已提交的成功查询审计"
        assert "审计" in (await case.publish(report))["reason"]
        case.query.delivery_source_issue.return_value = None
        case.task("explorer", "source", b"new")
        assert "过期" in (await case.publish(report))["reason"]
        assert (await case.publish(unrelated))["status"] == "verified"
        blocked = case.task(
            "explorer", "missing", b"missing", verdict="blocked", reason="缺少日期字段"
        )
        assert "缺少日期字段" in (await case.publish(blocked))["reason"]

    async def test_only_successful_publication_receipt_labels_attachment_verified(self):
        case = Scenario()
        ref = case.task("explorer", "sales", b"206")
        response = await case.publish(ref)
        message = ToolMessage(
            name="publish_artifact", tool_call_id="pub1", content=json.dumps(response)
        )
        public = await langchain_message_to_schema(message, case, 1, case.cid)
        assert public.attachments[0].delivery_status == "verified"
        case.resolve_artifacts = AsyncMock(
            return_value={ref.path: SimpleNamespace(relative_path=response["f_path"])}
        )
        draft = await langchain_message_to_schema(
            AIMessage(content=f"[[DATAAGENT_ARTIFACT:{ref.path}]]"), case, 1, case.cid
        )
        assert draft.attachments[0].delivery_status == "draft"


def test_session_list_marks_only_transitive_dependents_stale():
    from dataagent.engines.assistant.contracts.sessions import SessionSummary
    from dataagent.engines.assistant.sessions.management import _mark_stale_inputs

    def summary(role, session, version, inputs=()):
        return SessionSummary(
            analysis_id="orders",
            agent_type=role,
            session_id=session,
            delegation_id=version,
            status="completed",
            business_status="passed",
            inputs=list(inputs),
            outputs=[version],
        )

    old = TaskInput(
        agent_type="explorer", session_id="source", delegation_id="v1", path="v1"
    )
    report = TaskInput(
        agent_type="analyst",
        session_id="report",
        delegation_id="report1",
        path="report1",
    )
    sessions = [
        summary("reviewer", "audit", "review1", [report]),
        summary("analyst", "report", "report1", [old]),
        summary("explorer", "source", "v2"),
        summary("explorer", "other", "x"),
    ]
    _mark_stale_inputs(sessions)
    assert [s.business_status for s in sessions] == [
        "stale",
        "stale",
        "passed",
        "passed",
    ]


def test_postgres_replays_single_committed_receipt():
    """显式启用时，仅使用当前 PostgreSQL 连接的临时表。"""
    import asyncio
    import os
    import selectors
    import sys

    import pytest
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from dataagent.engines.assistant.repositories.delivery import DeliveryReceiptStore
    from dataagent.engines.contract.config import cfg
    from dataagent.engines.infra.postgres_client_manager import PostgresClientManager

    if not os.getenv("DATAAGENT_TEST_PG_RECEIPT"):
        pytest.skip("requires project PostgreSQL")

    async def probe():
        db = PostgresClientManager(cfg.langgraph_postgresql)
        try:
            async with asyncio.timeout(20), db.engine.connect() as conn:
                async with conn.begin():
                    await conn.execute(
                        text(
                            "CREATE TEMP TABLE delivery_receipts (id uuid PRIMARY KEY, conversation_id uuid NOT NULL, receipt json NOT NULL)"
                        )
                    )
                store = DeliveryReceiptStore(
                    SimpleNamespace(
                        session=async_sessionmaker(conn, expire_on_commit=False)
                    )
                )
                key, cid = uuid4(), uuid4()
                assert await store.save(key, cid, {"version": 1}) == {"version": 1}
                assert await store.save(key, cid, {"version": 2}) == {"version": 1}
                assert (
                    await conn.scalar(
                        text("select count(*) from pg_temp.delivery_receipts")
                    )
                    == 1
                )
        finally:
            await db.close()

    if sys.platform == "win32":
        asyncio.run(
            probe(),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    else:
        asyncio.run(probe())


def test_process_exit_after_receipt_commit(tmp_path):
    """回执已提交、工具消息尚未写入检查点时强退，恢复后只有一个发布标识。"""
    import sqlite3
    import subprocess
    import sys

    case = Scenario()
    target = case.task("explorer", "orders", b"206")
    fixture = tmp_path / "inputs.json"
    fixture.write_text(
        json.dumps(
            {
                "conversation": str(case.cid),
                "ref": target.model_dump(),
                "states": [[list(k), v] for k, v in case.states.items()],
                "files": {k: v.decode() for k, v in case.files.items()},
            }
        ),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "tests.assistant.test_delivery_gate",
        str(tmp_path),
    ]
    assert (
        subprocess.run([*command, "interrupt"], timeout=90, check=False).returncode
        == 73
    )
    subprocess.run([*command, "resume"], timeout=90, check=True)
    with sqlite3.connect(tmp_path / "receipts.db") as db:
        records = db.execute("SELECT receipt FROM receipts").fetchall()
        assert len(records) == 1
        assert json.loads(records[0][0])["target"] == target.model_dump()


if __name__ == "__main__":
    import asyncio
    import os
    import sqlite3
    import sys
    from pathlib import Path
    from uuid import UUID

    async def run_worker():
        folder = Path(sys.argv[1])
        fixture = json.loads((folder / "inputs.json").read_text(encoding="utf-8"))
        case = Scenario()
        case.cid = case.gate.conversation_id = UUID(fixture["conversation"])
        case.states = {tuple(k): v for k, v in fixture["states"]}
        case.files = {k: v.encode() for k, v in fixture["files"].items()}

        async def save(ident, cid, receipt):
            with sqlite3.connect(folder / "receipts.db") as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, receipt TEXT)"
                )
                db.execute(
                    "INSERT INTO receipts VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
                    (str(ident), json.dumps(receipt)),
                )
            if sys.argv[2] == "interrupt":
                os._exit(73)
            return receipt

        case.save = save
        result = await case.publish(TaskInput.model_validate(fixture["ref"]))
        assert result["status"] == "verified", result

    asyncio.run(run_worker())
