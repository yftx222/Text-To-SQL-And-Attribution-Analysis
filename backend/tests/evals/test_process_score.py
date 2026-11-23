import pytest

from evals.live_report import jsonl, save
from evals.metrics import recall_metrics
from evals.process_score import (
    adjudicate,
    behavior,
    channel_counts,
    coverage,
    digest,
    evaluate,
    macro,
)
from evals.trace_metrics import stability, timing, usage_and_price


def test_sql_context_follows_visible_delta_and_specialist():
    from evals.metrics import sql_context_coverage

    scope = {"turn": 0, "delegation": "d"}
    full = {"mode": "full", "query": "q", "tables": {"t": {"columns": {"a": {}}}}}
    delta = {
        "mode": "delta",
        "query": "q",
        "tables": {"t": {"columns": {"b": {}}}},
        "removed": {"tables": {"t": {"columns": {"a": {}}}}},
    }
    tools = [
        {**scope, "name": "recall_context", "result": full, "result_line": 1},
        {**scope, "name": "execute_sql", "call": "before", "call_line": 2},
        {**scope, "name": "recall_context", "result": delta, "result_line": 3},
        {**scope, "name": "execute_sql", "call": "after", "call_line": 4},
        {
            **scope,
            "delegation": "other",
            "name": "execute_sql",
            "call": "x",
            "call_line": 5,
        },
    ]
    result = sql_context_coverage(tools, {"column": [["t.b"]]})
    assert result["before"]["column"]["rate"] == 0
    assert result["after"]["column"]["rate"] == 1
    assert "x" not in result


def test_behavior_order_missing_and_forbidden():
    tools = [{"name": "sql", "call_line": 2}, {"name": "publish", "call_line": 2}]
    rule = {"required": ["sql"], "order": ["sql", "publish"], "forbidden": ["delete"]}
    assert behavior(tools, rule, True) is False
    assert behavior(tools, rule, False) is None
    tools.append({"name": "publish", "call_line": 3})
    assert behavior(tools, rule, True)
    assert behavior([*tools, {"name": "delete", "call_line": 9}], rule, False) is False


def test_macro_vs_micro_and_evidence_coverage():
    assert macro([{"rate": 0.5}, {"rate": 1}, None]) == {
        "rate": 0.75,
        "measured_cases": 2,
        "eligible_cases": 3,
    }
    assert coverage([True, False, None]) == {
        "rate": 0.5,
        "numerator": 1,
        "denominator": 2,
        "eligible": 3,
        "pending": 1,
    }
    assert coverage([None])["rate"] is None


def test_vector_difference_and_channel_failure_scope():
    key = "dwd_trade_order_detail_di"
    common = {
        "trace": "t",
        "recall_id": "a",
        "event": "recall_channel",
        "resource_type": "column",
        "term": "order",
    }
    events = [
        {**common, "channel": "fulltext", "keys": []},
        {
            **common,
            "recall_id": "other",
            "channel": "fulltext",
            "keys": [[key, "order_id"]],
        },
        {**common, "channel": "vector", "keys": [[key, "order_id"], [key, "order_id"]]},
        {
            "trace": "t",
            "recall_id": "a",
            "event": "recall_context",
            "terms": ["order"],
            "types": ["column"],
            "columns": [],
            "metrics": [],
            "failures": [],
        },
    ]
    assert recall_metrics("ORD-01", events)["column"]["vector_only_gold_groups"] == {
        "order": 1
    }
    events.append(
        {
            "trace": "t",
            "recall_id": "b",
            "event": "recall_context",
            "types": ["column"],
            "terms": ["a", "b"],
            "failures": [
                {"resource_type": "column", "channel": "vector", "term": None}
            ],
        }
    )
    stats = channel_counts(events)
    assert stats["column/vector"] == {"ok": 1, "failed": 2}
    assert stats["column/fulltext"] == {"ok": 1, "unknown_or_skipped": 2, "unclosed": 1}


