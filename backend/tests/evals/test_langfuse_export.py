import json
from uuid import uuid4

import httpx
import pytest

from evals.langfuse_export import export


def test_trace_scores_privacy_and_replay(tmp_path):
    trace, trial, run = (str(uuid4()) for _ in range(3))
    group = tmp_path / "first"
    group.mkdir()
    at = "2026-11-21T00:00:00+00:00"

    def write(path, rows):
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf8")

    write(
        tmp_path / "telemetry.jsonl",
        [
            {
                "trace": trace,
                "event": "model_start",
                "run": run,
                "at": at,
                "role": "explorer",
                "model": "deepseek-chat",
                "session": "PRIVATE_SENTINEL",
            },
            {
                "trace": trace,
                "event": "model_end",
                "run": run,
                "at": at,
                "usage": {"input_tokens": 9, "output_tokens": 2, "total_tokens": 11},
            },
        ],
    )
    write(
        group / "ORD-01.jsonl",
        [
            {
                "kind": "turn_start",
                "trace": trace,
                "turn": 0,
                "at": at,
                "question": "PRIVATE_SENTINEL",
            },
            {"kind": "turn_end", "turn": 0, "at": at, "status": "done"},
        ],
    )
    write(
        group / "trials.jsonl",
        [
            {
                "trial": trial,
                "case": "ORD-01",
                "business": "review",
                "first_correct_delivery": "fail",
                "closed": True,
            }
        ],
    )
    sent = []

    def handle(req):
        assert b"PRIVATE_SENTINEL" not in req.content
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={})

    with httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as client:
        assert export(tmp_path, "first", client) == 1
        assert export(tmp_path, "first", client) == 1
    assert sent[:2] == sent[2:]
    root, model = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert model["parentSpanId"] == root["spanId"]
    assert '"total": 11' in str(model["attributes"])
    assert [s["value"] for s in sent[1]] == ["review", "fail"]

    def unavailable(req):
        return httpx.Response(503)

    with (
        httpx.Client(
            base_url="http://test", transport=httpx.MockTransport(unavailable)
        ) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        export(tmp_path, "first", client)
