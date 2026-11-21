import sqlite3
from decimal import Decimal

import pytest

from evals.offline_score import HERE, compare, load_cards, read, score
from evals.pay_fixture import REFERENCE_SQL, payment_counts
from evals.score_replay import numeric_facts


def test_cards_cover_all_turns():
    cards = load_cards()
    assert len(cards) == 18
    assert sum(len(card["turns"]) for card in cards.values()) == 19


def test_order_amount_and_attribution_reference_queries():
    from contextlib import closing

    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript("""
            CREATE TABLE dwd_trade_order_detail_di
                (order_id INTEGER, order_create_time TEXT, receivable_amount INTEGER, channel_sk INTEGER);
            CREATE TABLE dim_channel_info (channel_sk INTEGER, channel_code TEXT);
            INSERT INTO dim_channel_info VALUES (1, 'APP'), (2, 'PC');
            INSERT INTO dwd_trade_order_detail_di VALUES
                (1,'2026-09-01',10,1), (1,'2026-09-01',20,1), (2,'2026-09-15',8,2),
                (3,'2026-09-16',40,1), (4,'2026-09-30',3,2), (5,'2026-10-01',100,2);
        """)
        queries = [
            sql
            for sql in (HERE / "reference_queries.sql")
            .read_text(encoding="utf-8")
            .split(";")
            if sql.strip()
        ]
        orders, amount, channels = [conn.execute(sql).fetchall() for sql in queries]
    assert orders == [(4,)]
    assert amount == [(81,)]
    assert dict(channels) == {"APP": 10, "PC": -5, "ALL": 5}


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        ("全站下单 **206 笔**。明细 301 行", "pass"),
        ("全站下单 **301 笔**。另有 206 名访客", "fail"),
        ("查询返回 206 和 301 两个数", "review"),
    ],
)
def test_orders_need_labeled_claim(claim, expected):
    facts = numeric_facts("ORD-01", claim, "answer")
    rows, _, _ = score(load_cards()["ORD-01"], "trial", facts)
    assert rows[0]["status"] == expected


def test_missing_evidence_and_rounding():
    card = load_cards()["ORD-01"]
    rows, business, delivery = score(
        card, "trial", {"orders": (206, None), "first_delivery": (False, "events")}
    )
    assert rows[0]["status"] == business == "review"
    assert delivery == "fail"
    assert compare("96.46", "96.4646") == "pass"
    assert compare("96.46", "96.465") == "fail"


def test_payment_counts_catch_events_after_period_end():
    without_cutoff = REFERENCE_SQL.replace(" AND e.event_time < '2026-10-01'", "")
    assert (
        payment_counts(without_cutoff, crossing=False)
        == payment_counts(REFERENCE_SQL, crossing=False)
        == [191, 198]
    )
    assert payment_counts(without_cutoff) == [2, 2]
    assert payment_counts(REFERENCE_SQL) == [1, 2]
    with pytest.raises((sqlite3.Error, ValueError)):
        payment_counts("SELECT * FROM missing_table")


def test_opposite_errors_in_channel_amounts():
    card = load_cards()["ATT-01"]
    reference = read(HERE / "scorecards.json")["ATT-01"]
    amounts = {
        k: Decimal(reference[k])
        for k in ("APP", "PC", "MINI_PROGRAM", "H5", "SEM", "ALL")
    }
    amounts["APP"] += 100
    amounts["PC"] -= 100
    facts = {k: (str(v), "report") for k, v in amounts.items()}
    facts["balanced"] = (
        sum(v for k, v in amounts.items() if k != "ALL") == amounts["ALL"],
        "report",
    )
    rows, business, _ = score(card, "trial", facts)
    statuses = {r["assertion"]: r["status"] for r in rows}
    assert statuses["balanced"] == "pass"
    assert statuses["APP"] == statuses["PC"] == business == "fail"
    assert statuses["delivered"] == "review"
