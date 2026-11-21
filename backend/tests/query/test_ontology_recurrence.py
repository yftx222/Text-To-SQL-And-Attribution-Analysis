"""在实际处理器上逐项检查方案 T01–T40。fixture 中的预期是独立业务断言。"""

import sqlite3
from datetime import datetime

import pytest
import yaml
from pydantic import ValidationError

from dataagent.engines.query.ontology.recurrence import (
    SemanticQueryRequest,
    compile_query,
    summarize,
    utc_us,
)
from scripts.development.manufacturing_pilot import CONF, OCTOBER, check, seed


def ts(value):
    return utc_us(datetime.fromisoformat("2026-" + value + "+08:00"))


@pytest.fixture
def fixture():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    specs = yaml.safe_load((CONF / "manufacturing_service.yaml").read_text("utf8"))[
        "sources"
    ]
    for s in specs.values():
        db.execute(
            "CREATE TABLE "
            + s["table"]
            + " ("
            + ",".join(k + " " + v for k, v in s["columns"].items())
            + ")"
        )

    def add(table, *values):
        db.execute(
            "INSERT INTO mfg_"
            + table
            + " VALUES ("
            + ",".join("?" for _ in range(len(values) + 1))
            + ")",
            ("s", *values),
        )

    add("device", "D1")
    add("device", "D2")
    for customer in ("C1", "C2"):
        add("customer", customer)
    for raw, cat in (("CODE-A", "F1"), ("CODE-B", "F2"), ("FIELD-A", "F1")):
        add("fault_category", raw, cat)
    add("fault_incident", "P", "P", "D1", "CODE-A", ts("09-24T09:00"), 1, 1)
    add("fault_incident", "N", "N", "D1", "CODE-A", ts("10-10T10:00"), 1, 1)
    add("fault_lifecycle", "P1", "P", "closed", ts("09-25T10:00"))
    add(
        "device_customer_relation",
        "R1",
        "D1",
        "C1",
        "using_customer",
        ts("01-01T00:00"),
        None,
    )
    try:
        yield db, add
    finally:
        db.close()


