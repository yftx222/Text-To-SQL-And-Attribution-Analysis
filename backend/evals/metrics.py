"""Coverage and delivery metrics from the local scorer and backend traces."""

from datetime import datetime
from statistics import median

from .offline_score import HERE, read, verdict


def rate(passed, denominator):
    return {
        "numerator": passed,
        "denominator": denominator,
        "rate": passed / denominator if denominator else None,
    }


def hits(groups, keys):
    seen = {".".join(key) if isinstance(key, (tuple, list)) else key for key in keys}
    return rate(
        sum(any(key in seen for key in choices) for choices in groups), len(groups)
    )


def recall_metrics(case, traces, k=5, *, tools=(), snapshots=()):
    gold = read(HERE / "recall_gold.json").get(case, {})
    first = next(
        (i for i, r in enumerate(traces) if r["event"] == "recall_context"), None
    )
    if first is None or not gold:
        return None
    context = traces[first]
    call = next((t for t in tools if t["name"] == "recall_context"), {})
    payload = call.get("result")
    snapshot = None
    if (
        isinstance(payload, dict)
        and payload.get("mode") == "full"
        and payload.get("updated_at")
    ):
        matches = [
            s
            for s in snapshots
            if s["query"] == payload.get("query")
            and datetime.fromisoformat(s["updated_at"])
            == datetime.fromisoformat(payload["updated_at"])
        ]
        if len(matches) == 1:
            snapshot = matches[0]
    result = {}
    for typ, groups in gold.items():
        channels = {}
        for ch in ("fulltext", "vector"):
            searched = [
                r
                for r in traces[:first]
                if r["event"] == "recall_channel"
                and r["resource_type"] == typ
                and r["channel"] == ch
            ]
            failed = [
                f
                for f in context["failures"]
                if f["resource_type"] == typ and f["channel"] == ch
            ]
            channels[ch] = {
                "terms": [
                    {
                        "term": r["term"],
                        "retrieved": len(r["keys"]),
                        "top_k": hits(groups, r["keys"][:k]),
                    }
                    for r in searched
                ],
                "status": "partial"
                if failed and searched
                else "failed"
                if failed
                else "ok"
                if searched
                else "unobserved"
                if typ in context["types"]
                else "not_requested",
                "failed_terms": [f.get("term") for f in failed],
            }
        keys = context["columns" if typ == "column" else "metrics"]
        visible = None
        if isinstance(payload, dict) and payload.get("mode") == "full":
            model_keys = (
                [
                    [table, col]
                    for table, desc in payload["tables"].items()
                    for col in desc.get("columns", {})
                ]
                if typ == "column"
                else list(payload["metrics"])
            )
            visible = hits(groups, model_keys)
        result[typ] = {
            "channels": channels,
            "first_context_coverage": hits(groups, keys),
            "k_per_term": k,
            "fused_top_k": hits(groups, snapshot[typ][:k]) if snapshot else None,
            "recall_snapshot": snapshot["id"] if snapshot else None,
            "model_visible": visible,
        }
    return result


def summarize(trials, scores):
    started = [t for t in trials if t["started"]]

    def by_rule(keys, candidates):
        states = []
        for t in candidates:
            selected = [
                r["status"]
                for r in scores
                if r["trial_id"] == t["trial"] and r["assertion"] in keys
            ]
            if selected:
                states.append(verdict(selected))
        return {
            **rate(states.count("pass"), len(states)),
            "review": states.count("review"),
        }

    numeric = {r["assertion"] for r in scores if type(r["expected"]) in (str, int)}
    seconds = sorted(t["seconds"] for t in started if t["closed"])
    sql_total = sum(sum(t["sql"].values()) for t in started)
    return {
        "started": len(started),
        "adjudicated": sum(t["business"] != "review" for t in started),
        "business": rate(sum(t["business"] == "pass" for t in started), len(started)),
        "first_correct_delivery": rate(
            sum(t["first_correct_delivery"] == "pass" for t in started), len(started)
        ),
        "numeric": by_rule(numeric, started),
        "sql_scope": by_rule({"sql_scope"}, started),
        "boundary": by_rule({"month_end_counts"}, started),
        "clarification": by_rule({"clarified"}, started),
        "insufficient_data": by_rule(
            {"coverage_disclosed", "change_unavailable"}, started
        ),
        "attribution": by_rule(
            {
                "APP",
                "PC",
                "SEM",
                "MINI_PROGRAM",
                "H5",
                "ALL",
                "balanced",
                "causal_limit",
            },
            started,
        ),
        "download": by_rule({"delivered"}, started),
        "sql_execution": rate(
            sum(t["sql"].get("succeeded", 0) for t in started), sql_total
        ),
        "known_tokens": sum(t["usage"]["known_tokens"] for t in started),
        "usage_coverage": rate(
            sum(t["usage"]["usage_reported"] for t in started),
            sum(t["usage"]["model_calls"] for t in started),
        ),
        "cost": None,
        "latency_seconds": {
            "n": len(seconds),
            "p50": median(seconds) if seconds else None,
            "p90_nearest_rank": seconds[(9 * len(seconds) + 9) // 10 - 1]
            if seconds
            else None,
        },
    }
