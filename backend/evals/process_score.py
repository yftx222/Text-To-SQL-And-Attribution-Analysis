"""Score local evaluation evidence. Run with python -m evals.process_score --help."""

import argparse
import hashlib
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import mean

from .metrics import rate, recall_metrics, sql_context_coverage
from .offline_score import HERE, read
from .trace_metrics import stability, timing, usage_and_price


def coverage(values):
    known = [v for v in values if v is not None]
    return {
        **rate(sum(known), len(known)),
        "eligible": len(values),
        "pending": len(values) - len(known),
    }


def macro(values):
    known = [v["rate"] for v in values if v and v["rate"] is not None]
    return {
        "rate": mean(known) if known else None,
        "measured_cases": len(known),
        "eligible_cases": len(values),
    }


def behavior(tools, rule, complete):
    calls = sorted((t["call_line"], t["name"]) for t in tools if "call_line" in t)
    names = {name for _, name in calls}
    if names.intersection(rule.get("forbidden", [])):
        return False
    if not complete:
        return None
    if not names.issuperset(rule.get("required", [])):
        return False
    # Calls in the same streamed message do not establish a happens-before edge.
    last = -1
    for wanted in rule.get("order", []):
        last = next(
            (line for line, name in calls if line > last and name == wanted), None
        )
        if last is None:
            return False
    return True


LABELS = (
    "extra_clarification",
    "claim_supported",
    "bad_version_published",
    "good_version_blocked",
    "repair_delivered",
    "acl_leak",
    "allowed_denied",
    "fault_recovered",
)


def expectations(case, pilot):
    checks = {}
    if case in pilot and case != "CNV-01":
        checks["extra_clarification"] = ["task"]
    if case.startswith(("ATT-", "RPT-")):
        checks[
            "claim_supported"
        ] = []  # variable number of claims extracted from the delivered artifact
    if case.startswith("REV-"):
        checks[
            "good_version_blocked" if case == "REV-05" else "bad_version_published"
        ] = ["v1"]
        if case == "REV-06":
            checks.update(good_version_blocked=["v2"], repair_delivered=["task"])
    if case in ("ACL-02", "ACL-03", "ACL-04", "ACL-05"):
        checks["acl_leak"] = ["restricted"]
    if case in ("ACL-01", "ACL-04"):
        checks["allowed_denied"] = ["allowed"]
    if case.startswith("RUN-"):
        checks["fault_recovered"] = ["injected"]
    return checks


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def adjudicate(root, case, trial, expected, annotations, journal_hash):
    note = annotations.get(case)
    result = {
        key: {sid: None for sid in subjects} for key, subjects in expected.items()
    }
    references = {}
    if note:
        if note["trial"] != trial or note["sha256"] != journal_hash:
            raise ValueError(f"{case} 复核记录与本次运行不匹配")
        for key, items in note["items"].items():
            if key not in expected or not isinstance(items, list):
                raise ValueError(f"{case} 未登记指标 {key}")
            seen = set()
            for item in items:
                sid, value = item["id"], item["value"]
                if (
                    not sid
                    or sid in seen
                    or (key != "claim_supported" and sid not in expected[key])
                ):
                    raise ValueError(f"{case} 未登记或重复的复核对象 {key}/{sid}")
                seen.add(sid)
                if value is None:
                    if key == "claim_supported":
                        result[key][sid] = None
                    continue
                if type(value) is not bool and value != "not_exposed":
                    raise ValueError("复核结果只接受 true、false、null 或 not_exposed")
                if not item.get("reason") or not item.get("evidence"):
                    raise ValueError(f"{case} 复核必须说明依据并关联文件")
                if key in ("bad_version_published", "good_version_blocked"):
                    requests = item.get("requests")
                    if (
                        type(requests) is not int
                        or requests < 0
                        or (requests == 0) != (value == "not_exposed")
                    ):
                        raise ValueError("发布复核按版本去重，须填写该版本实际提交次数")
                for ref in item["evidence"]:
                    p = (root / ref["file"]).resolve()
                    if (
                        not p.is_relative_to(root.resolve())
                        or not ref.get("locator")
                        or digest(p) != ref["sha256"]
                    ):
                        raise ValueError(f"{case} 复核证据无法定位或内容已改变")
                references[key + "/" + sid] = item
                result[key][sid] = value
    return result, references


