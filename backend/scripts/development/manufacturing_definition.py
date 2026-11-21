"""校验独立 Doris 试点后在元数据库发布新 revision；撤销保留历史记录。"""

import argparse

from dataagent.engines.common.async_runtime import run_async
from dataagent.engines.contract.config import cfg
from dataagent.engines.infra.postgres_client_manager import PostgresClientManager
from dataagent.engines.query.ontology.registry import (
    AnalysisOntology,
    OntologyDefinition,
    OntologyExecution,
    OntologyExperience,
    load_definition,
    publish,
    revoke,
)
from scripts.development.manufacturing_doris import run as verify_warehouse


async def apply(revision, actor, reason):
    if reason is None:
        await verify_warehouse(False)
    pg = PostgresClientManager(cfg.meta_postgresql)
    try:
        async with pg.engine.begin() as conn:
            for model in (
                OntologyDefinition,
                AnalysisOntology,
                OntologyExecution,
                OntologyExperience,
            ):
                await conn.run_sync(
                    lambda sync, table=model.__table__: table.create(
                        sync, checkfirst=True
                    )
                )
        async with pg.session.begin() as session:
            if reason is not None:
                await revoke(session, revision, reason)
            else:
                result = await publish(
                    session, {**load_definition(), "revision": revision}, actor
                )
                print(result.key, result.revision, result.content_hash)
    finally:
        await pg.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--revision", required=True, type=int)
    p.add_argument("--actor", required=True)
    p.add_argument("--revoke-reason")
    args = p.parse_args()
    run_async(apply(args.revision, args.actor, args.revoke_reason))