def test_usage_coverage_and_non_overlapping_cost_buckets():
    start = {"event": "model_start", "run": "a", "model": "M"}
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "input_token_details": {"cache_read": 40},
        "output_token_details": {"reasoning": 6},
    }
    end = {"event": "model_end", "run": "a", "usage": usage}
    price = {
        "version": "fixture",
        "currency": "CNY",
        "models": {"M": {"uncached_input": 2, "cache_read": 1, "output": 4}},
    }
    actual = usage_and_price([start, end, end], price)
    assert float(actual["priced_amount"]) == 0.0002 and actual["complete"]
    assert not usage_and_price([start, end, {**start, "run": "b"}], price)["complete"]
    assert not usage_and_price(
        [start, {**end, "usage": {**usage, "input_token_details": {}}}], price
    )["complete"]
    assert not usage_and_price(
        [start, end, {"event": "model_http", "outcome": "timeout"}], price
    )["complete"]


def test_graph_barriers_and_overlapping_stages():
    def e(kind, run, sec, **kw):
        return {
            "event": kind,
            "run": run,
            "at": f"2026-01-01T00:00:{sec:02}+00:00",
            **kw,
        }

    rows = [e("chain", "root", 0), e("chain_end", "root", 12)]
    for run, step, a, b in [("A", 0, 1, 4), ("B", 0, 1, 6), ("C", 1, 7, 11)]:
        rows += [
            e("chain", run, a, parent="root", step=step, node=run, name=run),
            e("chain_end", run, b),
        ]
    rows += [
        e("tool_start", "x", 1, name="execute_sql"),
        e("tool_start", "y", 3, name="execute_sql"),
        e("tool_end", "x", 5),
        e("tool_end", "y", 6),
    ]
    result = timing(rows)
    assert result["critical_node_seconds"] == 9
    assert result["stages"]["tool"]["wall_seconds"] == 5
    assert result["stages"]["tool"]["sum_seconds"] == 7
    assert result["stages"]["sql"]["wall_seconds"] is None
    assert timing(rows[1:])["critical_steps"] is None


def test_stability_requires_independent_repeats_and_counts_timeouts():
    def row(i):
        return {
            "case": "A",
            "started": True,
            "closed": True,
            "trial": str(i),
            "cid": str(i),
            "business": "pass",
        }

    groups = [[row(1)], [row(2)], [{**row(3), "turns": [{"status": "ReadTimeout"}]}]]
    stats = stability(["A", "missing"], groups)
    assert stats["missing_or_not_independent"] == 1 and stats["any_pass"]["rate"] == 1
    assert stats["all_pass"]["rate"] == 0
    assert stability(["A"], [groups[0]] * 3)["all_pass"]["rate"] is None


def test_controlled_review_is_versioned_and_requires_bound_evidence(tmp_path):
    root = tmp_path / "first"
    root.mkdir()
    save(root / "manifest.json", {"cases": ["REV-06", "ACL-02"]})
    raw = root / "REV-06.jsonl"
    jsonl(
        raw,
        [
            {"kind": "intent", "case": "REV-06", "trial": "run"},
            {"kind": "turn_start", "trace": "t", "question": "review"},
            {"kind": "turn_end", "turn": 0, "status": "done", "seconds": 1},
            {"kind": "closed"},
        ],
    )
    initial, template = evaluate(tmp_path)
    assert initial["not_started"] == ["ACL-02"]
    assert initial["rates"]["repair_delivered"]["pending"] == 1
    bad = template["REV-06"]["items"]["bad_version_published"][0]
    bad.update(value=True, reason="测试记录表明错误版本被发布", requests=2)
    bad["evidence"] = [
        {"file": raw.name, "sha256": digest(raw), "locator": "测试记录第 3 行"}
    ]
    save(root / "process_reviews.json", template)
    result, _ = evaluate(tmp_path)
    assert result["rates"]["bad_version_published"]["denominator"] == 1
    assert result["review_submission_requests_observed"] == 2
    raw.write_text(raw.read_text() + "\n", encoding="utf8")
    with pytest.raises(ValueError, match="不匹配"):
        adjudicate(root, "REV-06", "run", {}, template, digest(raw))