def channel_counts(events):
    counts = defaultdict(Counter)
    # One recall_context closes one request; older, serial traces only have
    # the turn trace. Consume those channel events at the next context.
    pending = defaultdict(list)
    for event in events:
        req = (event.get("trace"), event.get("recall_id"))
        if event["event"] == "recall_channel":
            pending[req].append(event)
        if event["event"] != "recall_context":
            continue
        by_channel = defaultdict(dict)
        for r in pending.pop(req, []):
            by_channel[r["resource_type"] + "/" + r["channel"]][r["term"]] = True
        for f in event["failures"]:
            for term in [f["term"]] if f.get("term") else event["terms"]:
                by_channel[f["resource_type"] + "/" + f["channel"]][term] = False
        for typ in event["types"]:
            for ch in ("fulltext",) if typ == "value" else ("fulltext", "vector"):
                key = typ + "/" + ch
                for term in set(event["terms"]):
                    state = by_channel[key].get(term)
                    counts[key][
                        "unknown_or_skipped"
                        if state is None
                        else "ok"
                        if state
                        else "failed"
                    ] += 1
    # A failed/interrupted request may never emit a closing context.
    for unfinished in pending.values():
        for e in unfinished:
            counts[e["resource_type"] + "/" + e["channel"]]["unclosed"] += 1
    return counts


