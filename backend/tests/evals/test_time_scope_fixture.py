import pytest
from sqlglot import exp, parse_one
from sqlglot.optimizer.scope import traverse_scope

from evals.time_scope_fixture import (
    PAYMENT,
    SESSION_COHORT,
    fixture_sql,
    with_placeholder_sku,
)


def test_replays_cte_and_self_join_without_reading_warehouse_tables():
    sql = """
    WITH attempts AS (SELECT pay_detail_id FROM dwd_trade_pay_detail_di)
    SELECT a.pay_detail_id FROM attempts a
    JOIN dwd_trade_pay_detail_di p ON p.pay_detail_id = a.pay_detail_id
    WHERE p.pay_request_time < '2026-10-01'
    """
    isolated = parse_one(fixture_sql(sql, PAYMENT), read="doris")
    assert len(list(isolated.find_all(exp.Join))) == 1
    assert "2026-10-01" in isolated.sql()
    assert all(
        not isinstance(source, exp.Table)
        for scope in traverse_scope(isolated)
        for _, source in scope.selected_sources.values()
    )


def test_missing_fixture_never_falls_back_to_real_table():
    with pytest.raises(ValueError, match="缺少反例表"):
        fixture_sql("SELECT * FROM another_table", PAYMENT)


def test_fixture_keeps_last_fractional_second_of_month():
    query = fixture_sql(
        "SELECT order_create_time FROM orders",
        {"orders": ("order_create_time", [("2026-09-30 23:59:59.500000",)])},
    )
    assert "DATETIME(6)" in query
    assert "23:59:59.500000" in query


def test_boundary_data_preserve_session_key_type_and_on_sale_placeholder():
    sql = fixture_sql(
        "SELECT source_session_id FROM dwd_trade_order_detail_di WHERE source_session_id <> ''",
        SESSION_COHORT,
    )
    tree = parse_one(sql, read="doris")
    types = [
        a.this.to.this
        for a in tree.find_all(exp.Alias)
        if a.alias == "source_session_id" and isinstance(a.this, exp.Cast)
    ]
    assert types and all(t == exp.DataType.Type.VARCHAR for t in types)
    keys, rows = with_placeholder_sku()["dim_sku_info_zip"]
    members = [dict(zip(keys.split(), row, strict=True)) for row in rows]
    assert any(
        m["sku_id"] == 0 and m["sku_status"] == "在售" and not m["is_deleted"]
        for m in members
    )
