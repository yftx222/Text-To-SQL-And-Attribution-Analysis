"""Prepare a paired first-pass regression, or compare two completed runs. Never calls an Agent."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from .catalog import catalog
from .offline_score import HERE, read, verdict


GROUPS = {
    "query": ["ORD-01", "AMT-09", "PAY-04", "STA-04", "REF-04", "PRD-05"],
    "dialog": ["CNV-03", "CNV-08", "CNV-09", "CNV-11"],
    "analysis": ["ATT-02", "ATT-03", "ATT-04", "ATT-05"],
    "delivery": [f"RPT-{i:02}" for i in range(1, 7)],
}


def impact(paths):
    selected = set()
    for path in paths:
        path = path.replace("\\", "/").lower()
        if any(s in path for s in ("analyst", "reviewer", "delivery", "artifact")):
            selected.update(("analysis", "delivery"))
        elif any(s in path for s in ("conversation", "message_context")):
            selected.update(("dialog", "analysis", "delivery"))
        else:
            selected.update(GROUPS)
    return [group for group in GROUPS if group in selected]


def scoring_hashes():
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(HERE.iterdir())
        if p.suffix in {".py", ".json", ".sql"} or p.name == "rubric.md"
    }


def prepare(folder, paths=()):
    groups = impact(paths) if paths else list(GROUPS)
    cards = catalog()
    selected = {key: cards[key] for group in groups for key in GROUPS[group]}
    plan = {
        "groups": groups,
        "cards": selected,
        "seconds": 1950,
        "jobs": 1,
        "scoring_sha256": scoring_hashes(),
    }
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "fixed_plan.json"
    if target.exists() and read(target) != plan:
        raise ValueError("目录已有不同计划，请使用新目录")
    if not target.exists():
        with target.open("x", encoding="utf8") as out:
            json.dump(plan, out, ensure_ascii=False, indent=2)
    return plan


def verified_files(root, trial):
    found = set()
    for receipt in trial.get("deliveries", []):
        digest = receipt.get("sha256")
        if not digest or not receipt.get("verified") or not receipt.get("attached"):
            continue
        if (
            receipt.get("file") != digest + ".artifact"
            or receipt.get("expected") != digest
        ):
            continue
        path = root / receipt["file"]
        if (
            path.parent == root
            and path.is_file()
            and path.stat().st_size
            and hashlib.sha256(path.read_bytes()).hexdigest() == digest
        ):
            found.add((receipt.get("turn"), digest))
    return found


def quality(folder, plan):
    from .live_report import rows

    root = folder / "first"
    trials = rows(root / "trials.jsonl")
    scores = rows(root / "scores.jsonl")
    by_case = {t["case"]: t for t in trials}
    if len(trials) != len(by_case) or set(by_case) != set(plan["cards"]):
        raise ValueError("首次试次缺失或重复")
    details = {}
    for key, card in plan["cards"].items():
        t = by_case[key]
        rules = {
            r["assertion"]: r["status"]
            for r in scores
            if r["case_id"] == key and r["trial_id"] == t["trial"]
        }
        sql = (
            rules.get("sql_scope", "review")
            if t.get("sql", {}).get("succeeded")
            else "fail"
        )
        required = [t["first_correct_delivery"], sql]
        if (
            not t["closed"]
            or len(t["turns"]) != len(card["messages"])
            or not all(
                turn.get("answer") and turn.get("status") == "done"
                for turn in t["turns"]
            )
        ):
            required.append("fail")
        record = {"sql_scope": sql}
        if key in GROUPS["delivery"]:
            files = verified_files(root, t)
            required_turns = set(range(1, len(card["messages"]) + 1))
            record["report_delivery"] = verdict(
                [
                    rules.get("rubric_met", "review"),
                    "pass"
                    if required_turns <= {turn for turn, digest in files}
                    and len({digest for turn, digest in files}) >= len(required_turns)
                    else "fail",
                ]
            )
            required.append(record["report_delivery"])
        record["end_to_end"] = verdict(required)
        details[key] = record
    return details


def paired(old, new):
    from .compare import compare
    from .live_report import rows

    plan = read(old / "fixed_plan.json")
    if plan != read(new / "fixed_plan.json"):
        raise ValueError("两个版本必须使用同一份冻结题单和判分规则")
    sessions, attempts = set(), set()
    for folder in (old, new):
        manifest = read(folder / "first/manifest.json")
        if (
            manifest.get("fixed_scoring") != plan["scoring_sha256"]
            or read(folder / "first/scoring_receipt.json") != plan["scoring_sha256"]
        ):
            raise ValueError("运行或判分程序版本与冻结版本不一致")
        if (
            manifest.get("stage") != "first_pass"
            or not manifest.get("rubric_cases")
            or manifest["cases"] != list(plan["cards"])
            or manifest["seconds"] != plan["seconds"]
            or manifest.get("parallel_trials", 1) != plan["jobs"]
            or manifest.get("question_texts")
            != {k: v["messages"] for k, v in plan["cards"].items()}
        ):
            raise ValueError("真实执行清单与冻结清单不一致")
        for trial in rows(folder / "first/trials.jsonl"):
            if trial["trial"] in attempts or (
                trial["cid"] and trial["cid"] in sessions
            ):
                raise ValueError("版本对照不能复用会话或试次")
            attempts.add(trial["trial"])
            sessions.add(trial["cid"])
    result = compare(old, new, frozen_scoring=plan["scoring_sha256"])
    if {"models", "limits", "config"}.intersection(result["version_changes"]):
        raise ValueError("本轮同条件对照要求模型、配置和执行预算一致")
    before, after = quality(old, plan), quality(new, plan)
    metrics = {}
    for name in ("end_to_end", "sql_scope", "report_delivery"):
        keys = [k for k in before if name in before[k]]
        if not keys:
            continue
        counts = [
            sum(data[k][name] == "pass" for k in keys) for data in (before, after)
        ]
        metrics[name] = {
            "denominator": len(keys),
            "old_pass": counts[0],
            "new_pass": counts[1],
            "delta_pp": round(100 * (counts[1] - counts[0]) / len(keys), 2),
            "pending": [
                sum(data[k][name] == "review" for k in keys) for data in (before, after)
            ],
        }
    movements = {k: [before[k]["end_to_end"], after[k]["end_to_end"]] for k in before}
    scenarios = {}
    for group in plan["groups"]:
        keys = GROUPS[group]
        scenarios[group] = {
            "tasks": len(keys),
            "old_pass": sum(before[k]["end_to_end"] == "pass" for k in keys),
            "new_pass": sum(after[k]["end_to_end"] == "pass" for k in keys),
        }
    return {
        "metrics": metrics,
        "scenarios": scenarios,
        "end_to_end_changes": movements,
        "old": before,
        "new": after,
        "trial_comparison": result,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("folder", type=Path)
    p.add_argument("--changed", action="append", default=[])
    p.add_argument(
        "--since", help="Git revision to compare to the current working tree"
    )
    p = sub.add_parser("compare")
    p.add_argument("old", type=Path)
    p.add_argument("new", type=Path)
    p.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        paths = args.changed.copy()
        if args.since:
            repo = HERE.parents[1]
            for cmd in (
                ["diff", "--name-only", args.since, "--"],
                ["ls-files", "--others", "--exclude-standard"],
            ):
                paths.extend(
                    subprocess.check_output(
                        ["git", "-C", str(repo), *cmd], text=True
                    ).splitlines()
                )
        plan = prepare(args.folder, paths)
        print("已冻结", len(plan["cards"]), "题；未启动会话。")
        print(
            "python -m evals.live",
            '"' + args.folder.as_posix() + '"',
            "--catalog --first-pass --seconds 1950 --jobs 1",
            " ".join("--case " + k for k in plan["cards"]),
        )
    else:
        result = paired(args.old, args.new)
        with args.output.open("x", encoding="utf8") as out:
            json.dump(result, out, ensure_ascii=False, indent=2)
