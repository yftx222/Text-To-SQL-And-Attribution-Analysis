"""Build a short, standalone two-period report from one additive amount per group."""

import argparse
import base64
import csv
import json
from decimal import Decimal
from html import escape
from pathlib import Path


def plot(args, changes):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {"font.family": "WenQuanYi Zen Hei", "axes.unicode_minus": False}
    )
    fig, ax = plt.subplots(figsize=(8, max(2.8, len(changes) * 0.38)))
    bars = ax.barh(
        range(len(changes)),
        [float(x) for _, x in changes],
        color=["#ad4937" if x < 0 else "#296d72" for _, x in changes],
    )
    ax.set_yticks(range(len(changes)), labels=[k for k, _ in changes])
    ax.axvline(0, color="#6b7280", linewidth=0.8)
    ax.set_xlabel(f"{args.metric}变化（{args.unit}）")
    ax.bar_label(bars, labels=[f"{x:+,.2f}" for _, x in changes], padding=3)
    ax.margins(x=0.28)
    ax.set_title(
        f"{args.title}\n{args.after_label} 减 {args.before_label}\n"
        f"合计变化 {sum(x for _, x in changes):+,.2f} {args.unit}"
    )
    fig.tight_layout()
    chart = args.output.with_suffix(".png")
    fig.savefig(chart, dpi=140)
    plt.close(fig)
    if getattr(args, "chart_only", False):
        return ""
    source = base64.b64encode(chart.read_bytes()).decode("ascii")
    return (
        f'<img alt="各分组本期比上期的金额变化" src="data:image/png;base64,{source}">'
    )


def grouped_changes(args, stream):
    reader = csv.DictReader(stream)
    if not {args.group, args.before, args.after}.issubset(reader.fieldnames or []):
        raise ValueError(f"请从实际 CSV 表头选择列：{reader.fieldnames}")
    if len({args.group, args.before, args.after}) != 3:
        raise ValueError("分组、前期、本期须为三个不同的列")
    amounts = {}
    for row in reader:
        key = row[args.group]
        if key in amounts or not key:
            raise ValueError("分组为空或重复，请提供每组一行的汇总 CSV")
        amounts[key] = (Decimal(row[args.before]), Decimal(row[args.after]))
    stated_total = (
        amounts.pop(args.total_label) if args.total_label is not None else None
    )
    if not amounts or any(not x.is_finite() for pair in amounts.values() for x in pair):
        raise ValueError("需要各分组的两期完整金额")
    total = tuple(sum(pair[i] for pair in amounts.values()) for i in (0, 1))
    if stated_total is not None and total != stated_total:
        raise ValueError("分项合计与来源文件中的合计不一致")
    changes = sorted(
        ((k, b - a) for k, (a, b) in amounts.items()), key=lambda item: item[1]
    )
    return amounts, total, changes


def compose(args, stream, make_chart):
    amounts, total, changes = grouped_changes(args, stream)
    chart = make_chart(changes) if make_chart else ""

    show_rate = not getattr(args, "amounts_only", False)
    rate_header = "<th>变化率</th>" if show_rate else ""

    def row(label, pair):
        a, b = pair
        rate = f"{(b - a) * 100 / a:+,.2f}%" if a else "—"
        rate_cell = f"<td>{rate}</td>" if show_rate else ""
        return f"<tr><th>{escape(label)}</th><td>{a:,.2f}</td><td>{b:,.2f}</td><td>{b - a:+,.2f}</td>{rate_cell}</tr>"

    body = "".join(row(k, amounts[k]) for k, _ in changes) + row("合计", total)
    low, high = changes[0], changes[-1]
    findings = [
        f"合计由 {total[0]:,.2f} 变为 {total[1]:,.2f} {args.unit}，变化 {total[1] - total[0]:+,.2f} {args.unit}。"
    ]
    if low[1] < 0:
        findings.append(f"{low[0]} 减少金额最多（{low[1]:+,.2f} {args.unit}）。")
    if high[1] > 0:
        findings.append(f"{high[0]} 增加金额最多（{high[1]:+,.2f} {args.unit}）。")
    columns = {k: getattr(args, k) for k in ("group", "before", "after", "total_label")}
    columns["show_rate"] = show_rate
    html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="dataagent-comparison" content="{escape(json.dumps(columns, ensure_ascii=False))}"><title>{escape(args.title)}</title>
<style>body{{max-width:880px;margin:2rem auto;padding:0 1rem;color:#192e3d;font:16px/1.55 sans-serif}}img{{width:100%;height:auto}}section{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}th,td{{padding:.45rem;border-bottom:1px solid #cbd5db;text-align:right}}th:first-child{{text-align:left}}</style>
<h1>{escape(args.title)}</h1><p>{escape(args.metric)}，{escape(args.before_label)} 与 {escape(args.after_label)} 对比，单位为{escape(args.unit)}。</p><p>{escape("".join(findings))}</p>
{chart}
<h2>证据表</h2><section><table><thead><tr><th>{escape(args.group)}</th><th>{escape(args.before_label)}</th><th>{escape(args.after_label)}</th><th>变化</th>{rate_header}</tr></thead><tbody>{body}</tbody></table></section>
<h2>口径与限制</h2><p>{escape(args.source_note)}</p><p>来源文件：{escape(args.csv.name)}</p><p>分组变化表示对总额变化的算术贡献，仅凭两期汇总数据无法确定因果关系。</p></html>"""
    return html, {
        "group_count": len(amounts),
        "before": str(total[0]),
        "after": str(total[1]),
        "change": str(total[1] - total[0]),
        "findings": findings,
        "html_bytes": len(html.encode("utf-8")),
    }


def render(args):
    with args.csv.open(encoding="utf-8-sig", newline="") as stream:
        if getattr(args, "chart_only", False):
            _, _, changes = grouped_changes(args, stream)
            plot(args, changes)
            return {"groups": len(changes)}
        html, summary = compose(
            args,
            stream,
            None if args.table_only else lambda changes: plot(args, changes),
        )
    args.output.write_bytes(html.encode("utf-8"))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--amounts-only", action="store_true", help="仅展示金额和绝对变化额"
    )
    parser.add_argument("--table-only", action="store_true", help="仅输出证据表和结论")
    parser.add_argument("--chart-only", action="store_true", help="仅生成同名 PNG")
    parser.add_argument(
        "--summary-json", action="store_true", help="输出与证据表同源的总额和变化摘要"
    )
    for flag in (
        "group",
        "before",
        "after",
        "before-label",
        "after-label",
        "metric",
        "unit",
        "source-note",
        "title",
    ):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument(
        "--total-label", help="已有的合计行的分组值；CSV 无合计行时省略"
    )
    args = parser.parse_args()
    summary = render(args)
    if args.summary_json:
        print(json.dumps(summary, ensure_ascii=False))
