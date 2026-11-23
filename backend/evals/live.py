"""Durable replay through the regular chat API. Start with python -m evals.serve."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from filelock import FileLock

from .offline_score import HERE, load_cards, read

API = "/api/v1"
EVIDENCE_EVENTS = {"message", "subagent_message", "subagent_status", "error", "done"}


def append(path, kind, **fields):
    row = {"kind": kind, "at": datetime.now(UTC).isoformat(), **fields}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def journal(path):
    if not path.exists():
        return []
    lines = path.read_bytes().splitlines(keepends=True)
    # A killed writer can leave an incomplete final record. Restore the last committed offset.
    if lines and not lines[-1].endswith(b"\n"):
        with path.open("rb+") as f:
            f.truncate(sum(map(len, lines[:-1])))
        lines.pop()
    return [json.loads(line) for line in lines]


def public_message(message):
    return {
        **message,
        "parts": [p for p in message.get("parts", []) if p["type"] != "thinking"],
    }


def needs_reconciliation(records):
    if not records or records[-1]["kind"] != "closed":
        return bool(records)
    if records[-1]["status"] == "unknown_creation":
        return False
    if not any(r["kind"] == "conversation" for r in records):
        return False
    last_run = next(
        (r for r in reversed(records) if r["kind"] in ("run", "run_error")), {}
    )
    last_history = next(
        (r for r in reversed(records) if r["kind"] in ("history", "history_error")), {}
    )
    return (
        last_run.get("data", {}).get("running") is not False
        or last_history.get("kind") != "history"
    )


async def reconcile(client, path, cid):
    for suffix, url in (("run", f"/chat/{cid}/run"), ("history", f"/chat/ls/{cid}")):
        try:
            response = await client.get(API + url)
            response.raise_for_status()
            data = response.json()
            if suffix == "history":
                data = [public_message(m) for m in data["messages"]]
            append(path, suffix, data=data)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            append(path, suffix + "_error", error=type(exc).__name__)


async def read_turn_events(stream, path, index):
    async with stream as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:])
            if event["type"] not in EVIDENCE_EVENTS:
                continue
            if "message" in event:
                event["message"] = public_message(event["message"])
            append(path, "event", turn=index, data=event)
            if event["type"] == "error":
                if (
                    event.get("content")
                    == "事件流消费速度过慢，请重新连接以恢复最新状态。"
                ):
                    return "reconnect"
                return "stream_error"
            if event["type"] == "done":
                return "done"
    return "incomplete"


async def replay_case(client, path, card, seconds, *, ensure_auth=None):
    rows = journal(path)
    if rows and rows[-1]["kind"] == "closed":
        if needs_reconciliation(rows):
            cid = next(r["cid"] for r in rows if r["kind"] == "conversation")
            await reconcile(client, path, cid)
            await downloads(client, path, cid)
            append(path, "closed", status=rows[-1]["status"])
        return
    cid = next((r["cid"] for r in rows if r["kind"] == "conversation"), None)
    if not rows:
        append(path, "intent", case=card["id"], trial=str(uuid4()))
        response = await client.post(
            API + "/chat/create",
            json={"is_draft": True, "initial_message": "评测 " + card["id"]},
        )
        response.raise_for_status()
        cid = response.json()["conversation_id"]
        append(path, "conversation", cid=cid)
    if not cid:
        append(path, "closed", status="unknown_creation")
        return
    started = [r for r in rows if r["kind"] == "turn_start"]
    completed = [r for r in rows if r["kind"] == "turn_end"]
    status = "done"
    if len(started) != len(completed):
        status = "unknown_submission"
    elif completed:
        status = completed[-1]["status"]
    for index in range(len(completed), len(card["turns"])):
        if status != "done":
            break
        if ensure_auth:
            await ensure_auth()
        turn = card["turns"][index]
        trace = str(uuid4())
        append(path, "turn_start", turn=index, trace=trace, question=turn["message"])
        start = monotonic()
        status = "incomplete"
        try:
            async with asyncio.timeout(seconds):
                stream = client.stream(
                    "POST",
                    API + "/chat/stream",
                    headers={"X-Request-ID": trace, "X-Trace-ID": trace},
                    json={
                        "conversation_id": cid,
                        "message": {
                            "parts": [{"type": "text", "text": turn["message"]}],
                            "attachments": [],
                        },
                    },
                )
                status = await read_turn_events(stream, path, index)
                while status == "reconnect":
                    await asyncio.sleep(1)
                    if ensure_auth:
                        await ensure_auth()
                    stream = client.stream("GET", API + f"/chat/{cid}/events")
                    status = await read_turn_events(stream, path, index)
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError) as exc:
            status = type(exc).__name__
        append(
            path,
            "turn_end",
            turn=index,
            status=status,
            seconds=round(monotonic() - start, 3),
        )
    if ensure_auth:
        await ensure_auth()
    await reconcile(client, path, cid)
    await downloads(client, path, cid)
    append(path, "closed", status=status)


async def downloads(client, path, cid):
    latest = next(
        (r["data"] for r in reversed(journal(path)) if r["kind"] == "history"), []
    )
    receipts = {}
    attachments = set()
    turn = 0
    for message in latest:
        if message.get("role") == "user":
            turn += 1
        attachments.update(
            (turn, a["f_path"]) for a in message.get("attachments") or []
            if a.get("delivery_status") == "verified"
        )
        for p in message["parts"]:
            if p["type"] == "tool_result" and p.get("name") == "publish_artifact":
                value = json.loads(p["content"])
                if value.get("status") == "verified":
                    receipts[value["receipt_id"]] = (turn, value)
    for rid, (turn, receipt) in receipts.items():
        remote = receipt["f_path"]
        try:
            resp = await client.get(
                API + "/chat/attachment/get",
                params={"conversation_id": cid, "f_path": remote},
            )
            resp.raise_for_status()
            observed = hashlib.sha256(resp.content).hexdigest()
            # The frozen artifact basename binds the receipt to the version's digest.
            expected = Path(remote).stem.rsplit("_", 1)[-1]
            matched = expected == observed
            filename = observed + ".artifact"
            (path.parent / filename).write_bytes(resp.content)
            append(
                path,
                "download",
                receipt=rid,
                file=filename,
                expected=expected,
                sha256=observed,
                turn=turn,
                attached=(turn, remote) in attachments,
                verified=matched,
            )
        except httpx.HTTPError as exc:
            append(
                path,
                "download",
                receipt=rid,
                verified=False,
                attached=remote in attachments,
                http_status=exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None,
                error=type(exc).__name__,
            )


async def batch(
    folder,
    url,
    cases,
    credentials,
    seconds,
    group,
    jobs=1,
    catalog_mode=False,
    blind=False,
    first_pass=False,
):
    cards = load_cards()
    if blind:
        cards = read(folder / "blind_cards.json")
        cases = cases or list(cards)
    if catalog_mode:
        from .catalog import catalog

        cards = {
            key: {"id": key, "turns": [{"message": m} for m in row["messages"]]}
            for key, row in catalog().items()
            if row["messages"]
        }
    if any(case not in cards for case in cases) or len(set(cases)) != len(cases):
        raise ValueError("未知或重复题号")
    if seconds <= 0 or jobs < 1:
        raise ValueError("并发数和回合时限必须为正")
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / group
    output.mkdir(exist_ok=True)
    manifest = output / "manifest.json"
    plan = {
        "cases": cases,
        "group": group,
        "url": url,
        "seconds": seconds,
        "principal": credentials["identifier"],
        "cards": hashlib.sha256((HERE / "scorecards.json").read_bytes()).hexdigest(),
        "questions": hashlib.sha256(
            (HERE / "pilot_cases.json").read_bytes()
        ).hexdigest(),
    }
    if first_pass:
        plan["stage"] = "first_pass"
    if jobs > 1:
        plan["parallel_trials"] = jobs
    if blind:
        plan["blind_cards"] = hashlib.sha256(
            (folder / "blind_cards.json").read_bytes()
        ).hexdigest()
        plan["cards"] = plan["questions"] = plan["blind_cards"]
    if catalog_mode:
        plan["rubric_cases"] = True
        plan["question_texts"] = {
            k: [t["message"] for t in cards[k]["turns"]] for k in cases
        }
    if (folder / "fixed_plan.json").exists():
        from .fixed_regression import scoring_hashes

        frozen = read(folder / "fixed_plan.json")
        hashes = scoring_hashes()
        if (not catalog_mode or not first_pass or group != "first"
                or cases != list(frozen["cards"])
                or (seconds, jobs) != (frozen["seconds"], frozen["jobs"])
                or hashes != frozen["scoring_sha256"]):
            raise ValueError("执行参数或代码与冻结计划不符")
        plan["fixed_scoring"] = hashes
    with FileLock(str(output / "batch.lock"), timeout=0):
        if manifest.exists() and read(manifest) != plan:
            raise ValueError("此批已有不同的运行计划")
        if not manifest.exists():
            manifest.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        async with httpx.AsyncClient(
            base_url=url, timeout=httpx.Timeout(90, connect=8), trust_env=False
        ) as client:
            ready = await client.get("/_eval/status")
            ready.raise_for_status()
            if ready.json() != {
                "experience_writes": False,
                "folder": str(folder.resolve()),
            }:
                raise ValueError("当前服务的经验写入或采集目录与实验不符")
            token, expires = None, 0
            lock = asyncio.Lock()
            capacity = asyncio.Semaphore(jobs)

            async def ensure_auth():
                nonlocal token, expires
                async with lock:
                    if monotonic() < expires:
                        return
                    auth = await client.post(
                        API + ("/auth/refresh" if token else "/auth/login"),
                        json={"refresh_token": token} if token else credentials,
                    )
                    auth.raise_for_status()
                    data = auth.json()
                    client.headers["Authorization"] = "Bearer " + data["access_token"]
                    token, expires = (
                        data["refresh_token"],
                        monotonic() + data["expires_in"] - 30,
                    )

            async def run_one(case):
                path = output / (case + ".jsonl")
                if (
                    path.exists()
                    and (prior := journal(path))
                    and prior[-1]["kind"] == "closed"
                    and not needs_reconciliation(prior)
                ):
                    return
                async with capacity:
                    await ensure_auth()
                    try:
                        await replay_case(
                            client, path, cards[case], seconds, ensure_auth=ensure_auth
                        )
                    except (httpx.HTTPError, ValueError, KeyError) as exc:
                        append(path, "closed", status=type(exc).__name__)
                    print(case, journal(path)[-1]["status"], flush=True)

            await asyncio.gather(*(run_one(case) for case in cases))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", type=Path)
    p.add_argument("--group", default="first")
    p.add_argument("--url", default="http://127.0.0.1:7014")
    p.add_argument("--env-file", type=Path, default=HERE.parent / "conf/.env")
    p.add_argument("--case", action="append")
    p.add_argument("--seconds", type=int, default=1950)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--first-pass", action="store_true", help="按原题验收，关闭反例回放")
    p.add_argument(
        "--catalog",
        action="store_true",
        help="从170道快照题中按 --case 选题，扩展题按业务口径复核",
    )
    p.add_argument(
        "--blind",
        action="store_true",
        help="使用本实验预登记的 blind_cards.json，聊天只发送 message",
    )
    args = p.parse_args()
    if args.blind and args.catalog:
        p.error("--blind 与 --catalog 只能选一个")
    env = dotenv_values(args.env_file)
    creds = {"identifier": env["ADMIN_USERNAME"], "password": env["ADMIN_PASSWORD"]}
    if args.catalog and not args.case:
        p.error("--catalog 需要至少一个 --case")
    asyncio.run(
        batch(
            args.folder.resolve(),
            args.url,
            args.case or ([] if args.blind else list(load_cards())),
            creds,
            args.seconds,
            args.group,
            args.jobs,
            args.catalog,
            args.blind,
            args.first_pass,
        )
    )
