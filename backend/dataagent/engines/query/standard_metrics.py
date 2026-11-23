"""已发布售后财务指标的全量口径。由只读查询服务照常鉴权、执行、审计。"""

from datetime import date


_PAYOUT = """SELECT p.refund_pay_detail_id,p.refund_detail_id,p.refund_amount,
         e.first_success_time
  FROM dwd_trade_refund_pay_detail_di p
  JOIN (
    SELECT refund_pay_detail_id,after_refund_pay_status,
      ROW_NUMBER() OVER(PARTITION BY refund_pay_detail_id ORDER BY event_seq_no DESC,event_time DESC) AS rn,
      MIN(CASE WHEN after_refund_pay_status='SUCCESS' THEN event_time END)
        OVER(PARTITION BY refund_pay_detail_id) AS first_success_time
    FROM dwd_trade_refund_pay_status_event_di WHERE event_time < '{end}'
  ) e ON e.refund_pay_detail_id=p.refund_pay_detail_id AND e.rn=1
  WHERE p.refund_pay_request_time >= '{start}' AND p.refund_pay_request_time < '{end}'
    AND e.after_refund_pay_status='SUCCESS'"""

_ORDERS = """SELECT DISTINCT order_detail_id FROM dwd_trade_order_detail_di
  WHERE order_create_time >= '{start}' AND order_create_time < '{end}'"""

_QUERIES = {
    "退款成功金额": (
        "payout",
        ("successful_payout_amount", "successful_payout_count"),
        """SELECT COALESCE(SUM(refund_amount),0) AS successful_payout_amount,
      COUNT(*) AS successful_payout_count FROM payout""",
    ),
    "平均退款处理时长": (
        "payout",
        ("avg_processing_hours", "payout_duration_count"),
        """SELECT AVG(TIMESTAMPDIFF(SECOND,a.apply_time,p.first_success_time))/3600.0 AS avg_processing_hours,
      COUNT(*) AS payout_duration_count FROM payout p
      JOIN dwd_trade_refund_detail_di a ON a.refund_detail_id=p.refund_detail_id""",
    ),
    "退货率": (
        "orders",
        ("return_applications", "order_lines", "return_rate_pct"),
        """SELECT t.return_applications,t.order_lines,
      100.0*t.return_applications/NULLIF(t.order_lines,0) AS return_rate_pct
      FROM (SELECT COUNT(DISTINCT a.refund_detail_id) AS return_applications,
        COUNT(DISTINCT o.order_detail_id) AS order_lines
        FROM orders o LEFT JOIN dwd_trade_refund_detail_di a
        ON a.order_detail_id=o.order_detail_id AND a.apply_time<'{end}' AND a.need_return_goods=1) t""",
    ),
}


def supports_standard_query(name: str) -> bool:
    return name in _QUERIES


def standard_query(names: list[str], start: date, end: date) -> str:
    if start >= end or not names or len(set(names)) != len(names):
        raise ValueError("请提供互不重复的指标名称和左闭右开的日期区间")
    if any(name not in _QUERIES for name in names):
        raise ValueError("存在尚未登记标准查询的指标")
    clocks = {"start": start.isoformat(), "end": end.isoformat()}
    needed = {_QUERIES[name][0] for name in names}
    ctes = [
        f"{key} AS ({sql.format(**clocks)})"
        for key, sql in (("payout", _PAYOUT), ("orders", _ORDERS))
        if key in needed
    ]
    projections = []
    for i, name in enumerate(names):
        key = f"m{i}"
        _, columns, sql = _QUERIES[name]
        ctes.append(f"{key} AS ({sql.format(**clocks)})")
        for column in columns:
            projections.append(
                f"m0.{column}"
                if i == 0
                else f"(SELECT {column} FROM {key}) AS {column}"
            )
    return (
        "WITH " + ",\n".join(ctes) + "\nSELECT " + ", ".join(projections) + " FROM m0"
    )
