import pytest

from evals.catalog import catalog
from evals.compare import compare
from evals.controlled import prepare
from evals.finalize import SNAPSHOT_KEYS
from evals.live_report import jsonl, save, totals
from evals.metrics import hits, recall_metrics
from evals.offline_score import read


def test_context_and_top_five_have_different_denominators_of_observation():
    group = [["t.a"], ["t.b"], ["t.c", "t.d"], ["t.e"]]
    assert hits(group, [["t", "a"], ["t", "a"], ["t", "b"], ["t", "d"]])["rate"] == 0.75
    key = "dwd_trade_order_detail_di"
    value = [key, "order_id"]
    context = {
        "event": "recall_context",
        "types": ["column"],
        "columns": [value],
        "failures": [],
        "metrics": [],
    }
    assert recall_metrics("ORD-01", []) is None
    trace = [
        {
            "event": "recall_channel",
            "channel": "fulltext",
            "resource_type": "column",
            "term": "订单",
            "keys": [],
        },
        context,
    ]
    data = {
        "mode": "full",
        "updated_at": "2026-11-20T01:00:00+00:00",
        "query": "order",
        "tables": {key: {"columns": {"order_id": {}, "biz_date": {}}}},
        "metrics": {},
    }
    tools = [{"name": "recall_context", "result": data}]
    stale = {
        "id": "old",
        "updated_at": "2026-11-19T01:00:00+00:00",
        "query": "order",
        "column": [value],
    }

    def get(snapshots):
        return recall_metrics("ORD-01", trace, tools=tools, snapshots=snapshots)[
            "column"
        ]

    unknown = get([stale])
    assert unknown["fused_top_k"] is None
    assert unknown["channels"]["fulltext"]["terms"][0]["top_k"]["rate"] == 0
    assert unknown["channels"]["vector"]["status"] == "unobserved"
    measured = get(
        [stale, {**stale, "id": "current", "updated_at": data["updated_at"]}]
    )
    assert measured["fused_top_k"]["rate"] == 0.5
    assert measured["model_visible"]["rate"] == 1
    assert totals([])["physical_requests"] is None


def test_catalog_and_boundary_packages_match_source(tmp_path):
    catalog_rows = catalog()
    assert len(catalog_rows) == 200
    assert sum(bool(c["messages"]) for c in catalog_rows.values()) == 170
    assert "206" not in catalog_rows["ORD-01"]["messages"][0]
    prepare(tmp_path)
    results = read(tmp_path / "inventory.json")
    assert len(results) == 30
    assert sum(row.get("reference_pass", False) for row in results) == 15
    materials = tmp_path / "review_inputs"
    assert len(list(materials.glob("*.html"))) == 7
    assert "-40,000.00" in (materials / "REV-06-v1.html").read_text(encoding="utf8")
    assert "-45,964.99" in (materials / "REV-06-v2.html").read_text(encoding="utf8")


def test_comparison_pairs_first_attempts_and_detects_drift(tmp_path):
    left, right = tmp_path / "old", tmp_path / "new"
    snap = dict.fromkeys(SNAPSHOT_KEYS, "snapshot")
    for folder, trial, status in ((left, "t1", "fail"), (right, "t2", "pass")):
        group = folder / "first"
        group.mkdir(parents=True)
        save(group / "manifest.json", {"cases": ["A"]})
        save(
            folder / "manifest.json",
            {
                "scoring_sha256": dict.fromkeys(
                    (
                        "scorecards.json",
                        "pilot_cases.json",
                        "offline_score.py",
                        "live_report.py",
                        "score_replay.py",
                        "pay_fixture.py",
                    ),
                    "same",
                )
            },
        )
        save(folder / "before.json", snap)
        save(folder / "after.json", snap)
        jsonl(
            group / "trials.jsonl", [{"case": "A", "trial": trial, "business": status}]
        )
    assert compare(left, right)["counts"]["旧错新对"] == 1
    with pytest.raises(ValueError, match="独立"):
        compare(left, left)
    save(right / "after.json", {**snap, "metadata": "drift"})
    with pytest.raises(ValueError, match="metadata"):
        compare(left, right)
