"""Verify the five manufacturing pilot chats against their registered event pairs."""

import argparse
import hashlib
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from dotenv import dotenv_values
from fastapi.testclient import TestClient

from dataagent.engines.common.async_runtime import create_event_loop
from dataagent.engines.query.ontology.registry import digest
from scripts.development.manufacturing_pilot import seed

from .live_report import read, rows, save, totals, trace_trial


def within_requested_filters(actual, requested):
    """An optional narrower check may add filters, but must retain every user restriction."""
    return not requested or (bool(actual) and set(actual) <= set(requested))


def verify_turn(client, headers, cid, case, index, turn, tools, expected, owners):
    expected = {tuple(pair) for pair in expected}
    customer = {"CUSTOMER": "C07", "OUTSIDE": "C01"}.get(case)
    if case == "FOLLOWUP" and index:
        customer = "C07"
    devices = ["BAT-0028"] if case == "ASSET" else []
    calls = []
    for tool in tools:
        if tool["turn"] != index or tool["name"] != "execute_semantic_query":
            continue
        result = tool.get("result")
        if not isinstance(result, dict) or result.get("status") != "success":
            continue
        m = result["manifest"]
        req = m["request"]
        eid = m["execution_id"]
        endpoint = f"/api/v1/chat/{cid}/ontology/{m['analysis_id']}/{eid}/evidence"
        response = client.get(endpoint, headers=headers)
        record = {
            "execution_id": eid,
            "request": req,
            "download_status": response.status_code,
        }
        calls.append(record)
        if response.status_code != 200:
            continue
        payload = response.json()
        data = payload["result"]
        pairs = [tuple(pair) for pair in data["event_pairs"]]
        expected_devices = {event.rsplit("-", 1)[0] for _, event in expected}
        expected_customers = {owners[device] for device in expected_devices}
        csv = client.get(endpoint + ".csv", headers=headers)
        checks = {
            "rule": m["operation"] == "trace_recurrent_faults"
            and m["ontology_revision"] == 2
            and m["semantic_validation_status"] == "passed"
            and m["audit_status"] == "recorded",
            "month": req["window_start"] == "2026-10-01T00:00:00+08:00"
            and req["window_end"] == "2026-11-01T00:00:00+08:00",
            "filters": req["customer_ids"] == ([customer] if customer else [])
            and req["device_ids"] == devices,
            "pairs": set(pairs) == expected
            and len(pairs) == len(expected) == data["summary"]["events"],
            "role_scope": set(data["customers"]) <= {"C04", "C07", "C10"}
            and all(
                e.get("customer_id") in {None, "C04", "C07", "C10"}
                for e in data.get("evidence", [])
            ),
            "attribution": set(data["devices"]) == expected_devices
            and set(data["customers"]) == expected_customers
            and data["summary"]["unknown_customer_events"] == 0
            and data["summary"]["unknown_recurrence_records"] == 0
            and all(
                any(
                    e["prior_event"] == a
                    and e["event_id"] == b
                    and e["customer_id"] == owners[b.rsplit("-", 1)[0]]
                    for e in data["evidence"]
                )
                for a, b in expected
            ),
            "evidence": payload["manifest"]["execution_id"] == eid
            and digest(data) == m["result_sha256"]
            and csv.status_code == 200
            and hashlib.sha256(csv.content).hexdigest()
            == payload["manifest"]["csv_sha256"],
            "anonymous_denied": client.get(endpoint).status_code == 401,
        }
        record.update(
            checks=checks,
            pairs=pairs,
            customers=data["customers"],
            devices=data["devices"],
        )
        if "window_basis" in payload["manifest"]:
            checks["recurrence_month"] = (
                payload["manifest"]["window_basis"] == "recurrence_occurred_at"
            )
            checks["customer_visibility"] = payload["manifest"].get(
                "unavailable_customer_ids"
            ) == (["C01"] if case == "OUTSIDE" else [])
    final = turn["answer"]
    pair_pattern = r"BAT-\d{4}-[A-Z]\b"
    answer_rows = [
        line
        for line in final.splitlines()
        if line.strip().startswith("|")
        and len(set(re.findall(pair_pattern, line))) >= 2
    ]
    mentioned = set(
        re.findall(pair_pattern, "\n".join(answer_rows) if answer_rows else final)
    )
    expected_ids = {eid for pair in expected for eid in pair}
    pairs_in_answer = mentioned == expected_ids
    if answer_rows:
        pairs_in_answer = pairs_in_answer and all(
            any(a in line and b in line for line in answer_rows) for a, b in expected
        )
    if not expected:
        pairs_in_answer = (
            pairs_in_answer
            and not re.search(r"BAT-\d{4}", final)
            and not (set(re.findall(r"C\d{2}(?!\d)", final)) - {customer})
            and bool(
                re.search(
                    r"无权|没有权限|不可见|无法判定|(?:当前|可见).{0,12}范围.{0,6}无法判断|不在.{0,16}(?:范围|可查询的客户目录)|依据.{0,12}不足",
                    final,
                )
            )
        )
    checks = {
        "completed": turn["status"] == "done",
        "answer_event_ids": pairs_in_answer,
        "all_semantic_calls_within_request": all(
            within_requested_filters(
                c["request"]["customer_ids"], [customer] if customer else []
            )
            and within_requested_filters(c["request"]["device_ids"], devices)
            for c in calls
        ),
        "all_downloaded_evidence_within_role": all(
            c.get("checks", {}).get("role_scope") for c in calls
        ),
        "audited_query": (case == "OUTSIDE" and not calls and pairs_in_answer)
        or any(
            all(c.get("checks", {}).values()) and bool(c.get("checks")) for c in calls
        ),
    }
    return {
        "turn": index + 1,
        "seconds": turn.get("seconds"),
        "checks": checks,
        "calls": calls,
        "expected_pairs": sorted(expected),
        "status": "pass" if all(checks.values()) else "review",
    }


