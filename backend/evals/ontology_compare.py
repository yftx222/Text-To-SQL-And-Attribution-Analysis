"""同一模型、角色、目录和业务说明的单轮生成对比。真实聊天流程另行回放。"""

import argparse
import json
import sqlite3
from contextlib import AsyncExitStack, aclosing, closing
from pathlib import Path
from time import monotonic
from uuid import uuid4

from dataagent.engines.agent.explorer.tools.ontology import ontology_tools
from dataagent.engines.assistant.models.session import AgentSessionKey
from dataagent.engines.common.async_runtime import run_async
from dataagent.engines.common.llm import create_configured_model
from dataagent.engines.contract.config import cfg
from dataagent.engines.identity import IdentityService
from dataagent.engines.infra.doris_client_manager import (
    DorisClientManager,
    DorisQueryClientRegistry,
)
from dataagent.engines.infra.postgres_client_manager import PostgresClientManager
from dataagent.engines.query.contracts import (
    QueryExecutionLimits,
    QueryExecutionOptions,
    SemanticMonthRequest,
    SemanticQueryRequest,
)
from dataagent.engines.query.ontology.service import OntologyService
from dataagent.engines.query.repositories.doris import DorisQueryRepository
from scripts.development.manufacturing_pilot import seed

QUESTIONS = (
    (
        "OCT",
        "列出 2026 年 10 月本角色范围内同类故障 30 天内复发的前后事件对。",
        None,
        None,
    ),
    (
        "CUSTOMER",
        "列出 2026 年 10 月发生时使用客户为 C07 的同类故障 30 天内复发事件对。",
        "C07",
        None,
    ),
    (
        "ASSET",
        "列出设备 BAT-0028 在 2026 年 10 月同类故障 30 天内复发的前后事件对。",
        None,
        "BAT-0028",
    ),
)


def reference():
    with closing(sqlite3.connect(":memory:")) as db:
        labels = seed(db)
        owner = dict(
            db.execute(
                "SELECT device_id,customer_id FROM mfg_device_customer_relation WHERE scope_id='demo-service' AND customer_id IN ('C04','C07','C10')"
            )
        )
        ids = dict(
            db.execute(
                "SELECT event_id,device_id FROM mfg_fault_incident WHERE scope_id='demo-service'"
            )
        )
        return {
            code: {
                tuple(pair)
                for pair in labels["pairs"]
                if ids[pair[1]] in owner
                and (not customer or owner[ids[pair[1]]] == customer)
                and (not device or ids[pair[1]] == device)
            }
            for code, _, customer, device in QUESTIONS
        }


