"""Compare first attempts on matching inputs, retaining old and new evidence IDs."""

import argparse
from pathlib import Path

from .live_report import rows, save
from .offline_score import read


def compare(left, right):
    a, b = (read(p / "before.json") for p in (left, right))
    changed = [k for k in ("tables", "metadata", "es") if a[k] != b[k]]
    for folder, before in ((left, a), (right, b)):
        after = read(folder / "after.json")
        changed.extend(
            k
            for k in (
                "tables",
                "metadata",
                "es",
                "source",
                "config",
                "models",
                "limits",
            )
            if after[k] != before[k]
        )
    versions = [read(p / "manifest.json")["scoring_sha256"] for p in (left, right)]
    scoring_files = (
        "scorecards.json",
        "pilot_cases.json",
        "offline_score.py",
        "live_report.py",
        "score_replay.py",
        "pay_fixture.py",
    )
    changed.extend(
        name for name in scoring_files if versions[0][name] != versions[1][name]
    )
    plans = [read(p / "first/manifest.json") for p in (left, right)]
    changed.extend(
        k
        for k in (
            "cards",
            "questions",
            "principal",
            "seconds",
            "cases",
            "question_texts",
            "parallel_trials",
        )
        if plans[0].get(k) != plans[1].get(k)
    )
    if changed:
        raise ValueError("缺少同条件对照：" + "，".join(sorted(set(changed))))
    planned = plans[0]["cases"]
    groups = [
        {r["case"]: r for r in rows(p / "first/trials.jsonl")} for p in (left, right)
    ]
    if any(set(g) != set(planned) for g in groups):
        raise ValueError("两批首轮的题目未全部登记")
    pairs = []
    for key in planned:
        old, new = [g[key] for g in groups]
        if old["trial"] == new["trial"]:
            raise ValueError("同一尝试不能用作两个版本的独立对照")
        pairs.append(
            {
                "case": key,
                "old_trial": old["trial"],
                "new_trial": new["trial"],
                "old": old["business"],
                "new": new["business"],
            }
        )
    return {
        "version_changes": [
            k for k in ("source", "models", "config", "limits") if a[k] != b[k]
        ],
        "pairs": pairs,
        "counts": {
            label: sum((r["old"], r["new"]) == key for r in pairs)
            for key, label in [
                (("fail", "pass"), "旧错新对"),
                (("pass", "fail"), "旧对新错"),
                (("pass", "pass"), "两者均对"),
                (("fail", "fail"), "两者均错"),
            ]
        },
        "pending": sum("review" in (r["old"], r["new"]) for r in pairs),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    save(args.output, compare(args.old, args.new))
