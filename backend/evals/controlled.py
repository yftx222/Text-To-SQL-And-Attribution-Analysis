"""Prepare isolated boundary data and versioned Reviewer inputs."""

import argparse
import csv
import html
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from .catalog import catalog
from .offline_score import HERE, read


def check_fixture(case, sql=None):
    with closing(sqlite3.connect(":memory:")) as db:
        for name, (columns, *records) in case["tables"].items():
            if not all(s.isidentifier() for s in (name, *columns)):
                raise ValueError("样本表名或列名不合法")
            cols = ",".join(f'"{k}"' for k in columns)
            db.execute(f'CREATE TABLE "{name}" ({cols})')
            placeholders = ",".join("?" for _ in columns)
            db.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', records)
        db.execute("PRAGMA query_only=ON")
        db.set_authorizer(
            lambda op, *_: (
                sqlite3.SQLITE_OK
                if op
                in (
                    sqlite3.SQLITE_READ,
                    sqlite3.SQLITE_SELECT,
                    sqlite3.SQLITE_FUNCTION,
                    sqlite3.SQLITE_RECURSIVE,
                )
                else sqlite3.SQLITE_DENY
            )
        )
        db.set_progress_handler(iter([0] * 1000 + [1]).__next__, 1000)
        return [
            list(r) for r in db.execute(sql or case["reference_sql"]).fetchmany(1001)
        ]


def review_materials(target):
    # Keep the answer key outside user-facing material.
    root = target / "review_inputs"
    root.mkdir(exist_ok=True)
    channels = {
        k: Decimal(v)
        for k, v in read(HERE / "scorecards.json")["ATT-01"].items()
        if k in {"APP", "PC", "SEM", "H5", "MINI_PROGRAM"}
    }
    with (root / "channel_evidence.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["channel", "delta_cny"])
        writer.writerows(channels.items())
        writer.writerow(["ALL", sum(channels.values())])
    (root / "delivery_evidence.csv").write_text(
        "metric,value,unit\naverage_delivery_time,35.57,hour\n", encoding="utf-8"
    )
    for case, versions in {
        "REV-01": 1,
        "REV-02": 1,
        "REV-03": 1,
        "REV-04": 1,
        "REV-05": 1,
        "REV-06": 2,
    }.items():
        for v in range(1, versions + 1):
            values = channels.copy()
            if case == "REV-02" or (case == "REV-06" and v == 1):
                values["PC"] = Decimal("-40000.00")
            total = Decimal("-30000.00") if case == "REV-01" else sum(channels.values())
            statement = f"下半月比上半月少了 {abs(total):,.2f} 元。"
            if case == "REV-04":
                statement += "活动结束导致全站下单额下降。"
            rows = "".join(
                f"<tr><td>{key}</td><td>{x:,.2f}</td></tr>" for key, x in values.items()
            )
            bars = "".join(
                f"<li>{k} <meter min='0' max='50000' value='{abs(x)}'></meter> {x:+,.2f} 元</li>"
                for k, x in values.items()
            )
            body = f"<h1>九月下单 GMV 变化</h1><p>{html.escape(statement)}</p><table><tr><th>渠道</th><th>变化额（元）</th></tr>{rows}</table><ul>{bars}</ul>"
            if case == "REV-03":
                body = "<h1>九月正向包裹平均配送时长</h1><p>平均配送时长（分钟）</p><meter min='0' max='60' value='35.57'></meter> 35.57"
            (root / f"{case}-v{v}.html").write_text(
                "<!doctype html><meta charset='utf-8'>" + body, encoding="utf-8"
            )


def prepare(target):
    target.mkdir(parents=True, exist_ok=True)
    fixtures = read(HERE / "controlled_fixtures.json")
    result = []
    for key, case in fixtures.items():
        observed = check_fixture(case)
        if observed != case["expected_rows"]:
            raise ValueError(f"{key} 样本与独立参照不符：{observed!r}")
        result.append({"case": key, "observed": observed, "reference_pass": True})
    review_materials(target)
    for key, row in catalog().items():
        if key.split("-")[0] in ("ACL", "RUN", "REV"):
            result.append(
                {
                    "case": key,
                    "setup": row["scope"],
                    "criteria": row["rubric"],
                    "materials": sorted(
                        p.name for p in (target / "review_inputs").glob(key + "-*.html")
                    ),
                    "live_pass": None,
                }
            )
    (target / "inventory.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    prepare(args.out)