async def compare(dest, user_id, native_only=False):
    gold = reference()
    records = []
    async with AsyncExitStack() as stack:
        auth = PostgresClientManager(cfg.auth_postgresql)
        meta = PostgresClientManager(cfg.meta_postgresql)
        admin = DorisClientManager(cfg.doris)
        clients = DorisQueryClientRegistry(
            cfg.doris.model_copy(update={"database": cfg.query.ontology_database})
        )
        for obj in (auth, meta, admin, clients):
            stack.push_async_callback(obj.close)
        service = OntologyService(
            IdentityService(auth, admin),
            meta,
            clients,
            cfg.query.model_copy(update={"learn_from_queries": False}),
        )
        model = await stack.enter_async_context(
            create_configured_model(cfg.lm_config.active)
        )
        for code, question, _, _ in QUESTIONS:
            key = AgentSessionKey(user_id, uuid4(), "mfg_eval", "explorer", "eval")
            bundle = await service.bundle(key)
            principal, scope, client, _, guard = await service._authorized(key)
            shared = json.dumps(
                {
                    k: bundle[k]
                    for k in (
                        "sources",
                        "relations",
                        "rule_summary",
                        "timezone",
                        "product_scope",
                    )
                },
                ensure_ascii=False,
            )
            info = f"业务范围 {scope}，时钟为 UTC 微秒。独立事件按 event_id 去重。完整历史才匹配；同刻前序不清晰不确认；按生命周期最近一次生效状态判断关闭，状态值 closed；只有 using_customer 关系有效且唯一才能确认客户。{shared}"
            for mode in ("tool",) if native_only else ("sql", "semantic"):
                record = {
                    "case": code,
                    "arm": mode,
                    "expected": sorted(gold[code]),
                    "model": cfg.lm_config.active,
                }
                start = monotonic()
                try:
                    instruction = (
                        "只返回单条 Doris SELECT，结果严格两列 prior_event 和 event_id，列出匹配的事件对。"
                        if mode == "sql"
                        else '只返回 JSON 请求。operation=trace_recurrent_faults，自然月参数 month=YYYY-MM，可选 device_ids 或 customer_ids，outputs=["event_pairs"]。'
                    )
                    messages = [
                        ("system", info + "\n" + instruction),
                        ("user", question),
                    ]
                    if mode == "tool":
                        execute = ontology_tools(service, key, "explorer")[1]
                        msg = await model.bind_tools(
                            [execute]
                        ).ainvoke(
                            [
                                (
                                    "system",
                                    info + "\n请按问题调用查询工具，返回 event_pairs。",
                                ),
                                ("user", question),
                            ]
                        )
                        if (
                            len(msg.tool_calls) != 1
                            or msg.tool_calls[0]["name"] != execute.name
                        ):
                            raise ValueError("未收到单次语义工具调用")
                        args = msg.tool_calls[0]["args"]
                        record["generated"] = args
                        res = await execute.ainvoke(args)
                        if res["status"] != "success":
                            raise ValueError(str(res))
                        pairs = res["result"]["event_pairs"]
                        record["window"] = res["manifest"]["request"]
                    else:
                        msg = await model.ainvoke(messages)
                        raw = msg.text.strip()
                        if raw.startswith("```"):
                            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                        record["generated"] = raw
                    record["tokens"] = (msg.usage_metadata or {}).get("total_tokens")
                    if mode == "sql":
                        valid = await guard.check(raw)
                        allowed = {s["table"] for s in bundle["sources"].values()}
                        if (
                            not valid.valid
                            or not valid.normalized_sql
                            or not valid.tables
                            or any(
                                t.name not in allowed
                                or t.database not in (None, cfg.query.ontology_database)
                                for t in valid.tables
                            )
                        ):
                            raise ValueError("查询目录或 Guard 校验未通过")
                        repo = DorisQueryRepository(client)
                        limits = QueryExecutionLimits(
                            workload_group=principal.workload_group,
                            timeout_seconds=cfg.query.timeout_seconds,
                            memory_limit_bytes=cfg.query.memory_limit_bytes,
                        )
                        pairs = []
                        async with aclosing(
                            repo.stream(
                                valid.normalized_sql,
                                limits,
                                QueryExecutionOptions(batch_size=100, sample_rows=0),
                            )
                        ) as batches:
                            async for b in batches:
                                if (
                                    b.column_names != ("prior_event", "event_id")
                                    or len(pairs) + len(b.rows) > 2000
                                ):
                                    raise ValueError("返回结构超出评测协议")
                                pairs.extend(b.rows)
                    elif mode == "semantic":
                        data = json.loads(raw)
                        request = (
                            SemanticMonthRequest(**data).resolve()
                            if "month" in data
                            else SemanticQueryRequest(**data)
                        )
                        res = await service.execute(key, request)
                        pairs = res["result"]["event_pairs"]
                        record["window"] = res["manifest"]["request"]
                    record["actual"] = sorted(pairs)
                    record["passed"] = sorted(tuple(x) for x in pairs) == sorted(
                        gold[code]
                    )
                except Exception as exc:  # noqa: BLE001
                    # 单题失败也保留分母
                    record.update(
                        passed=False, error=type(exc).__name__, detail=str(exc)[:450]
                    )
                record["seconds"] = round(monotonic() - start, 2)
                records.append(record)
                dest.write_text(
                    json.dumps(records, ensure_ascii=False, indent=2), "utf8"
                )
                print(code, mode, record["passed"], flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=Path)
    p.add_argument("--user-id", required=True, type=int)
    p.add_argument(
        "--native-only",
        action="store_true",
        help="单独测正式工具协议，结果写入独立文件",
    )
    args = p.parse_args()
    run_async(compare(args.output, args.user_id, args.native_only))
