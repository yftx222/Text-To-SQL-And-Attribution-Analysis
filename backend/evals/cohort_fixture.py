"""Run the cited numerator on a small dataset with an out-of-cohort event."""

import sqlite3
from contextlib import closing


def cohort_counts(case, sql):
    if case == "SEA-05":
        setup = """
        CREATE TABLE dwd_traffic_search_di(search_detail_id, biz_date, is_search_success);
        CREATE TABLE dwd_traffic_search_click_di(search_detail_id, biz_date);
        INSERT INTO dwd_traffic_search_di VALUES (1,'2026-09-10',1),(2,'2026-09-10',1),(3,'2026-08-31',1);
        INSERT INTO dwd_traffic_search_click_di VALUES (1,'2026-09-10'),(3,'2026-09-01');
        """
        columns = ("numerator_value", "denominator_value")
    elif case == "XDM-01":
        setup = """
        CREATE TABLE dwd_traffic_session_di(session_id, session_start_time);
        CREATE TABLE dwd_trade_order_detail_di(order_id, source_session_id, biz_date);
        CREATE TABLE dwd_trade_order_status_event_di(order_id,after_order_status,event_time,event_seq_no);
        CREATE TABLE dwd_trade_pay_status_event_di(pay_detail_id,after_pay_status,event_time,event_seq_no);
        CREATE TABLE dwd_trade_pay_order_detail_di(order_id,pay_detail_id);
        INSERT INTO dwd_traffic_session_di VALUES (1,'2026-09-10'),(2,'2026-09-10'),(3,'2026-08-31');
        INSERT INTO dwd_trade_order_detail_di VALUES (10,1,'2026-09-10'),(11,3,'2026-09-01');
        """
        columns = ("num_sessions_all_orders", "denom_sessions_sept")
    else:
        raise ValueError(case)
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(setup)
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        db.create_function("concat", -1, lambda *args: "".join(map(str, args)))
        db.set_authorizer(
            lambda op, *_: (
                sqlite3.SQLITE_OK
                if op
                in (
                    sqlite3.SQLITE_READ,
                    sqlite3.SQLITE_SELECT,
                    sqlite3.SQLITE_FUNCTION,
                    sqlite3.SQLITE_RECURSIVE,
                )
                else sqlite3.SQLITE_DENY
            )
        )
        db.set_progress_handler(iter([0] * 1000 + [1]).__next__, 1000)
        rows = db.execute(sql).fetchmany(2)
        if len(rows) != 1 or not set(columns) <= set(rows[0].keys()):
            raise ValueError("未识别主结果列")
        return [rows[0][k] for k in columns]
