"""在全新的开发库安装本站合成样本；复跑 --check 只读比较真实 Doris 与独立标准答案。"""

import argparse
import asyncio
import sqlite3
from contextlib import closing

from sqlalchemy import text

from dataagent.engines.contract.config import cfg
from dataagent.engines.infra.doris_client_manager import DorisClientManager
from dataagent.engines.metadata.contracts import (
    QueryCatalogColumn,
    QueryCatalogSnapshot,
)
from dataagent.engines.query.execution.guard import QueryGuardService
from dataagent.engines.query.ontology.recurrence import checked_query, summarize
from scripts.development.manufacturing_pilot import OCTOBER, SCOPE, check, seed

DATABASE = "dataagent_mfg_pilot_v1"


async def verify(conn, db, gold):
    schema = (
        await conn.execute(
            text(
                "SELECT TABLE_NAME,COLUMN_NAME,DATA_TYPE FROM information_schema.columns WHERE TABLE_SCHEMA=:db"
            ),
            {"db": DATABASE},
        )
    ).all()

    async def catalog():
        return QueryCatalogSnapshot(
            tuple(sorted({r[0] for r in schema})),
            tuple(QueryCatalogColumn(*r) for r in schema),
        )

    sql = await checked_query(
        OCTOBER, SCOPE, QueryGuardService(catalog, current_database=DATABASE)
    )
    data = (await conn.exec_driver_sql(sql)).mappings()
    actual = summarize(OCTOBER, data)
    expected = check(db, gold)
    if actual != expected:
        raise ValueError("Doris 与 SQLite 逐行证据或汇总不一致")
    return actual["summary"]


async def run(install):
    warehouse = DorisClientManager(cfg.doris)
    try:
        with closing(sqlite3.connect(":memory:")) as db:
            gold = seed(db)
            async with warehouse.engine.begin() as conn:
                if install:
                    # CREATE 无 IF NOT EXISTS，绝不改写已有数据库。
                    await conn.exec_driver_sql("CREATE DATABASE " + DATABASE)
                await conn.exec_driver_sql("USE " + DATABASE)
                if install:
                    tables = db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    ).fetchall()
                    for (table,) in tables:
                        fields = db.execute(f"PRAGMA table_info({table})").fetchall()
                        keys = [
                            f[1] for f in sorted(fields, key=lambda f: f[5]) if f[5]
                        ]
                        names = keys + [f[1] for f in fields if not f[5]]
                        types = {
                            f[1]: {
                                "INTEGER": "BIGINT",
                                "TEXT": "VARCHAR(512)",
                                "REAL": "DOUBLE",
                            }[f[2].upper()]
                            for f in fields
                        }
                        columns = ",".join(
                            f"`{n}` {types[n]}"
                            + (" NOT NULL" if n in keys else " NULL")
                            for n in names
                        )
                        key_sql = ",".join(f"`{n}`" for n in keys)
                        await conn.exec_driver_sql(
                            f'CREATE TABLE `{table}` ({columns}) UNIQUE KEY({key_sql}) DISTRIBUTED BY HASH({key_sql}) BUCKETS 1 PROPERTIES ("replication_num"="1")'
                        )
                        projection = ",".join(f"`{n}`" for n in names)
                        params = ",".join(":" + n for n in names)
                        values = [
                            dict(zip(names, r, strict=True))
                            for r in db.execute(f"SELECT {projection} FROM {table}")
                        ]
                        await conn.execute(
                            text(
                                f"INSERT INTO `{table}` ({projection}) VALUES ({params})"
                            ),
                            values,
                        )
                print(await verify(conn, db, gold))
    finally:
        await warehouse.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--check", action="store_true")
    asyncio.run(run(p.parse_args().install))
