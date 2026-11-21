"""Replay an audited period query in Doris with literal rows in place of warehouse tables."""

from sqlglot import exp, parse
from sqlglot.optimizer.scope import traverse_scope

from dataagent.engines.contract.config import cfg
from dataagent.engines.infra.doris_client_manager import DorisClientManager

# September requests 1 and 2. Both eventually succeed, only one by September end.
PAYMENT = {
    "dwd_trade_pay_detail_di": (
        "pay_detail_id biz_date pay_request_time",
        [
            (1, "2026-09-30", "2026-09-30 20:00:00"),
            (2, "2026-09-30", "2026-09-30 20:00:00"),
        ],
    ),
    "dwd_trade_pay_status_event_di": (
        "pay_detail_id after_pay_status event_seq_no event_time biz_date",
        [
            (1, "SUCCESS", 1, "2026-09-30 21:00:00", "2026-09-30"),
            (2, "FAILED", 1, "2026-09-30 21:00:00", "2026-09-30"),
            (2, "SUCCESS", 2, "2026-10-01 00:01:00", "2026-10-01"),
        ],
    ),
}
# A and B were on sale in September. Only A still was at month end. An October
# cancellation must not undo A's September sale. C's sale version ends at period start.
SKU = {
    "dim_sku_info_zip": (
        "sku_id sku_sk sku_status is_deleted is_current effective_start_time effective_end_time spu_id shop_id",
        [
            (1, 11, "在售", 0, 1, "2026-08-01 00:00:00", "9999-12-31 00:00:00", 7, 8),
            (2, 21, "在售", 0, 0, "2026-08-01 00:00:00", "2026-09-20 00:00:00", 7, 8),
            (2, 22, "下架", 0, 1, "2026-09-20 00:00:00", "9999-12-31 00:00:00", 7, 8),
            (3, 31, "在售", 0, 0, "2026-08-01 00:00:00", "2026-09-01 00:00:00", 7, 8),
        ],
    ),
    "dwd_trade_order_detail_di": (
        "order_id order_detail_id sku_id sku_sk sku_qty biz_date order_create_time",
        [(100, 1000, 1, 11, 1, "2026-09-15", "2026-09-15 10:00:00")],
    ),
    "dwd_trade_order_status_event_di": (
        "order_id after_order_status event_seq_no event_time biz_date",
        [
            (100, "PAID", 1, "2026-09-15 11:00:00", "2026-09-15"),
            (100, "CANCELLED", 2, "2026-10-02 10:00:00", "2026-10-02"),
        ],
    ),
    "dwd_trade_refund_detail_di": ("order_detail_id", []),
}


def after_october_delisting():
    """SKU A gains a later version. Its September status must remain reproducible."""
    columns, versions = SKU["dim_sku_info_zip"]
    keys = columns.split()
    original = dict(zip(keys, versions[0], strict=True))
    cutoff = "2026-10-02 00:00:00"
    september = {**original, "is_current": 0, "effective_end_time": cutoff}
    october = {
        **original,
        "sku_sk": 12,
        "sku_status": "下架",
        "effective_start_time": cutoff,
    }
    rows = [tuple(version[k] for k in keys) for version in (september, october)]
    return {**SKU, "dim_sku_info_zip": (columns, rows + versions[1:])}


def fixture_sql(sql, tables):
    trees = parse(sql, read="doris")
    if len(trees) != 1 or not isinstance(trees[0], exp.Query):
        raise ValueError("需要单条查询")
    tree = trees[0]
    seen = set()
    for scope in traverse_scope(tree):
        for _, source in scope.selected_sources.values():
            if not isinstance(source, exp.Table):
                continue
            name = source.name
            if name not in tables:
                raise ValueError(f"缺少反例表 {name}")
            columns, rows = tables[name]
            names = columns.split()
            records = []
            for row in rows or [[None] * len(names)]:
                fields = []
                for field, val in zip(names, row, strict=True):
                    typ = (
                        "DATETIME"
                        if field.endswith("_time")
                        else "DATE"
                        if field == "biz_date"
                        else "DECIMAL(18,2)"
                        if field.endswith("_amount")
                        else "VARCHAR"
                        if "status" in field
                        else "BIGINT"
                    )
                    value = exp.cast(exp.convert(val), typ, dialect="doris")
                    fields.append(exp.alias_(value, field))
                records.append(exp.select(*fields))
            body = records[0]
            if not rows:
                body = body.where(exp.false())
            for record in records[1:]:
                body = exp.union(body, record, distinct=False)
            source.replace(body.subquery(source.alias_or_name))
            seen.add(name)
    if not seen or any(
        isinstance(source, exp.Table)
        for scope in traverse_scope(tree)
        for _, source in scope.selected_sources.values()
    ):
        raise ValueError("未能隔离全部物理表")
    return tree.sql(dialect="doris")


async def replay(sql, tables):
    statement = fixture_sql(sql, tables)
    client = DorisClientManager(cfg.doris)
    try:
        async with client.engine.connect() as con:
            return [
                dict(row) for row in (await con.exec_driver_sql(statement)).mappings()
            ]
    finally:
        await client.close()


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    from dataagent.engines.common.async_runtime import run_async
    from evals.amount_fixture import september_amounts

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path, help="本组 audit.json")
    parser.add_argument("execution", help="完整执行 ID")
    parser.add_argument(
        "resource",
        choices=["payment", "sku", "sku-october", "amounts", "amounts-no-refunds"],
    )
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    record = next(
        (r for r in audit if r["id"] == args.execution and r["status"] == "succeeded"),
        None,
    )
    if record is None:
        parser.error("审计中找不到这次成功执行")
    data = {
        "payment": PAYMENT,
        "sku": SKU,
        "sku-october": after_october_delisting(),
        "amounts": september_amounts(),
        "amounts-no-refunds": september_amounts(successful_refund=False),
    }[args.resource]
    print(
        json.dumps(
            run_async(replay(record["raw_sql"], data)), default=str, ensure_ascii=False
        )
    )
