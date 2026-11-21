import pytest
from sqlglot import exp, parse_one
from sqlglot.optimizer.scope import traverse_scope

from evals.time_scope_fixture import PAYMENT, fixture_sql


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