def main(folder):
    from dataagent.app.app import app

    group = folder / "first"
    gold = read(folder / "expected_by_turn.json")
    creds = dotenv_values("conf/.env")
    cases = {}
    with closing(sqlite3.connect(":memory:")) as db:
        seed(db)
        owners = dict(
            db.execute(
                "SELECT device_id, customer_id FROM mfg_device_customer_relation WHERE scope_id='demo-service' AND role='using_customer' AND customer_id IN ('C04','C07','C10')"
            )
        )
    with TestClient(app, backend_options={"loop_factory": create_event_loop}) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={
                "identifier": creds["ADMIN_USERNAME"],
                "password": creds["ADMIN_PASSWORD"],
            },
        )
        login.raise_for_status()
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        assert (
            client.get("/api/v1/auth/me", headers=headers).json()["id"]
            == read(folder / "principal.json")["user_id"]
        )
        for case, expected in gold.items():
            events = rows(group / (case + ".jsonl"))
            turns, tools, _ = trace_trial(events)
            cid = next((e["cid"] for e in events if e["kind"] == "conversation"), None)
            if cid is None and not turns:
                cases[case] = []
                continue
            if len(turns) != len(expected) or events[-1]["kind"] != "closed":
                raise ValueError(f"{case} 未完成")
            cases[case] = [
                verify_turn(
                    client, headers, cid, case, i, t, tools, expected[i], owners
                )
                for i, t in enumerate(turns)
            ]
    report = {"cases": cases, "usage": totals(rows(folder / "telemetry.jsonl"))}
    save(folder / "result.json", report)
    lines = [
        "# 售后本体完整聊天专项",
        "",
        "| 场景 | 回合 | 正确事件对数 | 终答事件核对 | 工具范围与证据 | 耗时（秒） |",
        "|---|---:|---:|---|---|---:|",
    ]
    for case, turns in cases.items():
        if not turns:
            lines.append(f"| {case} | — | — | 未启动会话 | 无执行证据 | — |")
        for t in turns:
            checks = t["checks"]
            lines.append(
                f"| {case} | {t['turn']} | {len(t['expected_pairs'])} | {'通过' if checks['completed'] and checks['answer_event_ids'] else '需复核'} | {'通过' if checks['audited_query'] and checks['all_semantic_calls_within_request'] and checks['all_downloaded_evidence_within_role'] else '需复核'} | {t['seconds']:.1f} |"
            )
    lines.extend(
        [
            "",
            f"模型报告用量 {report['usage']['known_tokens']:,} Token。",
            "",
            "[逐回合执行编号、查询参数、标准答案和证据下载校验](result.json)",
        ]
    )
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    main(parser.parse_args().folder)