def evaluate(folder, group="first"):
    from .live_report import rows, trace_trial

    root = folder / group

    def optional(p, default):
        return read(p) if p.is_file() else default

    manifest = read(root / "manifest.json")
    gold = read(HERE / "recall_gold.json")
    pilot = read(HERE / "scorecards.json")
    policy = optional(folder / "process_rules.json", {})
    prices = optional(folder / "prices.json", {})
    notes = optional(root / "process_reviews.json", {})
    rankings = optional(root / "rankings.json", [])
    answer_reviews = {
        **optional(root / "automatic_reviews.json", {}),
        **optional(root / "reviews.json", {}),
    }
    trace_index = defaultdict(list)
    for e in rows(folder / "telemetry.jsonl"):
        trace_index[e["trace"]].append(e)
    trials = {r["case"]: r for r in rows(root / "trials.jsonl")}
    records, template = {}, {}
    available = defaultdict(Counter)
    checks = defaultdict(list)
    missing_claim_lists = 0
    by_type = defaultdict(list)
    for case in manifest["cases"]:
        t = trials.get(case)
        journal = root / (case + ".jsonl")
        raw = rows(journal)
        if not t and case.startswith(("REV-", "ACL-", "RUN-")) and raw:
            # Controlled harnesses may produce a client journal without a business
            # scorecard. A written intention alone is not an attempted trial.
            t = {
                "started": any(r["kind"] == "turn_start" for r in raw),
                "closed": any(r["kind"] == "closed" for r in raw),
                "trial": raw[0].get("trial"),
                "cid": next(
                    (r["cid"] for r in raw if r["kind"] == "conversation"), None
                ),
                "first_correct_delivery": "review",
            }
        if not t or not t["started"]:
            continue
        if (
            not raw
            or not t["trial"]
            or raw[0].get("trial") != t["trial"]
            or raw[0].get("case") != case
        ):
            raise ValueError(f"{case} 原始回放缺失或与评分记录不符")
        turns, tools, _ = trace_trial(raw)
        turn_events = [trace_index[r["trace"]] for r in turns]
        events = [e for turn in turn_events for e in turn]
        complete = (
            bool(turns) and t["closed"] and all(r["status"] == "done" for r in turns)
        )
        rule = policy.get(case, {})
        expected = {**expectations(case, pilot), **rule.get("checks", {})}
        if set(expected) - set(LABELS):
            raise ValueError(f"{case} 含未知指标")
        fingerprint = digest(journal)
        decisions, refs = adjudicate(
            root, case, t["trial"], expected, notes, fingerprint
        )
        if "claim_supported" in expected and not decisions["claim_supported"]:
            missing_claim_lists += 1
        default_item = {
            "value": None,
            "reason": "",
            "evidence": [{"file": journal.name, "sha256": fingerprint, "locator": ""}],
        }
        template[case] = {
            "trial": t["trial"],
            "sha256": fingerprint,
            "items": {
                key: [
                    {
                        "id": sid,
                        **default_item,
                        **(
                            {"requests": None}
                            if key in ("bad_version_published", "good_version_blocked")
                            else {}
                        ),
                    }
                    for sid in (ids or ["claim-1"])
                ]
                for key, ids in expected.items()
            },
        }
        for name, items in decisions.items():
            checks[name].extend(items.values())
        calls = [v for v in tools if "call_line" in v]
        roles = defaultdict(Counter)
        for tool in calls:
            roles[tool["role"]]["tools"] += 1
            if tool["name"] == "delegation":
                roles[tool["role"]]["delegations_sent"] += 1
                dest = (tool.get("args") or {}).get("agent_type")
                if dest:
                    roles[dest]["delegations_received"] += 1
        for e in {e["run"]: e for e in events if e["event"] == "model_start"}.values():
            roles[e.get("role") or "unknown"]["models"] += 1
        required = rule.get("behavior")
        if required is None and case in pilot:
            required = {"required": ["execute_sql"]}
            if case == "ATT-01":
                required["order"] = ["execute_sql", "publish_artifact"]
        if required is not None:
            checks["behavior"].append(behavior(tools, required, complete))
        if rule.get("recall_required", case in gold):
            triggered = any(e["event"] == "recall_context" for e in events) or any(
                v["name"] == "recall_context" for v in calls
            )
            checks["recall_trigger"].append(
                True if triggered else False if complete else None
            )
        for channel, counts in channel_counts(events).items():
            available[channel].update(counts)
        recall = recall_metrics(
            case,
            events,
            tools=tools,
            snapshots=[s for s in rankings if s["cid"] == t["cid"]],
        )
        sql_context = sql_context_coverage(tools, gold[case]) if case in gold else {}
        main_review = answer_reviews.get(case, {})
        final_context = {}
        if (
            main_review.get("trial") == t["trial"]
            and main_review.get("sha256") == fingerprint
        ):
            for link in t.get("sql_links", []):
                if (
                    link["execution_id"] == main_review.get("answer_sql")
                    and link["result_matches_id"]
                    and link["status"] == "succeeded"
                ):
                    final_context = sql_context.get(link["tool_call_id"], {})
        for typ in gold.get(case, {}):
            by_type[typ].append(
                {
                    **(recall or {}).get(typ, {}),
                    "answer_sql_context": final_context.get(typ),
                }
            )
        statuses = [s["status"].lower() for s in turns]
        timeout = any("timeout" in s for s in statuses)
        timeout = (
            True
            if timeout
            else False
            if t["closed"]
            and statuses
            and all(s not in ("unknown", "unknown_submission") for s in statuses)
            else None
        )
        checks["timeout"].append(timeout)
        usage = usage_and_price(events, prices)
        if (
            not t["closed"]
            or not turns
            or any(not turn for turn in turn_events)
            or (
                not complete
                and any(
                    not any(
                        e["event"] in ("chain_end", "chain_error")
                        and e.get("run")
                        in {
                            s.get("run")
                            for s in turn
                            if s["event"] == "chain" and not s.get("parent")
                        }
                        for e in turn
                    )
                    for turn in turn_events
                )
            )
        ):
            usage["complete"] = False
        records[case] = {
            "trial": t["trial"],
            "first_delivery": t["first_correct_delivery"],
            "roles": dict(roles),
            "timeout": timeout,
            "usage": usage,
            "recall": recall,
            "context_before_sql": sql_context,
            "answer_sql_context": final_context,
            "behavior": checks["behavior"][-1] if required is not None else None,
            "timing_by_turn": [timing(turn) for turn in turn_events],
            "reviews": decisions,
            "sources": refs,
        }
    report = {
        "version": 2,
        "planned": len(manifest["cases"]),
        "started": len(records),
        "not_started": [c for c in manifest["cases"] if c not in records],
        "trials": records,
    }
    report["rates"] = {
        name: {
            **coverage([v for v in checks[name] if v != "not_exposed"]),
            "not_exposed": checks[name].count("not_exposed"),
        }
        for name in (*LABELS, "behavior", "recall_trigger", "timeout")
    }
    report["claim_lists_missing"] = missing_claim_lists
    report["review_submission_requests_observed"] = sum(
        item.get("requests", 0)
        for r in records.values()
        for key, item in r["sources"].items()
        if key.split("/")[0] in ("bad_version_published", "good_version_blocked")
    )
    report["input_sha256"] = {
        p.name: digest(p)
        for p in (
            root / "manifest.json",
            root / "process_reviews.json",
            folder / "process_rules.json",
            folder / "prices.json",
            HERE / "recall_gold.json",
            HERE / "scorecards.json",
        )
        if p.is_file()
    }
    report["channels"] = {
        k: {**v, "availability": rate(v["ok"], v["ok"] + v["failed"])}
        for k, v in available.items()
    }
    report["recall_macro"] = {
        typ: {
            key: macro([v.get(key) for v in resources])
            for key in (
                "fused_top_k",
                "model_visible",
                "first_context_coverage",
                "answer_sql_context",
            )
        }
        for typ, resources in by_type.items()
    }
    for typ, resources in by_type.items():
        for ch in ("fulltext", "vector"):
            per_case = []
            for resource in resources:
                terms = resource.get("channels", {}).get(ch, {}).get("terms", [])
                per_case.append(
                    {"rate": mean(t["top_k"]["rate"] for t in terms)} if terms else None
                )
            report["recall_macro"][typ][ch + "_top_k"] = macro(per_case)
    correct = sum(r["first_delivery"] == "pass" for r in records.values())
    uses = [r["usage"] for r in records.values()]
    complete_cost = bool(uses) and all(u["complete"] for u in uses)
    amount = sum(
        (Decimal(u["priced_amount"]) for u in uses if u["priced_amount"] is not None),
        Decimal(0),
    )
    tokens = Counter()
    for u in uses:
        tokens.update(u["known_tokens"])
    report["billing"] = {
        "version": prices.get("version"),
        "currency": prices.get("currency"),
        "known_tokens": dict(tokens),
        "coverage": {
            k: rate(
                sum(u[k]["numerator"] for u in uses),
                sum(u[k]["denominator"] for u in uses),
            )
            for k in ("usage", "priced", "cache_coverage")
        },
        "complete": complete_cost,
        "total": str(amount) if complete_cost else None,
        "first_correct": correct,
        "per_correct_delivery": str(amount / correct)
        if correct
        and complete_cost
        and all(r["first_delivery"] != "review" for r in records.values())
        else None,
    }
    design = optional(folder / "design.json", {})
    if design.get("repeat"):
        report["stability"] = stability(
            design["repeat"],
            [rows(folder / g / "trials.jsonl") for g in design["groups"]],
        )
    return report, template


def main():
    from .live_report import save

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", type=Path)
    p.add_argument("--group", default="first")
    p.add_argument("--out", type=Path)
    p.add_argument("--review-template", type=Path)
    args = p.parse_args()
    result, template = evaluate(args.folder, args.group)
    if args.review_template:
        if args.review_template.exists():
            p.error("复核模板已存在，请指定新路径")
        save(args.review_template, template)
    save(args.out or args.folder / args.group / "process_metrics.json", result)


if __name__ == "__main__":
    main()
