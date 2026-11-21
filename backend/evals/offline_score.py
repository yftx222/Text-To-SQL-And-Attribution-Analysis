"""Score observed facts independently of the adapter that collected them."""

import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = "offline-v1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_cards():
    cases = read(HERE / "pilot_cases.json")
    expected = read(HERE / "scorecards.json")
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ValueError("题库与评分卡题号不一致")
    return {case["id"]: {**case, "facts": expected[case["id"]]} for case in cases}


def compare(expected, observed):
    if observed is None:
        return "review"
    if isinstance(
        expected, str
    ):  # Amount or percentage, rounded to the published precision.
        try:
            number = Decimal(str(observed).replace(",", ""))
            target = Decimal(expected)
            matches = number.quantize(target, rounding=ROUND_HALF_UP) == target
        except InvalidOperation:
            return "review"
    else:
        matches = type(expected) is type(observed) and expected == observed
    return "pass" if matches else "fail"


def verdict(statuses):
    if "fail" in statuses:
        return "fail"
    if "review" in statuses or "pass" not in statuses:
        return "review"
    return "pass"


def score(card, trial_id, facts):
    expected = {**card["facts"], "sql_scope": True, "first_delivery": True}
    rows = []
    for name, target in expected.items():
        observed, source = facts.get(name, (None, None))
        status = compare(target, observed) if source else "review"
        rows.append(
            {
                "case_id": card["id"],
                "trial_id": trial_id,
                "assertion": name,
                "expected": target,
                "observed": observed,
                "evidence": source,
                "status": status,
                "scorer": VERSION,
            }
        )
    business = verdict(
        [row["status"] for row in rows if row["assertion"] != "first_delivery"]
    )
    return rows, business, verdict([business, rows[-1]["status"]])
