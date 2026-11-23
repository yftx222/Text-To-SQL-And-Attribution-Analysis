"""Export scored DataAgent turns to a self-hosted Langfuse v4 project."""

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from dotenv import dotenv_values


def records(path):
    with path.open(encoding="utf8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def span_id(key):
    return sha256(key.encode()).hexdigest()[:16]


def nanos(at):
    dt = datetime.fromisoformat(at)
    return str(int(dt.timestamp()) * 10**9 + dt.microsecond * 1000)


TOOLS = {
    "delegation",
    "recall_context",
    "execute_sql",
    "shell",
    "read_file",
    "write_file",
    "read_verified_evidence",
    "recall_ontology",
    "execute_semantic_query",
    "publish_artifact",
    "freeze_artifact",
    "submit_task_result",
    "render_amount_comparison",
}
ROLES = {"planner", "explorer", "analyst", "reviewer"}


def turn_spans(trace, events, case, trial, begin, finish):
    trace_id = UUID(trace).hex
    root = span_id("root/" + trace)
    starts = {
        e["run"]: e
        for e in events
        if e["event"] in {"model_start", "tool_start", "chain"}
    }
    ends = {
        e["run"]: e
        for e in events
        if e["event"].endswith(("_end", "_error")) and "run" in e
    }
    common = {
        "langfuse.environment": "dataagent-eval",
        "langfuse.trace.name": case,
        "session.id": str(UUID(trial)),
    }

    def span(name, kind, ident, parent, at, until, extra, error=False):
        attrs = {**common, "langfuse.observation.type": kind, **extra}
        value = {
            "traceId": trace_id,
            "spanId": ident,
            "name": name,
            "kind": 1,
            "startTimeUnixNano": nanos(at),
            "endTimeUnixNano": nanos(until),
            "attributes": [
                {"key": k, "value": {"stringValue": v}} for k, v in attrs.items()
            ],
            "status": {"code": 2 if error else 0},
        }
        if parent:
            value["parentSpanId"] = parent
        return value

    spans = []
    incomplete = 0
    for run, start in starts.items():
        end = ends.get(run)
        if end is None:
            incomplete += 1
        parent = start.get("parent")
        kind = {"model_start": "generation", "tool_start": "tool", "chain": "chain"}[
            start["event"]
        ]
        role = start.get("role")
        attrs = {}
        name = kind
        if role in ROLES:
            attrs["langfuse.observation.metadata.role"] = role
            name = role + "." + kind
        if kind == "tool" and start.get("name") in TOOLS:
            name = start["name"]
        if kind == "chain" and start.get("name") in ROLES:
            name, kind = start["name"], "agent"
        if kind == "generation":
            usage = (end or {}).get("usage") or {}
            counts = {
                key: usage[field]
                for key, field in [
                    ("input", "input_tokens"),
                    ("output", "output_tokens"),
                    ("total", "total_tokens"),
                ]
                if type(usage.get(field)) is int
            }
            if counts:
                attrs["langfuse.observation.usage_details"] = json.dumps(counts)
            model = start.get("model") or ""
            if re.fullmatch(r"(?:deepseek|gpt|qwen)[-a-zA-Z0-9.]{1,64}", model):
                attrs["langfuse.observation.model.name"] = model
        if end is None:
            attrs["langfuse.observation.metadata.timing"] = "missing_end"
        spans.append(
            span(
                name,
                kind,
                span_id(run),
                span_id(parent) if parent in starts else root,
                start["at"],
                (end or start)["at"],
                attrs,
                bool(end and end["event"].endswith("_error")),
            )
        )
    spans.insert(
        0,
        span(
            case,
            "span",
            root,
            None,
            begin["at"],
            finish["at"],
            {"langfuse.observation.metadata.missing_ends": str(incomplete)},
            finish["status"] != "done",
        ),
    )
    return spans


def export(folder, group, client):
    folder = Path(folder)
    events = defaultdict(list)
    for event in records(folder / "telemetry.jsonl"):
        events[event["trace"]].append(event)
    exported = 0
    for result in records(folder / group / "trials.jsonl"):
        case = result["case"]
        if not re.fullmatch(r"[A-Z]{3}-\d{2}", case):
            raise ValueError("无效题号")
        journal = list(records(folder / group / (case + ".jsonl")))
        starts = [e for e in journal if e["kind"] == "turn_start"]
        finishes = {e["turn"]: e for e in journal if e["kind"] == "turn_end"}
        for start in starts:
            trace = start["trace"]
            if start["turn"] not in finishes or not events[trace]:
                raise ValueError(f"{case} 回合缺少终态或遥测，已保留本地证据")
            spans = turn_spans(
                trace,
                events[trace],
                case,
                result["trial"],
                start,
                finishes[start["turn"]],
            )
            for i in range(0, len(spans), 200):
                response = client.post(
                    "/api/public/otel/v1/traces",
                    headers={"x-langfuse-ingestion-version": "4"},
                    json={
                        "resourceSpans": [
                            {
                                "resource": {
                                    "attributes": [
                                        {
                                            "key": "service.name",
                                            "value": {"stringValue": "dataagent-eval"},
                                        }
                                    ]
                                },
                                "scopeSpans": [
                                    {
                                        "scope": {"name": "dataagent.eval"},
                                        "spans": spans[i : i + 200],
                                    }
                                ],
                            }
                        ]
                    },
                )
                response.raise_for_status()
                if int(
                    response.json().get("partialSuccess", {}).get("rejectedSpans", 0)
                ):
                    raise ValueError("Langfuse 拒收部分调用记录")
            if start == starts[-1] and result["closed"]:
                scores = []
                for key in ("business", "first_correct_delivery"):
                    verdict = result[key]
                    if verdict not in {"pass", "fail", "review"}:
                        raise ValueError("未知评分状态")
                    scores.append(
                        {
                            "id": str(uuid5(NAMESPACE_URL, trace + "/" + key)),
                            "traceId": UUID(trace).hex,
                            "name": key,
                            "value": verdict,
                            "dataType": "CATEGORICAL",
                            "environment": "dataagent-eval",
                        }
                    )
                response = client.post("/api/public/scores", json=scores)
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("errors"):
                    raise ValueError("Langfuse 拒收部分评分")
            exported += 1
    return exported


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", type=Path)
    p.add_argument("--group", default="first")
    p.add_argument("--env-file", type=Path, default=Path("conf/.env.langfuse"))
    args = p.parse_args()
    publish(args.folder, args.group, args.env_file)


def publish(folder, group, env_file):
    config = {**dotenv_values(env_file), **os.environ}
    with httpx.Client(
        base_url=config["LANGFUSE_BASE_URL"],
        auth=(config["LANGFUSE_PUBLIC_KEY"], config["LANGFUSE_SECRET_KEY"]),
        trust_env=False,
        timeout=30,
    ) as client:
        print("Langfuse accepted turns:", export(folder, group, client))


if __name__ == "__main__":
    main()
