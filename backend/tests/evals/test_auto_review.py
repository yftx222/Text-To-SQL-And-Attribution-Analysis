from evals.auto_review import answer_facts, submitted_query
from evals.offline_score import compare


def test_wrong_headline_is_scored_even_when_correct_value_appears_as_an_alternative():
    turns = [
        {"answer": "支付成功率 = 75%\n| 分子 | 3 |\n| 分母 | 4 |\n备选口径 96.46%"}
    ]
    facts = answer_facts("PAY-04", turns, "trial#stream")
    assert facts["num"][0] == 3
    assert facts["den"][0] == 4
    assert compare("96.46", facts["percent"][0]) == "fail"


def test_uncited_or_ambiguous_query_cannot_supply_counterexample_evidence():
    q = {
        "id": "a",
        "tool_call_id": "one",
        "status": "succeeded",
        "result_summary": {"path": "a.csv", "columns": [{"name": "orders"}]},
    }
    t = {"name": "execute_sql", "call": "one", "result": {"execution_id": "a"}}
    assert submitted_query([q], [t], "", ["orders"]) is None
    submit = {"name": "submit_task_result", "args": {"evidence": ["a.csv"]}}
    assert submitted_query([q], [t, submit], "", ["orders"]) is q
    other = {
        **q,
        "id": "b",
        "tool_call_id": "two",
        "result_summary": {**q["result_summary"], "path": "b.csv"},
    }
    tools = [
        t,
        submit,
        {"name": "execute_sql", "call": "two", "result": {"execution_id": "b"}},
        {"name": "submit_task_result", "args": {"evidence": ["b.csv"]}},
    ]
    assert submitted_query([q, other], tools, "", ["orders"]) is None
    assert submitted_query([q, other], tools, "b", ["orders"]) is other
