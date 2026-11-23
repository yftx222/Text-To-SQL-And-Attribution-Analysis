from evals.counterexamples import binding, check_record, outcome, suite


def test_first_pass_never_requires_a_counterexample_plan(tmp_path):
    import asyncio

    from evals.counterexamples import run

    group = tmp_path / "first"
    group.mkdir()
    (group / "manifest.json").write_text(
        '{"stage":"first_pass","cases":["Q"]}', encoding="utf8"
    )
    asyncio.run(run(tmp_path, "first"))
    assert not (group / "counterexamples.json").exists()


def test_ranked_prefix_requires_correct_membership_and_order():
    rows = [{"id": i} for i in (2, 1, 3)]
    assert outcome(rows, ["id"], [[2], [1]], ranked_prefix=True) == "pass"
    assert outcome(rows, ["id"], [[1], [2]], ranked_prefix=True) == "fail"
    assert outcome(rows, ["id"], [[2], [3]], ranked_prefix=True) == "fail"


def test_summary_row_requires_unique_label():
    rows = [{"part": "total", "n": 8}, {"part": "detail", "n": 1}]
    key = {"part": "total"}
    assert outcome(rows, ["n"], [8], row_equals=key) == "pass"
    assert outcome(rows[1:], ["n"], [8], row_equals=key) == "fail"
    assert outcome(rows + rows[:1], ["n"], [8], row_equals=key) == "fail"


def test_grouped_replay_checks_labels_counts_and_duplicate_groups():
    expected = [["PAID", 1], ["SHIPPED", 2]]
    cols = ["state", "orders"]
    rows = [{"state": "SHIPPED", "orders": 2}, {"state": "PAID", "orders": 1}]
    assert outcome(rows, cols, expected) == "pass"
    assert outcome(rows + rows[:1], cols, expected) == "fail"
    assert outcome(rows[:1], cols, expected) == "fail"
    assert outcome([{"orders": 1}], cols, expected) == "review"


def test_replay_gate_requires_the_same_sql_trial_fixture_and_observed_values():
    variants = suite({"Q": [{"fixture": "sessions", "expected": [1, 2]}]}, "Q")
    query = {"id": "execution", "raw_sql": "select 1"}
    cols = ["num", "den"]
    key = binding("Q", "trial", "journal", query, cols, variants)
    item = {"fixture": "sessions", "rows": [{"num": 1, "den": 2}]}
    proof = {"binding": key, "results": [item]}
    assert check_record(proof, key, cols, variants) == "pass"
    assert check_record({}, key, cols, variants) == "review"
    for fingerprint in (
        binding("Q", "different-trial", "journal", query, cols, variants),
        binding(
            "Q", "trial", "journal", {**query, "raw_sql": "select 2"}, cols, variants
        ),
        binding("Q", "trial", "journal", query, cols, [(variants[0][0], {})]),
    ):
        assert check_record(proof, fingerprint, cols, variants) == "review"
    item.update(rows=[{"num": 2, "den": 2}], status="pass")
    assert check_record(proof, key, cols, variants) == "fail"
    item.update(rows=[], error="missing table")
    assert check_record(proof, key, cols, variants) == "review"
