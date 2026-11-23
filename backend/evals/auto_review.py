"""Read structured assertions and bind submitted SQL to counterexample projections."""

import hashlib
import re
from decimal import Decimal
from html.parser import HTMLParser

from .offline_score import read

# Projection order follows each benchmark's fixture assertion. Unrecognized
# projections need review; a diagnostic SQL with the right numbers is insufficient.
PROJECTIONS = {
    "ORD-01": ["order_cnt"],
    "AMT-02": ["effective_gmv"],
    "PAY-04": ["numerator_success_attempt_cnt", "denominator_attempt_cnt"],
    "STA-04": ["last_status_asof_0915", "order_cnt"],
    "TRF-02": ["uv_mixed_prefixed"],
    "SEA-05": ["numer_clicked_requests", "denom_success_requests"],
    "INT-04": ["metric_name", "metric_value"],
    "USR-05": ["repurchase_users", "buyer_users"],
    "PRD-04": ["num_sku_all", "den_sku_all"],
    "INV-06": ["eq0_excl_norecord"],
    "FUL-06": ["numer_split_orders", "denom_fwd_pkg_orders"],
    "REF-04": ["paid_amount", "refund_amount", "net_pay_amount"],
    "MKT-04": ["numerator_used_inst_in_month", "denominator_recv_inst_in_month"],
    "CMT-03": ["numerator", "denominator"],
    "XDM-01": [
        "numerator_sessions_ordered_in_sep",
        "denominator_sessions_started_in_sep",
    ],
    "ATT-01": ["channel", "change_amount"],
    "CNV-01": ["gmv_all_detail_sum"],
    "LIM-02": ["month", "order_cnt"],
}

NUMBER = r"([+-]?\d[\d,]*(?:\.\d+)?)"


def submitted_query(queries, tools, final, columns):
    submitted = {
        str(path)
        for tool in tools
        if tool["name"] == "submit_task_result"
        for path in (tool.get("args") or {}).get("evidence", [])
    }
    observed = {
        (tool["call"], tool["result"].get("execution_id"))
        for tool in tools
        if tool["name"] in ("execute_sql", "execute_semantic_query")
        and isinstance(tool.get("result"), dict)
    }
    candidates = []
    for q in queries:
        sid = str(q["id"])
        if q["status"] != "succeeded" or (q["tool_call_id"], sid) not in observed:
            continue
        result = q.get("result_summary") or {}
        if result.get("path") not in submitted and sid not in final:
            continue
        if set(columns) <= {c["name"] for c in result.get("columns", [])}:
            candidates.append(q)
    cited = [q for q in candidates if str(q["id"]) in final]
    choices = cited or candidates
    if len(choices) > 1:
        primary_paths = {
            tool["args"]["evidence"][0]
            for tool in tools
            if tool["name"] == "submit_task_result"
            and (tool.get("args") or {}).get("evidence")
        }
        primary = [q for q in choices if q["result_summary"]["path"] in primary_paths]
        if len(primary) == 1:
            return primary[0]
    return choices[0] if len(choices) == 1 else None


def answer_facts(case, turns, ref):
    body = turns[-1]["answer"].replace("**", "").replace("−", "-")
    facts = {}

    def get(key, pattern, integer=False):
        match = re.search(pattern, body, re.MULTILINE)
        if match:
            value = match[1].replace(",", "")
            if integer and not re.fullmatch(r"\d+", value):
                return
            facts[key] = [
                int(value) if integer else value,
                ref + " " + match[0].strip(),
            ]

    if case in {
        "PAY-04",
        "SEA-05",
        "PRD-04",
        "USR-05",
        "CMT-03",
        "MKT-04",
        "XDM-01",
        "FUL-06",
    }:
        for key, word in (("num", "分子"), ("den", "分母")):
            get(key, r"^\|[^|\n]*" + word + r"[^|\n]*\|\s*" + NUMBER, True)
            if key not in facts:
                get(key, r"^- " + word + r"[：:][^\n]*?为\s*" + NUMBER + r"\s*$", True)
        # Headline percentage, before sensitivity analyses and alternative definitions.
        headline = next((s for s in body.splitlines()[:6] if "%" in s), "")
        m = re.search(NUMBER + r"\s*%", headline)
        if m:
            facts["percent"] = [m[1], ref + " " + headline]
    singles = {
        "AMT-02": ("gmv", r"有效\s*GMV\s*[=＝为]\s*" + NUMBER + r"\s*元", False),
        "ORD-01": ("orders", r"全站下单订单数为\s*" + NUMBER + r"\s*笔", True),
        "TRF-02": ("uv", r"全站\s*UV\s*[=＝为]\s*" + NUMBER, True),
        "INV-06": ("zero_skus", r"^答案[：:]\s*" + NUMBER + r"\s*个\s*SKU", True),
        "CNV-01": (
            "receivable",
            r"按下单应收口径[^\n]*\n\s*" + NUMBER + r"\s*元",
            False,
        ),
        "LIM-02": ("orders", r"全站下单订单数为\s*" + NUMBER + r"\s*单", True),
    }
    if case in singles:
        get(*singles[case])
    if case == "STA-04":
        for s in ("PAID", "SHIPPED", "COMPLETED", "CANCELLED"):
            get(s, r"^\|\s*" + s + r"(?:（[^）]+）)?\s*\|\s*" + NUMBER + r"\s*\|", True)
    if case == "INT-04":
        for k, s in (("removed", "删除"), ("cleared", "清空")):
            get(k, r"^\|\s*购物车" + s + r"次数\s*\|\s*" + NUMBER + r"\s*次", True)
    if case == "CNV-01" and len(turns) == 2:
        first = turns[0]["answer"]
        if (
            re.search(r"确认|请选择", first)
            and "下单应收" in first
            and "支付" in first
            and "下单应收" in turns[1]["question"]
        ):
            facts["clarified"] = [
                True,
                ref + " 第一轮询问金额口径，第二轮用户指定下单应收",
            ]
    if (
        case == "LIM-02"
        and re.search(r"8\s*月基数缺失", body)
        and re.search(r"变化量无法计算", body)
    ):
        facts.update(
            {
                k: [True, ref + " 8 月基数缺失，变化量无法计算"]
                for k in ("coverage_disclosed", "change_unavailable")
            }
        )
    return facts


class TableRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], [], None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        if tag in ("th", "td"):
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("th", "td") and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        if tag == "tr":
            self.rows.append(self.row)


def report_facts(root, events, answer):
    download = next(
        (e for e in reversed(events) if e["kind"] == "download" and e.get("verified")),
        None,
    )
    if not download:
        return {}
    file = root / download["file"]
    if (
        not file.is_file()
        or hashlib.sha256(file.read_bytes()).hexdigest() != download["sha256"]
    ):
        return {}
    html = file.read_text(encoding="utf8")
    parser = TableRows()
    parser.feed(html)
    keys = {"APP", "H5", "PC", "MINI_PROGRAM", "SEM", "合计"}
    values = {}
    arithmetic = []
    for row in parser.rows:
        if len(row) != 4 or row[0] not in keys:
            continue
        if row[0] in values:
            return {}
        amounts = [a.replace(",", "").replace("−", "-") for a in row[1:]]
        if not all(re.fullmatch(NUMBER, a) for a in amounts):
            return {}
        a, b, d = map(Decimal, amounts)
        arithmetic.append(b - a == d)
        values[row[0]] = d
    if values.keys() != keys:
        return {}
    total = values.pop("合计")
    ref = download["file"] + "#证据表"
    facts = {k: [str(v), ref] for k, v in {**values, "ALL": total}.items()}
    facts["balanced"] = [all(arithmetic) and sum(values.values()) == total, ref]
    if re.search(r"不推断变化原因|无法确定因果", html) and re.search(
        r"无法判断变化原因|无法确定因果", answer
    ):
        facts["causal_limit"] = [True, ref + " 与终答均限制因果解释"]
    return facts


def collect(folder, group, audit):
    from .live_report import rows, save, trace_trial

    root = folder / group
    manual = read(root / "reviews.json") if (root / "reviews.json").exists() else {}
    notes = dict(manual)
    generated = {}
    for case in read(root / "manifest.json")["cases"]:
        path = root / (case + ".jsonl")
        events = rows(path)
        if case not in PROJECTIONS or not events or events[-1]["kind"] != "closed":
            continue
        turns, tools, _ = trace_trial(events)
        if not turns or not all(t["status"] == "done" and t["answer"] for t in turns):
            continue
        ref = path.name + "#stream"
        facts = answer_facts(case, turns, ref)
        if case == "ATT-01":
            facts.update(report_facts(root, events, turns[-1]["answer"]))
        cid = next((e["cid"] for e in events if e["kind"] == "conversation"), None)
        cols = PROJECTIONS[case]
        query = submitted_query(
            [q for q in audit if str(q["conversation_id"]) == cid],
            tools,
            turns[-1]["answer"],
            cols,
        )
        note = {
            "trial": events[0]["trial"],
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "facts": facts,
        }
        if query:
            note.update(answer_sql=str(query["id"]), counterexample_columns=cols)
            if case == "PAY-04":
                note["pay_columns"] = cols
        generated[case] = note
        prior = manual.get(case, {})
        notes[case] = {**note, **prior, "facts": {**facts, **prior.get("facts", {})}}
    save(root / "automatic_reviews.json", generated)
    return notes
