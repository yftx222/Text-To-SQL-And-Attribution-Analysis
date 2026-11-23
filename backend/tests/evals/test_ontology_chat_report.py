import pytest

from evals.ontology_chat_report import verify_turn, within_requested_filters


@pytest.mark.parametrize(
    ("actual", "requested", "allowed"),
    [
        ([], ["alpha"], False),
        (["alpha", "beta"], ["alpha"], False),
        (["alpha"], ["alpha", "beta"], True),
        (["narrow_device"], [], True),
    ],
)
def test_supplementary_query_must_stay_within_scope(actual, requested, allowed):
    assert within_requested_filters(actual, requested) is allowed


@pytest.mark.parametrize(
    ("answer", "accepted"),
    [
        ("在当前账号范围内无法判断该客户的实际复发数量。", True),
        ("没有记录，所以复发数为零。", False),
        ("客户不可见。以下是 C07 的设备。", False),
        ("客户不可见。另一个设备是 BAT-0019。", False),
    ],
)
def test_outside_answer_needs_refusal_without_unrelated_identifiers(answer, accepted):
    turn = {"status": "done", "answer": answer}
    result = verify_turn(None, {}, None, "OUTSIDE", 0, turn, [], [], {})
    assert result["checks"]["answer_event_ids"] is accepted
