from evals.cohort_fixture import cohort_counts


def test_search_click_must_belong_to_september_successful_request():
    select = """SELECT count(distinct c.search_detail_id) AS numerator_value,
        (SELECT count(*) FROM dwd_traffic_search_di WHERE biz_date >= '2026-09-01'
        AND is_search_success=1) AS denominator_value FROM dwd_traffic_search_click_di c"""
    assert cohort_counts("SEA-05", select) == [2, 2]
    assert cohort_counts("SEA-05", select + """ JOIN dwd_traffic_search_di s
        ON c.search_detail_id=s.search_detail_id WHERE s.is_search_success=1
        AND s.biz_date >= '2026-09-01'""") == [1, 2]


def test_order_must_belong_to_session_started_in_september():
    select = """SELECT count(distinct o.source_session_id) AS num_sessions_all_orders,
        (SELECT count(*) FROM dwd_traffic_session_di WHERE session_start_time >= '2026-09-01')
        AS denom_sessions_sept FROM dwd_trade_order_detail_di o"""
    assert cohort_counts("XDM-01", select) == [2, 2]
    assert cohort_counts("XDM-01", select + """ JOIN dwd_traffic_session_di s
        ON o.source_session_id=s.session_id WHERE s.session_start_time >= '2026-09-01'""") == [1, 2]
