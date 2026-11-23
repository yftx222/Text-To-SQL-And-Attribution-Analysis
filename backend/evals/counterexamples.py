"""Replay the answer-producing SQL on independent rows; never call a model."""

import argparse
import asyncio
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from .offline_score import read, verdict
from .time_scope_fixture import (
    PAYMENT,
    SEARCH_COHORT,
    SESSION_COHORT,
    replay,
    with_placeholder_sku,
)


def digest(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def fixtures():
    # Add two eligible entities. The old and new denominators differ so a
    # hard-coded percentage cannot pass both datasets.
    specs = {}
    for name, tables, anchor, new_rows in (
        (
            "search",
            SEARCH_COHORT,
            "dwd_traffic_search_di",
            [
                (5, 1, "2026-09-20", "2026-09-20 12:00:00"),
                (6, 1, "2026-09-21", "2026-09-21 12:00:00"),
            ],
        ),
        (
            "sessions",
            SESSION_COHORT,
            "dwd_traffic_session_di",
            [
                (5, "2026-09-20 12:00:00", "2026-09-20"),
                (6, "2026-09-21 12:00:00", "2026-09-21"),
            ],
        ),
        (
            "sku",
            with_placeholder_sku(),
            "dim_sku_info_zip",
            [
                (
                    4,
                    41,
                    "在售",
                    0,
                    1,
                    "2026-09-20 00:00:00",
                    "9999-12-31 00:00:00",
                    7,
                    8,
                ),
                (
                    5,
                    51,
                    "在售",
                    0,
                    1,
                    "2026-09-25 00:00:00",
                    "9999-12-31 00:00:00",
                    7,
                    8,
                ),
            ],
        ),
    ):
        cols, rows = tables[anchor]
        specs[name] = tables
        specs[name + "_expanded"] = {**tables, anchor: (cols, [*rows, *new_rows])}
    for name in ("search", "search_expanded"):
        anchor = "dwd_traffic_search_di"
        cols, rows = specs[name][anchor]
        specs[name] = {
            **specs[name],
            anchor: (
                cols + " result_total_count",
                [(*r, 3 if r[1] else 0) for r in rows],
            ),
        }
    sku = dict(specs["sku"])
    detail = "dwd_trade_order_detail_di"
    cols, rows = sku[detail]
    sku[detail] = (
        cols,
        [*rows, (101, 1001, 0, 0, 1, "2026-09-16", "2026-09-16 10:00:00")],
    )
    events = "dwd_trade_order_status_event_di"
    cols, rows = sku[events]
    sku[events] = (
        cols,
        [*rows, (101, "PAID", 1, "2026-09-16 11:00:00", "2026-09-16", 103)],
    )
    specs["sku_placeholder_sold"] = sku
    payment = dict(PAYMENT)
    cols, rows = payment["dwd_trade_pay_detail_di"]
    payment["dwd_trade_pay_detail_di"] = (
        cols + " requested_pay_amount",
        [(*r, 10) for r in rows],
    )
    specs["payment"] = payment
    return specs


def suite(plan, case):
    data = fixtures()
    return [
        (s, data[s["fixture"]] if "fixture" in s else s["tables"])
        for s in plan.get(case, [])
    ]


def binding(case, trial, journal_hash, query, columns, variants):
    return digest(
        [case, trial, journal_hash, query["id"], query["raw_sql"], columns, variants]
    )


def outcome(rows, columns, expected, *, ranked_prefix=False, row_equals=None):
    if row_equals:
        if not rows or any(key not in row for row in rows for key in row_equals):
            return "review"
        rows = [
            row
            for row in rows
            if all(row[key] == value for key, value in row_equals.items())
        ]
        if len(rows) != 1:
            return "fail"
    if expected and isinstance(expected[0], list):
        # Compare groups independently of the SQL's display order.
        if not rows or not columns or any(len(row) != len(columns) for row in expected):
            return "review"
        observed = []
        try:
            for row in rows:
                observed.append(
                    tuple(
                        str(row[key])
                        if isinstance(sample, str)
                        else Decimal(str(row[key]))
                        for key, sample in zip(columns, expected[0], strict=True)
                    )
                )
        except (KeyError, InvalidOperation):
            return "review"
        if ranked_prefix:
            return (
                "pass"
                if observed[: len(expected)] == list(map(tuple, expected))
                else "fail"
            )
        return "pass" if sorted(observed) == sorted(map(tuple, expected)) else "fail"
    if (
        len(rows) != 1
        or not columns
        or len(columns) != len(expected)
        or len(set(columns)) != len(columns)
    ):
        return "review"
    try:
        observed = [Decimal(str(rows[0][c])) for c in columns]
        targets = [Decimal(str(n)) for n in expected]
        return "pass" if observed == targets else "fail"
    except (KeyError, InvalidOperation):
        return "review"


def check_record(record, fingerprint, columns, variants):
    if not record or record.get("binding") != fingerprint or not variants:
        return "review"
    results = record.get("results", [])
    if len(results) != len(variants):
        return "review"
    return verdict(
        [
            outcome(
                r.get("rows", []),
                columns,
                s["expected"],
                ranked_prefix=s.get("ranked_prefix", False),
                row_equals=s.get("row_equals"),
            )
            if r.get("fixture") == s.get("fixture", "custom") and not r.get("error")
            else "review"
            for r, (s, _) in zip(results, variants, strict=True)
        ]
    )


async def run(folder, group, reviews=None):
    from .live_report import save

    root = folder / group
    manifest = read(root / "manifest.json")
    if manifest.get("stage") == "first_pass":
        return
    plan = read(folder / "counterexample_plan.json")
    audit = read(root / "audit.json")
    if reviews is None:
        from .auto_review import collect

        reviews = collect(folder, group, audit)
    saved = root / "counterexamples.json"
    previous = read(saved) if saved.exists() else {}
    results = {}
    for case in manifest["cases"]:
        variants = suite(plan, case)
        note = reviews.get(case, {})
        if (
            not variants
            or not note.get("answer_sql")
            or not note.get("counterexample_columns")
        ):
            continue
        journal = root / (case + ".jsonl")
        journal_hash = hashlib.sha256(journal.read_bytes()).hexdigest()
        if note["sha256"] != journal_hash:
            raise ValueError(f"{case} 复核与原始日志不符")
        events = [
            json.loads(s) for s in journal.read_text(encoding="utf8").splitlines()
        ]
        cid = next(e["cid"] for e in events if e["kind"] == "conversation")
        q = next(
            r
            for r in audit
            if r["id"] == note["answer_sql"]
            and str(r["conversation_id"]) == cid
            and r["status"] == "succeeded"
        )
        columns = note["counterexample_columns"]
        record = {
            "execution_id": q["id"],
            "binding": binding(
                case, events[0]["trial"], journal_hash, q, columns, variants
            ),
            "results": [],
        }
        old = previous.get(case)
        if check_record(old, record["binding"], columns, variants) in (
            "pass",
            "fail",
        ) and all(not r.get("error") and r.get("rows") for r in old["results"]):
            results[case] = old
            continue
        for spec, tables in variants:
            try:
                rows = await replay(q["raw_sql"], tables)
                evidence = {"rows": rows}
            except (ValueError, SQLAlchemyError) as exc:
                evidence = {"rows": [], "error": str(exc)[:450]}
            record["results"].append(
                {
                    **evidence,
                    "fixture": spec.get("fixture", "custom"),
                    "columns": columns,
                    "expected": spec["expected"],
                    "status": outcome(
                        evidence["rows"],
                        columns,
                        spec["expected"],
                        ranked_prefix=spec.get("ranked_prefix", False),
                        row_equals=spec.get("row_equals"),
                    ),
                }
            )
        results[case] = record
    save(root / "counterexamples.json", results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--group", default="first")
    args = parser.parse_args()
    asyncio.run(run(args.folder, args.group))
