"""Independent synthetic reference for two proposed service metrics. No external services."""

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def us(date):
    return (
        int(datetime.fromisoformat(date + "+08:00").astimezone(UTC).timestamp())
        * 1_000_000
    )


# Keep customer ownership fixed in this fixture; the same customer code in a
# different service scope is deliberately present. Grants belong to the principal.
FIRST_FAULT = """
WITH observed AS (
  SELECT d.device, d.accepted, MIN(f.occurred) AS first_fault
  FROM delivery d LEFT JOIN fault f ON f.scope=d.scope AND f.device=d.device
    AND f.occurred>=d.accepted AND f.occurred<:end
  WHERE d.scope=:scope AND d.accepted>=:start AND d.accepted<:end
    AND (:customer IS NULL OR d.customer=:customer)
    AND EXISTS (SELECT 1 FROM grants g WHERE g.principal=:principal
                AND g.scope=d.scope AND g.customer=d.customer)
  GROUP BY d.device,d.accepted
)
SELECT COUNT(*) devices, COUNT(first_fault) observed,
       COUNT(*)-COUNT(first_fault) without_fault,
       AVG((first_fault-accepted)/3600000000.0) mean_hours FROM observed
"""
ON_TIME = """
WITH eligible AS (
  SELECT w.id,w.due,
    (SELECT s.state FROM transition s WHERE s.scope=w.scope AND s.id=w.id
      AND s.ts<:end ORDER BY s.ts DESC,s.seq DESC LIMIT 1) AS final_state,
    (SELECT s.ts FROM transition s WHERE s.scope=w.scope AND s.id=w.id
      AND s.ts<:end ORDER BY s.ts DESC,s.seq DESC LIMIT 1) AS final_ts
  FROM work_order w WHERE w.scope=:scope AND w.due>=:start AND w.due<:end
    AND (:customer IS NULL OR w.customer=:customer)
    AND EXISTS (SELECT 1 FROM grants g WHERE g.principal=:principal
                AND g.scope=w.scope AND g.customer=w.customer)
), counted AS (
  SELECT COUNT(*) AS due, COUNT(CASE WHEN final_state='closed' AND final_ts<=due
    THEN 1 END) AS on_time FROM eligible WHERE COALESCE(final_state,'open')!='cancelled'
)
SELECT due,on_time,1.0*on_time/NULLIF(due,0) AS fraction FROM counted
"""


def fixture(db):
    db.executescript("""
    CREATE TABLE grants(principal,scope,customer,PRIMARY KEY(principal,scope,customer));
    CREATE TABLE delivery(scope,customer,device,accepted INTEGER,PRIMARY KEY(scope,device));
    CREATE TABLE fault(scope,device,occurred INTEGER);
    CREATE TABLE work_order(scope,customer,id,due INTEGER,PRIMARY KEY(scope,id));
    CREATE TABLE transition(scope,id,ts INTEGER,seq INTEGER,state,PRIMARY KEY(scope,id,ts,seq));
    INSERT INTO grants VALUES ('pilot','A','C01'),('pilot','A','C02');
    """)
    for scope, customer, device, date in [
        ("A", "C01", "D1", "09-01"),
        ("A", "C01", "D2", "09-02"),
        ("A", "C02", "D3", "09-03"),
        ("A", "C03", "D4", "09-04"),
        ("B", "C01", "D1", "09-01"),
    ]:
        db.execute(
            "INSERT INTO delivery VALUES(?,?,?,?)",
            (scope, customer, device, us("2026-" + date + "T00:00")),
        )
    for scope, device, date in [
        ("A", "D1", "08-31"),
        ("A", "D1", "09-02"),
        ("A", "D1", "09-04"),
        ("A", "D2", "10-01"),
        ("A", "D3", "09-03"),
        ("A", "D4", "09-05"),
        ("B", "D1", "09-01"),
    ]:
        db.execute(
            "INSERT INTO fault VALUES(?,?,?)",
            (scope, device, us("2026-" + date + "T00:00")),
        )
    for scope, customer, ident, date in [
        ("A", "C01", "W1", "09-01"),
        ("A", "C01", "W2", "09-15"),
        ("A", "C01", "W3", "09-20"),
        ("A", "C01", "W4", "10-01"),
        ("A", "C01", "W5", "09-10"),
        ("A", "C03", "W6", "09-15"),
        ("B", "C01", "W1", "09-01"),
    ]:
        db.execute(
            "INSERT INTO work_order VALUES(?,?,?,?)",
            (scope, customer, ident, us("2026-" + date + "T00:00")),
        )
    for ident, date, state in [
        ("W1", "09-01", "closed"),
        ("W1", "10-01", "open"),
        ("W2", "09-16", "closed"),
        ("W3", "09-19", "closed"),
        ("W3", "09-20", "open"),
        ("W5", "09-01", "cancelled"),
    ]:
        db.execute(
            "INSERT INTO transition VALUES('A',?,?,1,?)",
            (ident, us("2026-" + date + "T00:00"), state),
        )


