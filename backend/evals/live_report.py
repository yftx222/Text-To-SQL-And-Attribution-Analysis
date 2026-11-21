"""Build trace-linked results from evidence produced by evals.live."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import text

from dataagent.engines.common.async_runtime import run_async
from dataagent.engines.contract.config import cfg
from dataagent.engines.infra.postgres_client_manager import PostgresClientManager

from .metrics import recall_metrics, summarize
from .offline_score import load_cards, read, score
from .pay_fixture import payment_counts
from .score_replay import TABLE_SNAPSHOT, numeric_facts


def rows(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.endswith("\n")]


def save(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    temp.replace(path)


def jsonl(path, data):
    path.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in data
        ),
        encoding="utf-8",
    )


async def fetch_audit(cids):
    pg = PostgresClientManager(cfg.meta_postgresql)
    try:
        async with pg.engine.connect() as con:
            result = await con.execute(
                text("""SELECT conversation_id, id, tool_call_id, analysis_id, session_id, role_name,
                status, purpose, raw_sql, result_summary, error_code, created_at FROM query_executions
                WHERE conversation_id = ANY(CAST(:ids AS uuid[])) ORDER BY created_at, id"""),
                {"ids": cids},
            )
            return [dict(r) for r in result.mappings()]
    finally:
        await pg.close()


async def fetch_rankings(cids):
    pg = PostgresClientManager(cfg.langgraph_postgresql)
    try:
        async with pg.engine.connect() as con:
            records = await con.execute(
                text("""SELECT conversation_id, recall_id, query, updated_at, response
                FROM semantic_recall_snapshots WHERE conversation_id=ANY(CAST(:ids AS uuid[]))
                ORDER BY updated_at,recall_id"""),
                {"ids": cids},
            )
            output = []
            for cid, rid, query, updated, response in records:
                data = response["semantic_resources"]
                direct = [
                    [c["t_name"], c["name"]]
                    for c in data["columns"]
                    if "direct_match" in c["inclusion_reasons"]
                ]
                output.append(
                    {
                        "cid": str(cid),
                        "id": rid,
                        "query": query,
                        "updated_at": updated.isoformat(),
                        "column": direct,
                        "metric": [m["name"] for m in data["metrics"]],
                    }
                )
            return output
    finally:
        await pg.close()


def answer(message):
    if message.get("role") == "assistant" and all(
        p["type"] != "tool_call" for p in message["parts"]
    ):
        return "\n".join(p["text"] for p in message["parts"] if p["type"] == "text")
    return ""


def trace_trial(raw):
    starts = [r for r in raw if r["kind"] == "turn_start"]
    turns = [
        {
            "question": r["question"],
            "trace": r["trace"],
            "status": "unknown",
            "answer": "",
        }
        for r in starts
    ]
    tools, hist = {}, []
    for n, row in enumerate(raw, 1):
        if row["kind"] == "history":
            hist = row["data"]
        if row["kind"] == "turn_end":
            turns[row["turn"]].update(status=row["status"], seconds=row["seconds"])
        if row["kind"] != "event":
            continue
        ev = row["data"]
        msg = ev.get("message", {})
        if ev["type"] == "message" and msg.get("role") == "assistant":
            turns[row["turn"]].update(answer=answer(msg), line=n)
        for p in msg.get("parts", []):
            if p["type"] not in ("tool_call", "tool_result"):
                continue
            key = (row["turn"], ev.get("delegation_id"), p["tool_call_id"])
            tool = tools.setdefault(
                key,
                {
                    "turn": row["turn"],
                    "name": p["name"],
                    "call": p["tool_call_id"],
                    "role": ev.get("agent_type", "planner"),
                    "delegation": ev.get("delegation_id"),
                },
            )
            if p["type"] == "tool_call":
                tool.update(args=p["args"], call_line=n)
            else:
                value = p.get("content")
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                tool.update(result=value, result_line=n)
    return turns, list(tools.values()), hist


def totals(traces):
    finished = {
        r["run"]: r for r in traces if r["event"] in ("model_end", "model_error")
    }
    starts = {r["run"]: r for r in traces if r["event"] == "model_start"}
    usages = [r["usage"] for r in finished.values() if r.get("usage")]
    http = [r for r in traces if r["event"] == "model_http"]
    return {
        "model_calls": len(starts),
        "physical_requests": len(http) if traces else None,
        "model_http_outcomes": dict(Counter(r["outcome"] for r in http)),
        "usage_reported": len(usages),
        "known_tokens": sum(u["total_tokens"] for u in usages),
        "total_tokens": sum(u["total_tokens"] for u in usages)
        if starts and len(usages) == len(starts)
        else None,
        "roles": dict(Counter(r["role"] for r in starts.values())),
        "recall_channels": sum(r["event"] == "recall_channel" for r in traces),
    }


def build(folder, group, audit, frozen):
    root = folder / group
    manifest = read(root / "manifest.json")
    cards = load_cards()
    if manifest.get("rubric_cases"):
        from .catalog import catalog

        for key, row in catalog().items():
            if key not in cards and row["messages"]:
                cards[key] = {
                    "id": key,
                    "turns": row["messages"],
                    "facts": {"rubric_met": True},
                }
    trace_rows = defaultdict(list)
    for r in rows(folder / "telemetry.jsonl"):
        trace_rows[r["trace"]].append(r)
    notes = read(root / "reviews.json") if (root / "reviews.json").exists() else {}
    rankings = read(root / "rankings.json") if (root / "rankings.json").exists() else []
    trials, scores = [], []
    for case in manifest["cases"]:
        path = root / (case + ".jsonl")
        data = rows(path)
        if not data:
            continue
        ref = path.name
        turns, tools, history = trace_trial(data)
        cid = next((r["cid"] for r in data if r["kind"] == "conversation"), None)
        trial = data[0]["trial"]
        first_text = turns[-1]["answer"] if turns else ""
        final, source = first_text, ref + "#stream"
        # Only recover a terminal message following the last requested user message.
        if not final and history and turns:
            users = [m for m in history if m.get("role") == "user"]
            if users and any(
                p.get("text") == turns[-1]["question"] for p in users[-1]["parts"]
            ):
                final, source = answer(history[-1]), ref + "#history"
        facts = numeric_facts(case, final, source) if frozen else {}
        complete = bool(
            turns
            and len(turns) == len(cards[case]["turns"])
            and all(t["status"] == "done" and t["answer"] for t in turns)
        )
        closed = any(r["kind"] == "closed" for r in data)
        if closed:
            facts["first_delivery"] = (complete, ref + "#turn_end")
        my_sql = [r for r in audit if str(r["conversation_id"]) == cid]
        my_downloads = [r for r in data if r["kind"] == "download"]
        if case == "ATT-01" and closed:
            latest = my_downloads[-1] if my_downloads else {}
            paths = {
                a["f_path"]
                for m in history
                for a in m.get("attachments") or []
                if a.get("delivery_status") == "verified"
            }
            receipts = [
                t["result"]
                for t in tools
                if t["name"] == "publish_artifact"
                and isinstance(t.get("result"), dict)
                and t["result"].get("status") == "verified"
            ]
            attached = bool(
                receipts
                and receipts[-1].get("receipt_id") == latest.get("receipt")
                and receipts[-1].get("f_path") in paths
            )
            downloadable = bool(
                attached
                and latest.get("verified")
                and (root / latest["file"]).is_file()
                and hashlib.sha256((root / latest["file"]).read_bytes()).hexdigest()
                == latest["sha256"]
            )
            original = (
                next((r for r in my_downloads if r["receipt"] == latest["receipt"]), {})
                if latest
                else {}
            )
            facts["first_delivery"] = (
                complete and downloadable and bool(original.get("verified")),
                ref + "#stream,download",
            )
            facts["delivered"] = (downloadable, ref + "#download")
        note = notes.get(case, {})
        if note:
            if (
                note["trial"] != trial
                or note["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest()
            ):
                raise ValueError(f"{case} 复核标注绑定的原始证据发生变化")
            sql_ids = {str(s["id"]) for s in my_sql}
            for key, (value, evidence) in note["facts"].items():
                if key not in {*cards[case]["facts"], "sql_scope"} or not evidence:
                    raise ValueError(f"{case} 标注缺少证据或字段未知")
                if key == "sql_scope" and not any(sid in evidence for sid in sql_ids):
                    raise ValueError(f"{case} SQL 口径需要本次真实执行 ID")
                if frozen:
                    facts[key] = (value, evidence)
            if case == "PAY-04" and note.get("answer_sql"):
                query = next(r for r in my_sql if str(r["id"]) == note["answer_sql"])
                try:
                    counts = payment_counts(
                        query["raw_sql"], columns=note["pay_columns"]
                    )
                except (ValueError, sqlite3.Error):
                    counts = None
                facts["month_end_counts"] = (counts, "audit.json#" + str(query["id"]))
                if counts is not None and counts != [1, 2]:
                    facts["sql_scope"] = (False, "audit.json#" + str(query["id"]))
        scored, business, strict = score(cards[case], trial, facts)
        scores.extend(scored)
        traces = [r for t in turns for r in trace_rows[t["trace"]]]
        submissions = [
            t for t in tools if t["name"] == "submit_task_result" and "args" in t
        ]
        executed = {t["call"]: t for t in tools if t["name"] == "execute_sql"}
        links = []
        for record in my_sql:
            sid = str(record["id"])
            attempt = executed.get(record["tool_call_id"], {})
            result = attempt.get("result")
            links.append(
                {
                    "execution_id": sid,
                    "tool_call_id": record["tool_call_id"],
                    "status": record["status"],
                    "role": record["role_name"],
                    "turn": attempt.get("turn"),
                    "delegation": attempt.get("delegation"),
                    "call_line": attempt.get("call_line"),
                    "result_line": attempt.get("result_line"),
                    "result_matches_id": result.get("execution_id") == sid
                    if isinstance(result, dict)
                    else None,
                    "submit_evidence_lines": [
                        t["call_line"]
                        for t in submissions
                        if sid in json.dumps(t["args"])
                    ],
                }
            )
        trial_record = {
            "case": case,
            "trial": trial,
            "cid": cid,
            "started": bool(turns),
            "closed": closed,
            "business": business,
            "first_correct_delivery": strict,
            "seconds": round(sum(t.get("seconds", 0) for t in turns), 2),
            "sql": dict(Counter(r["status"] for r in my_sql)),
            "tool_counts": dict(Counter(t["name"] for t in tools)),
            "usage": totals(traces),
            "turns": turns,
            "final": final,
            "deliveries": my_downloads,
            "recall": recall_metrics(
                case,
                traces,
                tools=tools,
                snapshots=[s for s in rankings if s["cid"] == cid],
            ),
            "sql_links": links,
        }
        trials.append(trial_record)
    jsonl(root / "trials.jsonl", trials)
    jsonl(root / "scores.jsonl", scores)
    metrics = summarize(trials, scores)
    save(root / "metrics.json", metrics)
    started = [t for t in trials if t["started"]]
    spent = sum(t["usage"]["known_tokens"] for t in started)
    sql = Counter()
    for t in started:
        sql.update(t["sql"])
    lines = [
        "# 正式接口评测",
        "",
        (
            f"计划 {len(manifest['cases'])} 题，已启动 {len(started)} 题，"
            f"业务已裁决 {sum(t['business'] != 'review' for t in started)} 题。"
        ),
        (
            f"业务通过 {sum(t['business'] == 'pass' for t in started)}/{len(started)}，"
            f"首次正确交付 {sum(t['first_correct_delivery'] == 'pass' for t in started)}/{len(started)}。"
        ),
        f"SQL 成功 {sql['succeeded']}/{sum(sql.values())} 次。供应商已报告 {spent:,} Token。",
        "",
        "| 题目 | 业务 | 首次正确交付 | 秒 | 失败或待裁决项 |",
        "|---|---|---|---:|---|",
    ]
    for t in trials:
        issues = [
            r["assertion"] + "=" + r["status"]
            for r in scores
            if r["case_id"] == t["case"] and r["status"] != "pass"
        ]
        lines.append(
            f"| {t['case']} | {t['business']} | {t['first_correct_delivery']} | {t['seconds']} | {', '.join(issues) or '—'} |"
        )
    lines.extend(["", "| 指标 | 分子 / 分母 | 比例 |", "|---|---:|---:|"])
    for name, key in (
        ("数字及分组", "numeric"),
        ("SQL 业务口径", "sql_scope"),
        ("跨月反例", "boundary"),
        ("澄清", "clarification"),
        ("缺数处理", "insufficient_data"),
        ("归因分项、守恒与限制", "attribution"),
        ("正式文件下载", "download"),
        ("执行 SQL 成功率", "sql_execution"),
        ("模型用量可观测率", "usage_coverage"),
    ):
        metric = metrics[key]
        percentage = "N/A" if metric["rate"] is None else f"{metric['rate']:.1%}"
        lines.append(
            f"| {name} | {metric['numerator']} / {metric['denominator']} | {percentage} |"
        )
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return trials


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", type=Path)
    p.add_argument("--group", default="first")
    p.add_argument("--refresh-audit", action="store_true")
    args = p.parse_args()
    root = args.folder / args.group
    cases = read(root / "manifest.json")["cases"]
    cids = [
        r["cid"]
        for case in cases
        for r in rows(root / (case + ".jsonl"))
        if r["kind"] == "conversation"
    ]
    if args.refresh_audit or not (root / "audit.json").exists():
        save(root / "audit.json", run_async(fetch_audit(cids)))
        save(root / "rankings.json", run_async(fetch_rankings(cids)))
    before = read(args.folder / "before.json")
    frozen = (
        hashlib.sha256(
            json.dumps(before["tables"], sort_keys=True).encode()
        ).hexdigest()
        == TABLE_SNAPSHOT
    )
    if (args.folder / "after.json").exists():
        after = read(args.folder / "after.json")
        frozen = frozen and before["tables"] == after["tables"]
    build(args.folder, args.group, read(root / "audit.json"), frozen)


if __name__ == "__main__":
    main()
