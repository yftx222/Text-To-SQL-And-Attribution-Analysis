"""H06：固定模型决策，在真实开发服务上验收同版本复核、回执和下载。"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk

from tests.assistant.test_live_recovery import (
    CHECK,
    api,
    database_evidence,
    scripted_stream,
    serve,
)

REPORT = "<html><body>September distinct orders: 206</body></html>"


def planner_call(results):
    delegates = [data for name, data in results if name == "delegation"]
    publications = [data for name, data in results if name == "publish_artifact"]
    for delegate in delegates:
        assert delegate["status"] == "completed" and delegate["outputs"], delegate
    refs = {
        d["agent_type"]: {
            key: d[key] for key in ("agent_type", "session_id", "delegation_id")
        }
        | {"path": d["outputs"][0]}
        for d in delegates
    }
    if not delegates:
        role, message, inputs = "explorer", "取数", []
    elif len(delegates) == 1:
        role, message, inputs = "analyst", "写报告", [refs["explorer"]]
    elif len(delegates) == 2 or (len(delegates) == 3 and publications):
        role = "reviewer"
        message = "补交证据" if publications else "首次复核"
        inputs = [refs["analyst"], refs["explorer"]]
    elif len(publications) < 3:
        return "publish_artifact", {"analysis_id": "orders", "target": refs["analyst"]}
    else:
        return None, {}
    return "delegation", {
        "analysis_id": "orders",
        "session_id": role,
        "agent_type": role,
        "message": message,
        "acceptance": [CHECK],
        "inputs": inputs,
    }


async def review_stream(self, messages, **kwargs):
    idx = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
    context = messages[idx].text
    tools = [m for m in messages[idx + 1 :] if isinstance(m, ToolMessage)]
    if context.startswith("取数"):
        async for chunk in scripted_stream(self, messages, **kwargs):
            yield chunk
        return
    if any(t.get("function", t).get("name") == "delegation" for t in kwargs["tools"]):
        name, args = planner_call([(m.name, json.loads(m.content)) for m in tools])
    else:
        task = json.loads(context.split("任务验收与固定输入 ", 1)[1])
        report = context.startswith("写报告")
        inputs = task["inputs"]
        filename = (
            "report.html"
            if report
            else ("corrected.txt" if context.startswith("补交证据") else "first.txt")
        )
        workspace = re.search(r"当前工作目录是 `([^`]+)`", messages[0].text)
        assert workspace
        path = f"{workspace[1]}/{filename}"
        if len(tools) < len(inputs):
            name, args = "read_file", {"file_path": inputs[len(tools)]["path"]}
        elif tools[-1].name == "read_file":
            assert all("206" in m.text for m in tools), [m.text for m in tools]
            name, args = (
                "write_file",
                {
                    "file_path": path,
                    "content": REPORT if report else "Report and source both show 206.",
                },
            )
        elif tools[-1].name == "write_file":
            name, args = "freeze_artifact", {"f_path": path}
        elif tools[-1].name == "freeze_artifact":
            frozen = json.loads(tools[-1].text)
            assert frozen["status"] == "success", (frozen, tools[-2].text)
            evidence = [frozen["path"]]
            if not report:
                prefix = "报告：" if context.startswith("首次复核") else ""
                evidence += [prefix + inputs[0]["path"], inputs[1]["path"]]
            name, args = (
                "submit_task_result",
                {"verdict": "passed", "checks": {CHECK: "206"}, "evidence": evidence},
            )
        else:
            assert tools[-1].name == "submit_task_result"
            name, args = None, {}
    yield ChatGenerationChunk(
        message=AIMessageChunk(
            content="" if name else "处理结束。",
            id=uuid4().hex,
            tool_calls=[{"name": name, "args": args, "id": uuid4().hex}]
            if name
            else [],
        )
    )


@pytest.mark.skipif(
    os.getenv("DATAAGENT_TEST_LIVE_DELIVERY") != "1", reason="需要本地开发数据服务"
)
def test_review_correction_to_download(tmp_path):
    from dataagent.engines.common.async_runtime import run_async

    with api(tmp_path, "h06", "tests.assistant.test_live_review_correction") as (
        client,
        _,
    ):
        created = client.post(
            "/api/v1/chat/create", json={"is_draft": True, "initial_message": "H06"}
        )
        created.raise_for_status()
        cid = created.json()["conversation_id"]
        response = client.post(
            "/api/v1/chat/stream",
            json={
                "conversation_id": cid,
                "message": {
                    "parts": [{"type": "text", "text": "交付九月订单报告"}],
                    "attachments": [],
                },
            },
        )
        response.raise_for_status()
        history = client.get("/api/v1/chat/ls/" + cid).json()["messages"]
        publications = [
            json.loads(p["content"])
            for m in history
            for p in m["parts"]
            if p["type"] == "tool_result" and p["name"] == "publish_artifact"
        ]
        assert [p["status"] for p in publications] == [
            "needs_revision",
            "verified",
            "verified",
        ], publications
        assert "复核目标" in publications[0]["reason"]
        receipt = publications[1]
        assert receipt == publications[2]
        _, stored = run_async(database_evidence(cid))
        assert stored == [receipt]
        assert any(
            a["delivery_status"] == "verified" and a["f_path"] == receipt["f_path"]
            for m in history
            for a in m.get("attachments") or []
        )
        downloaded = client.get(
            "/api/v1/chat/attachment/get",
            params={"conversation_id": cid, "f_path": receipt["f_path"]},
        )
        downloaded.raise_for_status()
        assert downloaded.content == REPORT.encode()
        assert hashlib.sha256(downloaded.content).hexdigest() in receipt["f_path"]
        print("H06", cid, receipt["receipt_id"])


if __name__ == "__main__":
    from dataagent.engines.common.async_runtime import run_async

    run_async(serve(Path(sys.argv[1]), sys.argv[2], review_stream))