FIRST_ALL = {"devices": 3, "observed": 2, "without_fault": 1, "mean_hours": 12}
FIRST_C01 = {"devices": 2, "observed": 1, "without_fault": 1, "mean_hours": 24}
SLA = {"due": 3, "on_time": 1, "fraction": 1 / 3}
EMPTY = {"due": 0, "on_time": 0, "fraction": None}

# (user turn, metric, requested customer, hand-calculated expected values)
CARDS = {
    "interval": [
        (
            "九月验收的可见设备，截至月底首次故障平均相隔多少小时？同时给出有故障、无故障台数。",
            "fault",
            None,
            FIRST_ALL,
        )
    ],
    "interval_followup": [
        ("查九月设备验收后首次故障间隔及样本量。", "fault", None, FIRST_ALL),
        ("只看 C01，保留原观察期。", "fault", "C01", FIRST_C01),
    ],
    "interval_boundary": [
        (
            "只看 C02，验收当时就发生故障的设备也计入。",
            "fault",
            "C02",
            {"devices": 1, "observed": 1, "without_fault": 0, "mean_hours": 0},
        )
    ],
    "interval_denied": [
        ("看 C03 九月首次故障间隔。", "fault", "C03", {"status": "scope_denied"})
    ],
    "sla": [("九月到期的维修工单按期关闭率是多少？给出分子分母。", "sla", None, SLA)],
    "sla_followup": [
        ("按九月到期工单看按期关闭率。", "sla", None, SLA),
        ("只看 C02。", "sla", "C02", EMPTY),
    ],
    "sla_boundary": [
        (
            "只看 C01 九月到期工单，按月底有效状态核算，含恰好到期关闭的工单。",
            "sla",
            "C01",
            SLA,
        )
    ],
    "sla_denied": [
        (
            "看同服务域 C03 的九月工单按期关闭率。",
            "sla",
            "C03",
            {"status": "scope_denied"},
        )
    ],
}


def reference(db, metric, customer, *, scope="A", principal="pilot"):
    args = dict(
        scope=scope,
        principal=principal,
        customer=customer,
        start=us("2026-09-01T00:00"),
        end=us("2026-10-01T00:00"),
    )
    grant = db.execute(
        "SELECT 1 FROM grants WHERE principal=:principal AND scope=:scope AND (:customer IS NULL OR customer=:customer)",
        args,
    )
    if grant.fetchone() is None:
        return {"status": "scope_denied"}
    return dict(
        db.execute({"fault": FIRST_FAULT, "sla": ON_TIME}[metric], args).fetchone()
    )


def review_pack():
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        fixture(db)
        result = {}
        for key, turns in CARDS.items():
            result[key] = []
            for question, metric, customer, expected in turns:
                actual = reference(db, metric, customer)
                if actual != expected:
                    raise ValueError(
                        f"{key} 独立 SQL 与手算不符：{actual} / {expected}"
                    )
                result[key].append(
                    dict(
                        question=question,
                        metric=metric,
                        customer=customer,
                        expected=expected,
                        reference_sql_result=actual,
                        business_reviewer=None,
                        decision="待业务复核",
                        rationale=None,
                    )
                )
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = review_pack()
    with args.output.open("x", encoding="utf8") as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
