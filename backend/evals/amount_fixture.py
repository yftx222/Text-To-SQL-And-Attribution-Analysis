"""Small September warehouse slice with known answers for AMT-02 and REF-04."""


def september_amounts(*, successful_refund=True):
    august = "2026-08-31 19:00:00"
    september = "2026-09-15 10:00:00"
    last_day = "2026-09-30 23:59:59"
    october = "2026-10-01 00:00:00"
    refund_success_time = last_day if successful_refund else october

    def events(id_field, status_field, records):
        return (
            f"{id_field} {status_field} event_seq_no event_time biz_date",
            [(key, state, seq, time, time[:10]) for key, state, seq, time in records],
        )

    return {
        "dwd_trade_order_detail_di": (
            "order_id order_detail_id receivable_amount order_create_time biz_date",
            [
                (100, 101, 60, september, september[:10]),
                (100, 102, 40, september, september[:10]),
                (200, 201, 80, september, september[:10]),
            ],
        ),
        "dwd_trade_order_status_event_di": events(
            "order_id",
            "after_order_status",
            [
                (100, "PAID", 1, last_day),
                (100, "CANCELLED", 2, october),
                (200, "CREATED", 1, september),
                (200, "CANCELLED", 2, last_day),
            ],
        ),
        "dwd_trade_pay_detail_di": (
            "pay_detail_id pay_amount pay_request_time biz_date",
            [(1, 100, september, september[:10]), (2, 80, last_day, last_day[:10])],
        ),
        "dwd_trade_pay_order_detail_di": (
            "pay_detail_id order_id order_detail_id allocated_pay_amount pay_request_time biz_date",
            [
                (1, 100, 101, 60, september, september[:10]),
                (1, 100, 102, 40, september, september[:10]),
                (2, 200, 201, 80, last_day, last_day[:10]),
            ],
        ),
        "dwd_trade_pay_status_event_di": events(
            "pay_detail_id",
            "after_pay_status",
            [
                (1, "SUCCESS", 1, last_day),
                (2, "PENDING", 1, last_day),
                (2, "SUCCESS", 2, october),
            ],
        ),
        "dwd_trade_refund_pay_detail_di": (
            "refund_pay_detail_id original_pay_detail_id refund_detail_id order_id refund_amount refund_pay_request_time biz_date",
            [
                (11, 1, 21, 100, 10, september, september[:10]),
                (12, 1, 22, 100, 5, last_day, last_day[:10]),
                (13, 1, 23, 100, 7, august, august[:10]),
            ],
        ),
        "dwd_trade_refund_pay_status_event_di": events(
            "refund_pay_detail_id",
            "after_refund_pay_status",
            [
                (11, "PENDING", 1, september),
                (11, "SUCCESS", 2, refund_success_time),
                (12, "PENDING", 1, last_day),
                (12, "SUCCESS", 2, october),
                (13, "SUCCESS", 1, september),
            ],
        ),
    }