@pytest.mark.parametrize("case", range(1, 41), ids=lambda i: f"T{i:02}")
def test_rule(case, fixture):
    db, add = fixture
    request = OCTOBER

    def mod(field, value, record="N"):
        db.execute(
            f"UPDATE mfg_fault_incident SET {field}=? WHERE record_id=?",
            (value, record),
        )

    def close(at):
        db.execute(
            "UPDATE mfg_fault_lifecycle SET effective_us=? WHERE transition_id='P1'",
            (at,),
        )

    reason = "MATCHED"
    wanted = [["P", "N"]]
    customers = ["C1"]
    if case in (2, 29):
        mod("raw_code", "CODE-B")
        reason = "NO_PREDECESSOR"
    elif case in (3, 27):
        mod("device_id", "D2")
        reason = "NO_PREDECESSOR"
    elif case == 4:
        for row in db.execute("SELECT * FROM mfg_fault_incident").fetchall():
            add(
                "fault_incident",
                row["record_id"] + "2",
                row["event_id"] + "2",
                "D2",
                row["raw_code"],
                row["occurred_us"],
                1,
                1,
            )
        add("fault_lifecycle", "P2C", "P2", "closed", ts("09-25T10:00"))
        add(
            "device_customer_relation",
            "R2",
            "D2",
            "C1",
            "using_customer",
            ts("01-01T00:00"),
            None,
        )
        wanted.append(["P2", "N2"])
    elif case in (6, 10):
        mod(
            "occurred_us",
            ts("10-01T00:00")
            if case == 6
            else utc_us(datetime.fromisoformat("2026-09-30T16:00:00Z")),
        )
    elif case == 7:
        mod("occurred_us", ts("11-01T00:00"))
        wanted = []
    elif case == 9:
        mod("occurred_us", ts("11-01T00:00") - 1)
        close(ts("10-15T00:00"))
    elif case in (11, 12):
        close(ts("10-01T10:00"))
        mod("occurred_us", ts("10-31T10:00") + (case == 12))
        reason = "OUTSIDE_INTERVAL" if case == 12 else reason
    elif case == 13:
        close(ts("10-10T10:00"))
        reason = "OUTSIDE_INTERVAL"
    elif case == 14:
        close(ts("10-11T10:00"))
        reason = "PREDECESSOR_OPEN"
    elif case == 15:
        mod("occurred_us", ts("10-25T09:59:59"))
    elif case in (16, 17, 20):
        add(
            "fault_incident",
            "Q",
            "Q",
            "D1",
            "CODE-A",
            ts("09-24T09:00") if case == 20 else ts("09-27T10:00"),
            1,
            1,
        )
        if case == 16:
            add("fault_lifecycle", "Q1", "Q", "closed", ts("09-30T10:00"))
            wanted = [["Q", "N"]]
        else:
            reason = "PREDECESSOR_AMBIGUOUS" if case == 20 else "PREDECESSOR_OPEN"
    elif case == 18:
        add("fault_incident", "K", "K", "D1", "CODE-A", ts("10-19T10:00"), 1, 1)
        add("fault_lifecycle", "N1", "N", "closed", ts("10-13T10:00"))
        wanted.append(["N", "K"])
    elif case == 19:
        db.execute("DELETE FROM mfg_fault_incident WHERE event_id='P'")
        reason = "NO_PREDECESSOR"
    elif case in (21, 22, 23):
        for i in range(3):
            add("work_order", f"W{i}", "N" if case == 21 else "P")
        if case == 23:
            db.execute("DELETE FROM mfg_fault_incident WHERE event_id='N'")
            add("fault_lifecycle", "P2", "P", "reopened", ts("10-05T00:00"))
            wanted = []
    elif case == 24:
        mod("event_id", None)
        reason = "EVENT_IDENTITY_UNRESOLVED"
    elif case == 25:
        add("fault_lifecycle", "P2", "P", "reopened", ts("10-08T10:00"))
        add("fault_lifecycle", "P3", "P", "closed", ts("10-09T10:00"))
    elif case == 26:
        # 视图中的 device_id 已按可信资产对照标准化；两套源记录仍有独立证据 ID。
        mod("record_id", "ERP-104", "P")
        mod("record_id", "FIELD-902")
    elif case in (28, 30):
        mod("raw_code", "FIELD-A" if case == 28 else "UNMAPPED")
        reason = reason if case == 28 else "FAULT_UNMAPPED"
    elif case == 31:
        close(None)
        reason = "CLOSE_TIME_MISSING"
    elif case == 32:
        mod("history_complete", 0)
        reason = "HISTORY_INCOMPLETE"
    elif case == 33:
        db.execute(
            "UPDATE mfg_device_customer_relation SET valid_to_us=?",
            (ts("10-10T10:00"),),
        )
        add(
            "device_customer_relation",
            "R2",
            "D1",
            "C2",
            "using_customer",
            ts("10-10T10:00"),
            None,
        )
        customers = ["C2"]
    elif case in (34, 35):
        if case == 34:
            db.execute("DELETE FROM mfg_device_customer_relation")
        else:
            add(
                "device_customer_relation",
                "R2",
                "D1",
                "C2",
                "using_customer",
                ts("10-01T00:00"),
                None,
            )
        customers = []
    elif case in (36, 37, 38):
        with pytest.raises(ValidationError, match="AS_OF_UNSUPPORTED"):
            SemanticQueryRequest(
                **request.model_dump(exclude={"as_of"}),
                as_of="2026-11-02T00:00:00+08:00",
            )
        return
    elif case == 40:
        db.execute("UPDATE mfg_fault_incident SET scope_id='other' WHERE event_id='N'")
        wanted = []
    evidence = [
        dict(r)
        for r in db.execute(
            compile_query(request, authorized_scope="s", dialect="sqlite")
        )
    ]
    if case == 39:
        evidence[0]["prior_event"] = None
        with pytest.raises(ValueError, match="证据"):
            summarize(request, evidence)
        return
    result = summarize(request, evidence)
    if reason != "MATCHED":
        assert result["evidence"][-1]["reason"] == reason
        wanted = []
    assert sorted(result["event_pairs"]) == sorted(wanted)
    assert result["customers"] == (customers if wanted else [])
    assert result["summary"]["events"] == len(wanted)
    if case == 4:
        assert result["summary"]["devices"] == 2
    if case == 25:
        assert result["evidence"][-1]["elapsed_us"] == 86400_000000
    if case in (34, 35):
        assert (
            not result["complete"] and result["summary"]["unknown_customer_events"] == 1
        )


def test_independent_device_ledger():
    with sqlite3.connect(":memory:") as db:
        gold = seed(db)
        result = check(db, gold)
        assert result["summary"]["events"] == 40
        assert not result["complete"]
