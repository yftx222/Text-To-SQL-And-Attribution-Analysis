"""Read the experiment inputs before and after a live batch."""

import asyncio
import hashlib
import json
import subprocess
from datetime import UTC, datetime

import httpx

from dataagent.engines.contract.config import cfg
from dataagent.engines.infra.doris_client_manager import DorisClientManager
from dataagent.engines.infra.postgres_client_manager import PostgresClientManager

from .offline_score import HERE


def sha(data):
    return hashlib.sha256(data).hexdigest()


def deployment_inputs():
    backend = HERE.parent
    files = sorted(
        p
        for p in (backend / "dataagent").rglob("*")
        if p.suffix in {".py", ".md", ".yaml"}
    )
    return {
        "git": (
            subprocess.check_output(
                ["git", "-C", str(backend), "rev-parse", "HEAD"],
                text=True,
            )
        ).strip(),
        "source": sha(
            "".join(
                p.relative_to(backend).as_posix() + sha(p.read_bytes()) for p in files
            ).encode()
        ),
        "config": sha((backend / "conf/app_config.yaml").read_bytes()),
    }


async def snapshot(*, fixture_config=None, catalog_only=False):
    doris = DorisClientManager(fixture_config or cfg.doris)
    pg = PostgresClientManager(cfg.meta_postgresql)
    tables, metadata = {}, {}
    try:
        async with doris.engine.connect() as con:
            names = (
                await con.exec_driver_sql(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE() AND table_type='BASE TABLE'"
                )
            ).scalars()
            for name in sorted(names):
                rows = await con.exec_driver_sql(
                    "SELECT * FROM `" + name.replace("`", "``") + "`"
                )
                hashes = sorted(
                    sha(json.dumps(list(row), default=str, ensure_ascii=False).encode())
                    for row in rows
                )
                tables[name] = [len(hashes), sha("".join(hashes).encode())]
                if catalog_only:
                    columns = (
                        await con.exec_driver_sql(
                            "SELECT column_name,ordinal_position,column_type,is_nullable "
                            "FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=%s "
                            "ORDER BY ordinal_position",
                            (name,),
                        )
                    ).all()
                    if not columns:
                        raise ValueError("missing fixture table schema")
                    tables[name].append(
                        sha(
                            json.dumps(
                                [tuple(c) for c in columns], default=str
                            ).encode()
                        )
                    )
        async with pg.engine.connect() as con:
            names = (
                await con.exec_driver_sql(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                )
            ).scalars()
            for name in sorted(names):
                if catalog_only and name not in {
                    "table_info",
                    "column_info",
                    "metric_info",
                    "column_metric",
                    "query_experiences",
                    "query_experience_assets",
                    "ontology_definitions",
                }:
                    continue
                if name.startswith(("alembic", "query_execution")):
                    continue
                quoted = '"' + name.replace('"', '""') + '"'
                sql = f"SELECT count(*), md5(string_agg(h,'' ORDER BY h)) FROM (SELECT md5(row_to_json(t)::text) h FROM {quoted} t) s"
                metadata[name] = list((await con.exec_driver_sql(sql)).one())
    finally:
        await doris.close()
        await pg.close()
    es = cfg.elasticsearch
    indices = [
        es.column_index,
        es.metric_index,
        es.value_index,
        es.query_experience_index,
    ]
    async with httpx.AsyncClient(trust_env=False, timeout=20) as client:

        async def stats(selector):
            resp = await client.get(
                f"http://{es.host}:{es.port}/{selector}/_stats/docs",
                params={
                    "level": "shards",
                    "ignore_unavailable": "false",
                    "allow_no_indices": "false",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if catalog_only and (not data["indices"] or data["_shards"]["failed"]):
                raise ValueError("missing index state")
            return {
                name: {
                    "uuid": value["uuid"],
                    "docs": value["primaries"]["docs"]["count"],
                    "seq": {
                        s: next(
                            copy["seq_no"]["max_seq_no"]
                            for copy in copies
                            if copy["routing"]["primary"]
                        )
                        for s, copies in value["shards"].items()
                    },
                }
                for name, value in data["indices"].items()
            }

        if catalog_only:
            observed = await asyncio.gather(*(stats(name) for name in indices))
            es_state = {
                (name if selector == name else f"{selector}/{name}"): value
                for selector, group in zip(indices, observed, strict=True)
                for name, value in group.items()
            }
        else:
            es_state = await stats(",".join(indices))
    return {
        "time": datetime.now(UTC).isoformat(),
        "tables": tables,
        "metadata": metadata,
        "es": es_state,
        **await asyncio.to_thread(deployment_inputs),
        "models": {k: v.model for k, v in cfg.lm_config.models.items()},
        "limits": cfg.agent.orchestration.model_dump(),
    }


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    from dataagent.engines.common.async_runtime import run_async

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dest", type=Path)
    parser.add_argument("--against", type=Path)
    args = parser.parse_args()
    dest = args.dest
    state = run_async(snapshot())
    dest.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.against:
        baseline = json.loads(args.against.read_text(encoding="utf-8"))
        changed = [
            key
            for key in (
                "tables",
                "metadata",
                "es",
                "source",
                "config",
                "models",
                "limits",
            )
            if baseline[key] != state[key]
        ]
        if changed:
            raise SystemExit("实验输入发生变化：" + ", ".join(changed))
