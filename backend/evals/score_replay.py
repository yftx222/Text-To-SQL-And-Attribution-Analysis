"""Extract labeled numerical claims from evaluation responses."""

import re
from decimal import Decimal

TABLE_SNAPSHOT = "d128f88258b97b2edfd6427aab128a732d1eb3491be5e5636977a1efa21954bb"
NUMBER = r"([-+]?\d[\d,]*(?:\.\d+)?)"


def numeric_facts(case, answer, ref):
    """Read labeled claims in the archive's presentation format. Abstain on unknown formats."""
    clean = answer.replace("**", "")
    facts = {}

    def capture(key, pattern, *, integer=False):
        match = re.search(pattern, clean)
        if match:
            value = match[1].replace(",", "")
            facts[key] = (
                int(value) if integer and re.fullmatch(r"\d+", value) else value,
                ref,
            )

    single = {
        "ORD-01": ("orders", r"全站下单\s*" + NUMBER + r"\s*笔"),
        "AMT-02": ("gmv", r"有效\s*GMV\s*为\s*" + NUMBER + r"\s*元"),
        "TRF-02": ("uv", r"全站\s*UV\s*=\s*" + NUMBER),
        "INV-06": ("zero_skus", r"^\s*" + NUMBER + r"\s*个\s*SKU"),
        "CNV-01": ("receivable", r"下单应收计口径的实际成交金额为\s*" + NUMBER),
        "LIM-02": ("orders", r"\|\s*2026-09\s*\|\s*" + NUMBER + r"\s*单"),
    }
    if case in single:
        key, pattern = single[case]
        capture(key, pattern, integer=key in {"orders", "zero_skus", "uv"})
    if case in {
        "PAY-04",
        "SEA-05",
        "USR-05",
        "PRD-04",
        "FUL-06",
        "MKT-04",
        "CMT-03",
        "XDM-01",
    }:
        heading = clean.splitlines()[0] if clean else ""
        pct = re.search(NUMBER + "%", heading)
        if pct:
            facts["percent"] = (pct[1], ref)
        if case == "USR-05":
            for name, label in (("num", "分子"), ("den", "分母")):
                capture(
                    name,
                    label + r"“[^”]+”：[^\n]*?即\s*" + NUMBER + r"\s*人",
                    integer=True,
                )
        else:
            # Use the ratio's own line; secondary examples and dates cannot supply missing counts.
            ratio_line = next(
                (
                    line
                    for line in clean.splitlines()
                    if re.search(
                        r"^\|\s*(支付成功率|比率|动销率|拆包率|使用率|占比)\s*\|", line
                    )
                    or (case == "CMT-03" and "好评率 =" in line)
                ),
                "",
            )
            ratio = re.search(NUMBER + r"\s*[/÷]\s*" + NUMBER, ratio_line)
            if ratio:
                for name, raw in zip(("num", "den"), ratio.groups(), strict=True):
                    value = Decimal(raw.replace(",", ""))
                    facts[name] = (
                        int(value) if value == int(value) else str(value),
                        ref,
                    )
    if case == "STA-04":
        for status in ("CANCELLED", "COMPLETED", "PAID", "SHIPPED"):
            capture(
                status, r"\|\s*" + status + r"\s*\|[^|\n]*\|\s*" + NUMBER, integer=True
            )
    if case == "INT-04":
        for name, label in (("removed", "删除"), ("cleared", "清空")):
            capture(
                name,
                r"\|\s*购物车" + label + r"次数\s*\|[^|\n]*\|\s*" + NUMBER,
                integer=True,
            )
    if case == "REF-04":
        for name, label in (
            ("paid", "实付金额"),
            ("refund", "成功退款金额"),
            ("net", "净支付金额"),
        ):
            capture(name, r"\|\s*" + label + r"\s*\|\s*" + NUMBER)
    if case == "LIM-02":
        if re.search(r"\| 2026-08 \| 无数据", clean):
            facts["coverage_disclosed"] = (True, ref)
        if all(
            re.search(r"\| " + label + r"[^|\n]*\| 不可计算", clean)
            for label in ("差值", "变化率")
        ):
            facts["change_unavailable"] = (True, ref)
    return facts
