"""Run the answer-producing SELECT against isolated, fixed payment events."""

import sqlite3
from contextlib import closing

REFERENCE_SQL = """
SELECT COUNT(*) AS total_pay_attempts,
       SUM(CASE WHEN (
           SELECT e.after_pay_status FROM dwd_trade_pay_status_event_di e
           WHERE e.pay_detail_id = p.pay_detail_id AND e.event_time < '2026-10-01'
           ORDER BY e.event_seq_no DESC LIMIT 1
       ) = 'SUCCESS' THEN 1 ELSE 0 END) AS success_pay_attempts
FROM dwd_trade_pay_detail_di p
WHERE p.biz_date >= '2026-09-01' AND p.biz_date < '2026-10-01'
"""


def payment_counts(
    sql, *, crossing=True, columns=("success_pay_attempts", "total_pay_attempts")
):
    """Returns [successful, attempted]. Raises sqlite3.Error for unsupported SQL."""
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript("""
            CREATE TABLE dwd_trade_pay_detail_di
                (pay_detail_id INTEGER PRIMARY KEY, biz_date TEXT, pay_request_time TEXT,
                 requested_pay_amount NUMERIC DEFAULT 10);
            CREATE TABLE dwd_trade_pay_status_event_di
                (pay_detail_id INTEGER, after_pay_status TEXT, event_seq_no INTEGER,
                 event_time TEXT, biz_date TEXT);
        """)
        count = 2 if crossing else 198
        successes = 1 if crossing else 191
        for i in range(1, count + 1):
            db.execute(
                "INSERT INTO dwd_trade_pay_detail_di(pay_detail_id, biz_date, pay_request_time) VALUES (?, '2026-09-30', '2026-09-30 20:00')",
                (i,),
            )
            state = "SUCCESS" if i <= successes else "FAILED"
            db.execute(
                "INSERT INTO dwd_trade_pay_status_event_di VALUES (?, ?, 1, '2026-09-30 21:00', '2026-09-30')",
                (i, state),
            )
        if crossing:
            db.execute(
                "INSERT INTO dwd_trade_pay_status_event_di VALUES (2, 'SUCCESS', 2, '2026-10-01 00:01', '2026-10-01')"
            )
        allowed = {
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE,
        }
        db.set_authorizer(
            lambda op, *_: sqlite3.SQLITE_OK if op in allowed else sqlite3.SQLITE_DENY
        )
        db.set_progress_handler(iter([0] * 1000 + [1]).__next__, 1000)
        db.row_factory = sqlite3.Row
        rows = db.execute(sql).fetchmany(2)
        if (
            len(columns) != 2
            or len(set(columns)) != 2
            or len(rows) != 1
            or not set(columns) <= set(rows[0].keys())
        ):
            raise ValueError("需要一行结果以及可识别的成功笔数、总尝试数两列")
        return [rows[0][key] for key in columns]
