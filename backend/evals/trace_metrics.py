"""Resource and wall-clock measurements from the evaluation callback."""

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal

from .metrics import rate


def usage_and_price(events, prices):
    starts = {e["run"]: e for e in events if e["event"] == "model_start"}
    ends = {e["run"]: e for e in events if e["event"] in ("model_end", "model_error")}
    tokens = Counter()
    priced, measured, cache_known = 0, 0, 0
    amount = Decimal(0)
    for run, start in starts.items():
        usage = ends.get(run, {}).get("usage") or {}
        inp, out = usage.get("input_tokens"), usage.get("output_tokens")
        if any(type(x) is not int or x < 0 for x in (inp, out)):
            continue
        measured += 1
        tokens.update(input=inp, output=out)
        cache = (usage.get("input_token_details") or {}).get("cache_read")
        if type(cache) is int and 0 <= cache <= inp:
            cache_known += 1
            tokens.update(cache_read=cache, uncached_input=inp - cache)
        else:
            cache = None
        price = prices.get("models", {}).get(start.get("model"))
        if price is None:
            continue
        miss, hit, generated = (
            Decimal(str(price[k])) for k in ("uncached_input", "cache_read", "output")
        )
        if (
            not prices.get("version")
            or not prices.get("currency")
            or any(not v.is_finite() or v < 0 for v in (miss, hit, generated))
        ):
            raise ValueError("价格表需要版本、币种和有效的非负单价")
        if cache is None and miss != hit:
            continue
        amount += (
            inp * miss + (cache or 0) * (hit - miss) + out * generated
        ) / 1_000_000
        priced += 1
    runs = starts.keys() | ends.keys()
    unmatched_http = any(
        e["event"] == "model_http" and e.get("outcome") != "ok" for e in events
    )
    return {
        "known_tokens": dict(tokens),
        "usage": rate(measured, len(runs)),
        "cache_coverage": rate(cache_known, len(runs)),
        "priced": rate(priced, len(runs)),
        "priced_amount": str(amount) if priced else None,
        "complete": bool(runs) and priced == len(runs) and not unmatched_http,
    }


def timing(events):
    starts = {
        e["run"]: e
        for e in events
        if e["event"] in ("chain", "model_start", "tool_start", "sql_start")
    }
    ends = {
        e["run"]: e
        for e in events
        if e["event"]
        in (
            "chain_end",
            "chain_error",
            "model_end",
            "model_error",
            "tool_end",
            "tool_error",
            "sql_end",
        )
    }
    intervals = defaultdict(list)
    durations = {}
    for run in starts.keys() & ends.keys():
        start, end = starts[run], ends[run]
        a, b = (datetime.fromisoformat(e["at"]).timestamp() for e in (start, end))
        if b < a:
            continue
        durations[run] = (a, b)
        if start["event"] in ("model_start", "tool_start", "sql_start"):
            intervals[start["event"].split("_")[0]].append((a, b))
            if start.get("name") == "execute_sql":
                intervals["execute_sql"].append((a, b))
    stages = {}
    for name in ("model", "tool", "execute_sql", "sql"):
        parts = intervals[name]
        observed = sum(
            s["event"] == name + "_start"
            or (
                name == "execute_sql"
                and s["event"] == "tool_start"
                and s.get("name") == name
            )
            for s in starts.values()
        )
        end, wall = None, 0
        for a, b in sorted(parts):
            wall += b - max(a, min(b, end)) if end is not None else b - a
            end = max(b, end) if end is not None else b
        stages[name] = {
            "started_spans": observed,
            "completed_spans": len(parts),
            "wall_seconds": round(wall, 3) if parts else None,
            "sum_seconds": round(sum(b - a for a, b in parts), 3) if parts else None,
        }
    # The outer graph waits at each LangGraph superstep barrier. Work inside
    # a specialist node is already included in that node's wall duration.
    roots = [
        r for r, s in starts.items() if s["event"] == "chain" and not s.get("parent")
    ]
    path = None
    if len(roots) == 1 and roots[0] in durations:
        nodes = [
            s
            for s in starts.values()
            if s["event"] == "chain" and s.get("parent") == roots[0]
        ]
        if nodes and all(
            type(s.get("step")) is int
            and s.get("node") == s.get("name")
            and s["run"] in durations
            for s in nodes
        ):
            steps = defaultdict(list)
            for s in nodes:
                a, b = durations[s["run"]]
                steps[s["step"]].append((b - a, s["name"], s["run"]))
            path = [
                {"step": step, "node": name, "run": run, "seconds": round(d, 3)}
                for step, parts in sorted(steps.items())
                for d, name, run in [max(parts)]
            ]
    return {
        "stages": stages,
        "critical_steps": path,
        "critical_node_seconds": round(sum(p["seconds"] for p in path), 3)
        if path
        else None,
    }


def stability(cases, groups):
    result = {}
    for case in cases:
        runs = [
            [
                r
                for r in group
                if r["case"] == case and r.get("started") and r.get("closed")
            ]
            for group in groups
        ]
        chosen = [g[0] for g in runs if len(g) == 1]
        unique = (
            all(r.get("cid") and r.get("trial") for r in chosen)
            and len({r["trial"] for r in chosen}) == len(chosen)
            and len({r["cid"] for r in chosen}) == len(chosen)
        )
        states = [
            "fail"
            if any("timeout" in x["status"].lower() for x in r.get("turns", []))
            else r["business"]
            for r in chosen
        ]
        complete = len(groups) == 3 and len(chosen) == 3 and unique
        result[case] = {
            "observed": len(chosen),
            "complete": complete,
            "states": states,
            "all_pass": all(s == "pass" for s in states) if complete else None,
            "any_pass": "pass" in states if complete else None,
        }
        if complete and "review" in states:
            if "fail" not in states:
                result[case]["all_pass"] = None
            if "pass" not in states:
                result[case]["any_pass"] = None
    complete = [r for r in result.values() if r["complete"]]
    return {
        "cases": result,
        "missing_or_not_independent": len(result) - len(complete),
        **{
            key: {
                **rate(sum(r[key] is True for r in complete), len(complete)),
                "pending": sum(r[key] is None for r in complete),
            }
            for key in ("all_pass", "any_pass")
        },
    }
