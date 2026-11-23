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


def matched(groups, keys):
    seen = {".".join(key) if isinstance(key, (tuple, list)) else key for key in keys}
    return {
        i for i, choices in enumerate(groups) if any(key in seen for key in choices)
    }


def hits(groups, keys):
    return rate(len(matched(groups, keys)), len(groups))


def sql_context_coverage(tools, gold):
    """Replay visible metadata in the same specialist turn up to each SQL call."""
    events = []
    for t in tools:
        if t["name"] == "execute_sql" and t.get("call_line"):
            events.append((t["call_line"], 0, t))
        if t["name"] == "recall_context" and t.get("result_line"):
            events.append((t["result_line"], 1, t))
    contexts, result = {}, {}
    for _, is_result, t in sorted(events, key=lambda e: e[:2]):
        scope = (t["turn"], t.get("delegation"))
        if not scope[1]:
            continue
        if not is_result:
            known = [v for (s, _), v in contexts.items() if s == scope]
            if known and all(v is not None for v in known):
                result[t["call"]] = {
                    typ: hits(groups, set().union(*(v[typ] for v in known)))
                    for typ, groups in gold.items()
                }
            continue
        payload = t.get("result")
        if not isinstance(payload, dict) or payload.get("mode") not in (
            "full",
            "delta",
        ):
            continue
        key = (scope, payload.get("query"))
        if payload["mode"] == "full":
            contexts[key] = {"column": set(), "metric": set()}
        state = contexts.setdefault(key, None)
        if state is None:
            continue
        for table, info in payload.get("tables", {}).items():
            state["column"].update(table + "." + c for c in info.get("columns", {}))
        state["metric"].update(payload.get("metrics", {}))
        removed = payload.get("removed", {})
        state["metric"].difference_update(removed.get("metrics", []))
        for table, info in removed.get("tables", {}).items():
            if not info:
                state["column"] = {
                    c for c in state["column"] if not c.startswith(table + ".")
                }
            else:
                state["column"].difference_update(
                    table + "." + c
                    for c, change in info.get("columns", {}).items()
                    if not change
                )
    return result


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
        term_hits = {}
        for ch in ("fulltext", "vector"):
            searched = [
                r
                for r in traces[:first]
                if r["event"] == "recall_channel"
                and r.get("recall_id") == context.get("recall_id")
                and r.get("trace") == context.get("trace")
                and r["resource_type"] == typ
                and r["channel"] == ch
            ]
            failed = [
                f
                for f in context["failures"]
                if f["resource_type"] == typ and f["channel"] == ch
            ]
            term_hits[ch] = {
                r["term"]: matched(groups, r["keys"][:k])
                for r in searched
                if not any(f.get("term") in (None, r["term"]) for f in failed)
            }
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
            "vector_only_gold_groups": {
                term: len(found - term_hits["fulltext"][term])
                for term, found in term_hits["vector"].items()
                if term in term_hits["fulltext"]
            },
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
    verified = [
        verdict([t["first_correct_delivery"], t.get("counterexample_status", "review")])
        for t in started
    ]
    return {
        "started": len(started),
        "verified_delivery": {
            **rate(verified.count("pass"), len(started)),
            "review": verified.count("review"),
        },
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
