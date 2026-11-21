import asyncio
import hashlib
import json

import httpx

from evals.live import append, downloads, journal, replay_case
from evals.live_report import totals, trace_trial
from evals.offline_score import load_cards


def event(kind, **fields):
    return "data: " + json.dumps({"type": kind, **fields}) + "\n\n"


def test_dropped_stream_continues_and_resume_never_resubmits(tmp_path):
    posted = []
    next_id = iter(("one", "two"))
    cards = load_cards()
    msg = {
        "message_id": "id",
        "role": "assistant",
        "parts": [
            {"type": "text", "text": "结果"},
            {"type": "thinking", "text": "private-canary"},
        ],
    }

    def handler(req):
        url = req.url.path
        if url.endswith("/create"):
            return httpx.Response(201, json={"conversation_id": next(next_id)})
        if url.endswith("/stream"):
            body = json.loads(req.content)
            assert set(body["message"]) == {"parts", "attachments"}
            posted.append(body["conversation_id"])
            stream = (
                event("error")
                if posted[-1] == "one"
                else event("message", message=msg) + event("done")
            )
            return httpx.Response(200, text=stream)
        if url.endswith("/run"):
            return httpx.Response(200, json={"running": False})
        return httpx.Response(200, json={"messages": [msg]})

    async def replay():
        async with httpx.AsyncClient(
            base_url="http://local", transport=httpx.MockTransport(handler)
        ) as client:
            for _ in range(2):
                for case in ("ORD-01", "AMT-02"):
                    await replay_case(
                        client, tmp_path / (case + ".jsonl"), cards[case], 5
                    )
            partial = tmp_path / "unfinished.jsonl"
            append(partial, "intent", case="ORD-01", trial="t")
            append(partial, "conversation", cid="three")
            append(partial, "turn_start", turn=0, trace="tr", question="问题")
            await replay_case(client, partial, cards["ORD-01"], 5)
            assert journal(partial)[-1]["status"] == "unknown_submission"

    asyncio.run(replay())
    assert posted == ["one", "two"]
    assert journal(tmp_path / "ORD-01.jsonl")[-1]["status"] == "stream_error"
    good = tmp_path / "AMT-02.jsonl"
    assert journal(good)[-1]["status"] == "done"
    assert "private-canary" not in good.read_text(encoding="utf-8")
    assert trace_trial(journal(good))[0][0]["answer"] == "结果"


def test_slow_subscriber_reconnects_to_original_run(tmp_path):
    paths = []
    path = tmp_path / "trial.jsonl"

    def handler(request):
        route = request.url.path
        paths.append((request.method, route))
        if route.endswith("/create"):
            return httpx.Response(201, json={"conversation_id": "c"})
        if route.endswith("/stream"):
            return httpx.Response(
                200,
                text=event(
                    "error", content="事件流消费速度过慢，请重新连接以恢复最新状态。"
                ),
            )
        if route.endswith("/events"):
            msg = {"role": "assistant", "parts": [{"type": "text", "text": "答案"}]}
            return httpx.Response(
                200, text=event("message", message=msg) + event("done")
            )
        if route.endswith("/run"):
            return httpx.Response(200, json={"running": False})
        return httpx.Response(200, json={"messages": []})

    async def replay():
        async with httpx.AsyncClient(
            base_url="http://local", transport=httpx.MockTransport(handler)
        ) as client:
            await replay_case(client, path, load_cards()["AMT-02"], 5)

    asyncio.run(replay())
    assert sum(route.endswith("/stream") for _, route in paths) == 1
    assert any(
        method == "GET" and route.endswith("/c/events") for method, route in paths
    )
    assert trace_trial(journal(path))[0][0]["answer"] == "答案"
    assert journal(path)[-1]["status"] == "done"


def test_download_receipt_checked_and_repeated_receipt_deduplicated(tmp_path):
    content = b"real report"
    digest = hashlib.sha256(content).hexdigest()
    receipt = {
        "status": "verified",
        "receipt_id": "receipt",
        "f_path": "/data/file_" + digest + ".html",
    }
    part = {
        "type": "tool_result",
        "name": "publish_artifact",
        "content": json.dumps(receipt),
    }

    async def check(data, expected):
        path = tmp_path / (str(expected) + ".jsonl")
        append(path, "history", data=[{"parts": [part, part]}])
        async with httpx.AsyncClient(
            base_url="http://local",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=data)),
        ) as c:
            await downloads(c, path, "cid")
        recorded = [r for r in journal(path) if r["kind"] == "download"]
        assert len(recorded) == 1 and recorded[0]["verified"] is expected

    asyncio.run(check(content, True))
    asyncio.run(check(b"earlier version", False))


def test_dedup_model_usage_and_missing_values():
    start = {"event": "model_start", "run": "one", "role": "explorer"}
    end = {"event": "model_end", "run": "one", "usage": {"total_tokens": 7}}
    assert totals([start, start, end, end])["total_tokens"] == 7
    assert totals([start, end, {**start, "run": "two"}])["total_tokens"] is None
