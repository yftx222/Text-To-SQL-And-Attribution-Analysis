"""Assemble the first-trial baseline and the predeclared stability sample."""

import argparse
import hashlib
from collections import Counter
from pathlib import Path

from .live_report import jsonl, rows, save
from .metrics import rate, summarize
from .offline_score import read

SNAPSHOT_KEYS = ("tables", "metadata", "es", "source", "config", "models", "limits")


def ratio(metric):
    n, d = metric["numerator"], metric["denominator"]
    return f"{n}/{d}（{n / d:.1%}）" if d else "N/A（0 个可评分项）"


def finalize(folder):
    design = read(folder / "design.json")
    groups = design["groups"]
    before = read(folder / "before.json")
    snapshots = {
        p.name: read(p)
        for p in [
            folder / "after.json",
            *(folder / (g + "-before.json") for g in groups[1:]),
        ]
    }
    changed = {
        name: [k for k in SNAPSHOT_KEYS if s[k] != before[k]]
        for name, s in snapshots.items()
    }
    all_trials, all_scores, source_files = [], [], []
    for g in groups:
        root = folder / g
        trials, scores = rows(root / "trials.jsonl"), rows(root / "scores.jsonl")
        planned = read(root / "manifest.json")["cases"]
        if [t["case"] for t in trials] != planned or not all(
            t["started"] and t["closed"] for t in trials
        ):
            raise ValueError(f"{g} 有未完成的运行，请先对账")
        if g != groups[0] and set(planned) != set(design["repeat"]):
            raise ValueError(f"{g} 复跑题目与预登记不符")
        all_trials.extend({"group": g, **t} for t in trials)
        all_scores.extend({"group": g, **s} for s in scores)
        source_files.extend(
            root / name
            for name in (
                "manifest.json",
                "scores.jsonl",
                "trials.jsonl",
                "reviews.json",
                "audit.json",
                "rankings.json",
            )
        )
    if len({t["cid"] for t in all_trials}) != len(all_trials) or len(
        {t["trial"] for t in all_trials}
    ) != len(all_trials):
        raise ValueError("部分尝试复用了会话或 trial_id")
    first = [t for t in all_trials if t["group"] == groups[0]]
    metrics = summarize(first, all_scores)
    human = read(folder / groups[0] / "reviews.json")
    by_type = {}
    for name, kind in (("查数", "other"), ("多轮", "CNV"), ("归因报告", "ATT")):
        selected = [
            t
            for t in first
            if (
                t["case"].split("-")[0]
                if t["case"].startswith(("ATT-", "CNV-"))
                else "other"
            )
            == kind
        ]
        by_type[name] = summarize(selected, all_scores)
    repeats = {
        key: [t["business"] for t in all_trials if t["case"] == key]
        for key in design["repeat"]
    }
    recall = {}
    for key in ("column", "metric"):
        values = [t["recall"][key] for t in first if t["recall"] and key in t["recall"]]
        recall[key] = {"cases": len(values)}
        for field in ("fused_top_k", "model_visible"):
            measured = [v[field] for v in values if v[field] is not None]
            recall[key][field] = rate(
                sum(m["numerator"] for m in measured),
                sum(m["denominator"] for m in measured),
            )
            recall[key][field + "_measured_cases"] = len(measured)
    sql = Counter()
    for t in first:
        sql.update(t["sql"])
    catalog = read(folder / "catalog_200.json")
    package = {
        "cases": len(catalog),
        "chat_ready": sum(bool(c["messages"]) for c in catalog),
        "controlled": len(read(folder / "controlled/inventory.json")),
    }
    source_files.extend(
        [
            folder / n
            for n in (
                "before.json",
                "after.json",
                "design.json",
                "telemetry.jsonl",
                "catalog_200.json",
            )
        ]
    )
    batches = {}
    first_ids = {t["case"] for t in first}
    for row in catalog:
        if row["id"] in first_ids:
            continue
        key = row["id"].split("-")[0]
        batches.setdefault(
            key, {"setup": row["setup"], "scoring": row["scoring"], "cases": []}
        )["cases"].append(row["id"])
    save(folder / "next_batches.json", batches)
    manifest = {
        "design": design,
        "source": before["source"],
        "models": before["models"],
        "scoring_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(__file__).parent.iterdir())
            if p.suffix in (".json", ".py", ".sql")
        },
        "snapshot_differences": changed,
        "planned_first": len(first),
        "total_trials": len(all_trials),
        "user_turns": sum(len(t["turns"]) for t in all_trials),
        "inventory": package,
        "first": metrics,
        "by_type": by_type,
        "repeat_results": repeats,
        "diagnostic_recall": recall,
        "first_sql_outcomes": dict(sql),
        "evidence_sha256": {
            p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source_files
        },
    }
    save(folder / "manifest.json", manifest)
    jsonl(folder / "trials.jsonl", all_trials)
    jsonl(folder / "scores.jsonl", all_scores)
    lines = [
        "# DataAgent 正式评测基线",
        "",
        f"首轮 {len(first)} 道题，各运行一次，另有 {len(all_trials) - len(first)} 次高风险复跑。共 {len(all_trials)} 个独立会话、{manifest['user_turns']} 个用户回合。业务通过 {ratio(metrics['business'])}，首次正确交付 {ratio(metrics['first_correct_delivery'])}。",
        "",
        "快照对照覆盖 Doris 数据、PostgreSQL 元数据和已有 SQL 经验、ES 索引、应用源码、模型、配置和预算。"
        + (
            "所有对照一致。"
            if not any(changed.values())
            else f"检测到变化：{changed}。"
        ),
        "",
        "| 首轮指标 | 实测 | 待裁决题数 |",
        "|---|---:|---:|",
    ]
    for name, key in (
        ("业务正确", "business"),
        ("首次正确交付", "first_correct_delivery"),
        ("数字及分组", "numeric"),
        ("SQL 业务口径", "sql_scope"),
        ("支付跨月反例", "boundary"),
        ("澄清", "clarification"),
        ("缺数处理", "insufficient_data"),
        ("归因分项和解释", "attribution"),
        ("正式附件交付", "download"),
        ("SQL 执行成功", "sql_execution"),
        ("模型返回 Token 用量", "usage_coverage"),
    ):
        m = metrics[key]
        lines.append(f"| {name} | {ratio(m)} | {m.get('review', '—')} |")
    lines += [
        "",
        "SQL 按尝试计数。"
        + "，".join(f"{k} {v}" for k, v in sql.items())
        + "。数字等断言按题计数，用户未拿到报告时，其内容保留待裁决。",
        "",
        "| 问题类型 | 首轮正确 | 耗时中位数 / P90（秒） | 已报告 Token | 用量记录 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, m in by_type.items():
        timing = m["latency_seconds"]
        lines.append(
            f"| {name} | {ratio(m['business'])} | {timing['p50']:.1f} / {timing['p90_nearest_rank']:.1f} | {m['known_tokens']:,} | {ratio(m['usage_coverage'])} |"
        )
    lines += [
        "",
        f"首轮已报告 {metrics['known_tokens']:,} Token，包含重复上下文。缺失用量单列；未绑定计费价格与缓存折扣，实际费用未知。耗时为每题用户回合耗时之和，P90 取最近秩。",
        "",
        "## 高风险复跑",
        "",
        "| 题目 | 首次 | 第二次 | 第三次 |",
        "|---|---|---|---|",
    ]
    for key, statuses in repeats.items():
        lines.append("| " + " | ".join([key, *statuses]) + " |")
    lines += [
        "",
        f"预登记题首次通过 {sum(v[0] == 'pass' for v in repeats.values())}/{len(repeats)}，三次均通过 {sum(all(s == 'pass' for s in v) for v in repeats.values())}/{len(repeats)}。复跑单独统计。",
        "",
        "## 首次检索与最终收到的上下文",
        "",
        "| 资源 | 标注题 | 首次融合前五项覆盖 | 首次完整工具结果覆盖 |",
        "|---|---:|---:|---:|",
    ]
    for name, k in (("字段", "column"), ("指标", "metric")):
        m = recall[k]
        lines.append(
            f"| {name} | {m['cases']} | {ratio(m['fused_top_k'])} | {ratio(m['model_visible'])} |"
        )
    lines += [
        "",
        "分母是人工标注的必需资源组数，每组允许等价字段。仅统计能与实际工具返回时间和查询对应的证据。逐词、逐通道排名见各题记录。",
        "",
        "## 首轮失败题",
        "",
    ]
    for t in first:
        if t["business"] == "fail":
            scope = human[t["case"]]["facts"]["sql_scope"]
            reason = scope[1].partition(" ")[2] if not scope[0] else t["final"]
            lines.append(f"- {t['case']}　{reason}")
    lines += [
        "",
        "## 复查入口",
        "",
        "[首轮逐题报告](first/report.md) · [第二次](repeat_2/report.md) · [第三次](repeat_3/report.md) · [运行与文件指纹](manifest.json) · [全部断言](scores.jsonl)",
        "",
        "[200 题清单](catalog_200.json)含 170 道可发送的业务快照题，首轮覆盖其中 18 道。其余 152 道快照题和 30 道受控题已经按主题列入[后续批次](next_batches.json)。其余 30 道见[受控场景材料](controlled/inventory.json)。独立样本的参考 SQL 验证与真实模型的端到端成绩分别统计。",
    ]
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    finalize(parser.parse_args().folder)
