import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dataagent.engines.common.observability import evaluation

pytest.importorskip(
    "trace_sdk.client", reason="requires evaluation-platform-trace wheel for D2"
)
from evals.platform_trace import EvaluationPlatformTraceSink, Finish


def test_scoped_callbacks_and_failed_delivery():
    received = {}

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            attempt = self.headers["Authorization"].split()[1].split(".")[0]
            entry = received.setdefault(attempt, {"events": []})
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("events:batch"):
                entry["events"].extend(body["events"])
                reply = {"accepted": len(body["events"]), "duplicate": 0}
            else:
                entry["outcome"] = body["outcome"]
                reply = {"status": body["outcome"]}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"data": reply}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def exercise():
        sink = EvaluationPlatformTraceSink(
            f"http://127.0.0.1:{server.server_port}", "reader"
        )
        user = SimpleNamespace(id=1, username="reader", is_admin=False)
        gate = asyncio.Event()

        async def one(name, index):
            attempt, cid = uuid4(), uuid4()
            headers = {
                "x-trace-id": str(uuid4()),
                "x-trace-write": str(attempt) + "." + "x" * 43,
                "x-eval-turn": str(index),
            }

            async def background():
                await gate.wait()
                evaluation.emit("chain", run="parent", name=name)
                evaluation.emit(
                    "recall_context", terms=["private evidence"], columns=[], metrics=[]
                )
                evaluation.emit("chain_end", run="parent")

            with sink.capture(headers, user, cid):
                bg = asyncio.create_task(background())
            assert evaluation.observer.get() is None
            await asyncio.sleep(0)
            gate.set()
            await bg
            req = SimpleNamespace(headers=headers)
            wrong = await sink.finish(
                Finish(conversation_id=uuid4(), complete=True), req, user
            )
            assert wrong["status"] == "MISSING"
            res = await sink.finish(
                Finish(conversation_id=cid, complete=True), req, user
            )
            return str(attempt), res["status"]

        first, second = await asyncio.gather(one("alpha", 1), one("beta", 2))
        assert first[1] == "COMPLETE" and second[1] == "PARTIAL"
        for (owner, _), name in [(first, "alpha"), (second, "beta")]:
            assert [e["name"] for e in received[owner]["events"]] == [
                name,
                "recall_context",
                "planner.final",
            ]
        assert "private evidence" not in json.dumps(received)
        assert not sink.attempts
        server.shutdown()
        server.server_close()
        owner, status = await one("unavailable", 1)
        assert status == "PARTIAL" and owner not in received

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
