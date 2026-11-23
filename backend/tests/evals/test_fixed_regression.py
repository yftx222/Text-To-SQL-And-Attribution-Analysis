import hashlib
import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import SystemMessage
import pytest

from dataagent.engines.agent.specialist.delivery_clock import (
    closing_request,
    delivery_clock,
)
from evals.fixed_regression import GROUPS, prepare, quality, paired
from evals.service_extension import fixture, reference, review_pack


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf8")


def lines(path, records):
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf8")


def test_clock_scoped_and_keeps_system_rules():
    req = ModelRequest(
        model=None, messages=[], system_message=SystemMessage(content="原权限规则")
    )
    with patch(
        "dataagent.engines.agent.specialist.delivery_clock.monotonic",
        side_effect=[0, 74, 75],
    ):
        with delivery_clock(100):
            assert closing_request(req, "analyst") is req
            assert closing_request(req, "explorer") is req
            assert closing_request(req, "reviewer").system_message.content.startswith(
                "原权限规则\n"
            )
    assert closing_request(req, "reviewer") is req


def test_clock_preserves_structured_system_content():
    from dataagent.engines.common.llm import DataAgentDeepSeekResponses

    system = SystemMessage(content=[{"type": "text", "text": "系统规则"}])
    req = ModelRequest(model=None, messages=[], system_message=system)
    with patch(
        "dataagent.engines.agent.specialist.delivery_clock.monotonic",
        side_effect=[0, 75],
    ):
        with delivery_clock(100):
            blocks = closing_request(req, "reviewer").system_message.content
    assert all(block["type"] == "text" for block in blocks)
    assert blocks[0] == system.content[0]
    model = DataAgentDeepSeekResponses(
        model="offline", api_key="unit-test", use_responses_api=True
    )
    payload = model._get_request_payload([SystemMessage(content=blocks)])
    assert payload["input"][0]["content"][-1]["type"] == "input_text"


def test_fixture_boundary_and_access():
    pack = review_pack()
    assert len(pack) == 8 and sum(map(len, pack.values())) == 10
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        fixture(db)
        assert reference(db, "fault", "C01", scope="B") == {"status": "scope_denied"}
        assert reference(db, "sla", None, principal="unknown") == {
            "status": "scope_denied"
        }


def test_fixed_plan_can_only_be_reused_unchanged(tmp_path):
    plan = prepare(tmp_path)
    assert sum(map(len, GROUPS.values())) == len(plan["cards"]) == 20
    assert plan == prepare(tmp_path)
    with pytest.raises(ValueError):
        prepare(tmp_path, ["planner/tools/delivery.py"])


def make_arm(folder, candidate=False):
    plan = prepare(folder, ["delivery.py"])
    root = folder / "first"
    root.mkdir()
    before = dict(
        tables={},
        metadata={},
        es={},
        source=folder.name,
        config="config",
        models={},
        limits={},
    )
    for name in ["before", "after"]:
        write(folder / (name + ".json"), before)
    write(
        root / "manifest.json",
        dict(
            stage="first_pass",
            rubric_cases=True,
            cases=list(plan["cards"]),
            question_texts={k: c["messages"] for k, c in plan["cards"].items()},
            seconds=1950,
            principal="pilot",
            cards="cards",
            questions="questions",
            fixed_scoring=plan["scoring_sha256"],
        ),
    )
    write(root / "scoring_receipt.json", plan["scoring_sha256"])
    trials, scores = [], []
    for key, card in plan["cards"].items():
        ident = folder.name + key
        turns = [{"answer": "ok", "status": "done"} for msg in card["messages"]]
        receipts = []
        for i, turn in enumerate(turns, 1):
            data = (key + str(i)).encode()
            digest = hashlib.sha256(data).hexdigest()
            file = digest + ".artifact"
            (root / file).write_bytes(data)
            receipts.append(
                dict(
                    file=file,
                    sha256=digest,
                    expected=digest,
                    verified=True,
                    attached=True,
                    turn=1 if candidate and key == "RPT-06" else i,
                )
            )
        trials.append(
            dict(
                case=key,
                trial=ident,
                cid=ident,
                business="pass",
                started=True,
                first_correct_delivery="pass",
                closed=True,
                turns=turns,
                sql={"succeeded": 1},
                deliveries=receipts,
            )
        )
        for assertion in ("sql_scope", "rubric_met"):
            scores.append(
                dict(case_id=key, trial_id=ident, assertion=assertion, status="pass")
            )
    lines(root / "trials.jsonl", trials)
    lines(root / "scores.jsonl", scores)
    return plan


def test_paired_detects_missing_second_delivery_and_budget_drift(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    make_arm(old)
    plan = make_arm(new, True)
    result = paired(old, new)
    assert result["metrics"]["report_delivery"]["new_pass"] == 5
    assert result["end_to_end_changes"]["RPT-06"] == ["pass", "fail"]
    digest = hashlib.sha256(b"RPT-011").hexdigest()
    (new / "first" / (digest + ".artifact")).write_bytes(b"corrupt")
    assert quality(new, plan)["RPT-01"]["report_delivery"] == "fail"
    for name in ("before", "after"):
        p = new / (name + ".json")
        write(p, {**json.loads(p.read_text()), "limits": {"seconds": 10}})
    with pytest.raises(ValueError, match="预算"):
        paired(old, new)
