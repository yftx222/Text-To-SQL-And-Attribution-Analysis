"""生成独立、可复现的新威产品售后演练库，运行十月复发查询和标准答案核对。"""

import argparse
import json
import random
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from dataagent.engines.query.ontology.recurrence import (
    SemanticQueryRequest,
    compile_query,
    summarize,
    utc_us,
)

CONF = Path(__file__).resolve().parents[2] / "conf/ontology"
LOCAL = timezone(timedelta(hours=8))
SCOPE = "demo-service"
OCTOBER = SemanticQueryRequest(
    operation="trace_recurrent_faults",
    window_start="2026-10-01T00:00:00+08:00",
    window_end="2026-11-01T00:00:00+08:00",
)
FAULTS = {
    "CONTACT": ("接触一致性复测", "通道端电压波动", "复核夹具接线并比对参考通道"),
    "COMMS": ("采集链路中断", "记录时间戳出现间断", "复核采集链路并回读中断前后记录"),
    "CURRENT": ("电流读数偏差", "设定值与标准表读数偏差", "使用参考仪表复测同一量程"),
}


def seed(db: sqlite3.Connection):
    definition = yaml.safe_load((CONF / "manufacturing_service.yaml").read_text("utf8"))
    for src in definition["sources"].values():
        fields = [f"{name} {dtype}" for name, dtype in src["columns"].items()]
        fields.append("PRIMARY KEY (" + ",".join(src["key"]) + ")")
        db.execute(f"CREATE TABLE {src['table']} ({','.join(fields)})")
    db.executescript("""
        CREATE TABLE service_ticket(scope_id TEXT,work_order_id TEXT,opened_us INTEGER,action TEXT,summary TEXT,channel INTEGER,set_voltage_v REAL,set_current_a REAL,PRIMARY KEY(scope_id,work_order_id));
        CREATE TABLE customer_detail(scope_id TEXT,customer_id TEXT,label TEXT,PRIMARY KEY(scope_id,customer_id));
    """)
    products = json.loads((CONF / "neware_products.json").read_text("utf8"))
    db.executemany(
        "INSERT INTO product VALUES (:model,:application,:max_voltage_v,:max_current_a,:current_ranges,:source)",
        products,
    )
    rng = random.Random(202610)
    golden = {
        "pairs": [],
        "devices": set(),
        "customers": set(),
        "unknown_customer_events": 0,
        "unknown_recurrence_records": 0,
    }
    customers = [
        "材料研发实验室",
        "聚合物电芯中试中心",
        "储能电芯验证中心",
        "检测认证实验室",
    ]
    scope = SCOPE

    def add(table, *values):
        db.execute(
            f"INSERT INTO {table} VALUES ("
            + ",".join("?" for _ in (scope, *values))
            + ")",
            (scope, *values),
        )

    def episode(
        device,
        i,
        readings,
        suffix,
        when,
        category,
        *,
        closed=None,
        history=1,
        extra_order=False,
    ):
        channel, voltage, current = readings
        eid = f"{device}-{suffix}"
        name, symptom, action = FAULTS[category]
        add(
            "mfg_fault_incident",
            "SRC-" + eid,
            eid,
            device,
            ("BTS-" if suffix == "A" else "FIELD-") + category,
            utc_us(when),
            history,
            1,
        )
        add("mfg_fault_lifecycle", eid + "-OPEN", eid, "open", utc_us(when))
        if closed:
            add("mfg_fault_lifecycle", eid + "-CLOSE", eid, "closed", utc_us(closed))
        for part in range(2 if extra_order else 1):
            order = f"WO-{i:04}-{suffix}-{part + 1}"
            add("mfg_work_order", order, eid)
            add(
                "service_ticket",
                order,
                utc_us(when + timedelta(minutes=15 + part * 70)),
                action if part else name,
                symptom,
                channel,
                voltage,
                current,
            )
        return eid

    for scope in (SCOPE, "demo-other"):
        for cid in range(1, 13):
            add("mfg_customer", f"C{cid:02}")
            add(
                "customer_detail",
                f"C{cid:02}",
                f"试点{customers[(cid - 1) % 4]}-{cid:02}",
            )
        for family in FAULTS:
            for source in ("BTS", "FIELD"):
                add("mfg_fault_category", source + "-" + family, family)
        for i in range(1, 73 if scope == SCOPE else 9):
            device = f"BAT-{i:04}"
            customer = f"C{(i - 1) % 12 + 1:02}"
            model = rng.choice(products)
            installed = datetime(2024, 5, 1, tzinfo=LOCAL) + timedelta(
                days=rng.randrange(630)
            )
            add("mfg_device", device)
            add(
                "asset_detail",
                device,
                model["model"],
                f"SIM-{i:06}",
                installed.date().isoformat(),
                f"{model['application']}区-{(i - 1) % 4 + 1}",
            )
            family = rng.choice(list(FAULTS))
            channel = rng.randint(1, 8)
            voltage = round(rng.uniform(3.4, 4.25), 2)
            current = round(model["max_current_a"] * rng.uniform(0.18, 0.75), 4)
            # 有设备整期无报修；剩余样本按场景分层，故障占比不代表厂商质量。
            scenario = i % 9
            n = datetime(
                2026,
                10,
                rng.randrange(7, 27),
                rng.randrange(8, 18),
                rng.randrange(60),
                tzinfo=LOCAL,
            )
            handover = n - timedelta(days=2)
            if scenario not in (4, 5):
                add(
                    "mfg_device_customer_relation",
                    "REL-" + device,
                    device,
                    customer,
                    "using_customer",
                    utc_us(installed),
                    utc_us(handover) if scenario == 3 else None,
                )
            if scenario == 3:
                customer = f"C{(i + 4) % 12 + 1:02}"
                add(
                    "mfg_device_customer_relation",
                    "TRANSFER-" + device,
                    device,
                    customer,
                    "using_customer",
                    utc_us(handover),
                    None,
                )
            elif scenario == 5:
                for j in (0, 1):
                    add(
                        "mfg_device_customer_relation",
                        f"CONFLICT-{i}-{j}",
                        device,
                        f"C{j + 1:02}",
                        "using_customer",
                        utc_us(handover),
                        None,
                    )
            if scenario == 0:
                continue
            prior_close = n - timedelta(
                days=rng.randint(3, 19) if scenario != 6 else 31, hours=rng.randrange(5)
            )
            prior = episode(
                device,
                i,
                (channel, voltage, current),
                "A",
                prior_close - timedelta(days=rng.randint(1, 4)),
                family,
                closed=None if scenario == 7 else prior_close,
                extra_order=(i % 3 == 0),
            )
            new = episode(
                device,
                i,
                (channel, voltage, current),
                "B",
                n,
                family,
                closed=n + timedelta(hours=rng.randint(2, 35)),
                history=int(scenario != 8),
                extra_order=(i % 2 == 0),
            )
            if scope == SCOPE and scenario in (1, 2, 3, 4, 5):
                golden["pairs"].append([prior, new])
                golden["devices"].add(device)
                if scenario in (4, 5):
                    golden["unknown_customer_events"] += 1
                else:
                    golden["customers"].add(customer)
            if scope == SCOPE and scenario == 8:
                golden["unknown_recurrence_records"] += 1
    db.commit()
    return golden


def check(db, golden):
    db.row_factory = sqlite3.Row
    result = summarize(
        OCTOBER,
        db.execute(compile_query(OCTOBER, authorized_scope=SCOPE, dialect="sqlite")),
    )
    assert sorted(result["event_pairs"]) == sorted(golden["pairs"])
    assert (
        set(result["devices"]) == golden["devices"]
        and set(result["customers"]) == golden["customers"]
    )
    for k in ("unknown_customer_events", "unknown_recurrence_records"):
        assert result["summary"][k] == golden[k]
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("database", type=Path, help="写入新的 SQLite 试点数据库")
    args = p.parse_args()
    # 使用新目录/新文件名，避免覆盖已经运行过的试点。
    args.database.parent.mkdir(parents=True, exist_ok=True)
    args.database.touch(exist_ok=False)
    with closing(sqlite3.connect(args.database)) as db:
        golden = seed(db)
        result = check(db, golden)
        print(json.dumps(result["summary"], ensure_ascii=False))
        args.database.with_suffix(".evidence.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), "utf8"
        )


if __name__ == "__main__":
    main()
