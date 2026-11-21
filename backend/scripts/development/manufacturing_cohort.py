"""从已验收的合成库制作客户范围固定的只读试点数据集。"""

import argparse

from dataagent.engines.common.async_runtime import run_async
from dataagent.engines.contract.config import cfg
from dataagent.engines.infra.doris_client_manager import DorisClientManager
from scripts.development.manufacturing_doris import DATABASE

COHORT = "dataagent_mfg_service_demo"
ROLE = "dataagent_test"
CUSTOMERS = ("C04", "C07", "C10")
SCOPE = "demo-service"
# 选定的客户在本批数据中只有持续独占使用的设备。遇到跨客户交接则停止，
# 不能把设备在另一家客户期间的工单随整机复制。
OWNED = f"""SELECT DISTINCT device_id FROM {DATABASE}.mfg_device_customer_relation
WHERE scope_id='demo-service' AND `role`='using_customer' AND customer_id IN ('C04','C07','C10')"""


async def run(grant=False):
    d = DorisClientManager(cfg.doris)
    try:
        async with d.engine.begin() as c:
            if grant:
                # 核对完整范围后才允许授予指定表的 SELECT，未来新增表不自动授权。
                await inspect(c)
                for t in (
                    "asset_detail",
                    "product",
                    "mfg_device",
                    "mfg_customer",
                    "mfg_fault_category",
                    "mfg_fault_incident",
                    "mfg_work_order",
                    "mfg_fault_lifecycle",
                    "mfg_device_customer_relation",
                ):
                    await c.exec_driver_sql(
                        f"GRANT SELECT_PRIV ON `{COHORT}`.`{t}` TO ROLE `{ROLE}`"
                    )
                return
            disputed = (
                await c.exec_driver_sql(f"""SELECT COUNT(*) FROM (SELECT device_id FROM {DATABASE}.mfg_device_customer_relation
WHERE scope_id='demo-service' AND `role`='using_customer' AND device_id IN ({OWNED})
GROUP BY device_id HAVING COUNT(*)<>1 OR COUNT(valid_to_us)>0) x""")
            ).scalar_one()
            if disputed:
                raise ValueError(
                    "选定客户存在交接、冲突或过期关系，请按事件时点单独投影"
                )
            # 事件都落在唯一有效区间才允许复制该设备的事实。
            outside = (
                await c.exec_driver_sql(f"""SELECT COUNT(*) FROM {DATABASE}.mfg_fault_incident i JOIN {DATABASE}.mfg_device_customer_relation r
ON i.scope_id=r.scope_id AND i.device_id=r.device_id WHERE r.scope_id='demo-service' AND r.`role`='using_customer'
AND r.customer_id IN ('C04','C07','C10') AND i.occurred_us<r.valid_from_us""")
            ).scalar_one()
            if outside:
                raise ValueError("有事件早于使用关系起点")
            await c.exec_driver_sql(f"CREATE DATABASE {COHORT}")
            events = f"SELECT event_id FROM {DATABASE}.mfg_fault_incident WHERE scope_id='demo-service' AND device_id IN ({OWNED})"
            orders = f"SELECT work_order_id FROM {DATABASE}.mfg_work_order WHERE scope_id='demo-service' AND event_id IN ({events})"
            filters = {
                "mfg_customer": "customer_id IN ('C04','C07','C10')",
                "customer_detail": "customer_id IN ('C04','C07','C10')",
                "mfg_device": f"device_id IN ({OWNED})",
                "asset_detail": f"device_id IN ({OWNED})",
                "mfg_fault_category": "1=1",
                "mfg_fault_incident": f"device_id IN ({OWNED})",
                "mfg_work_order": f"event_id IN ({events})",
                "mfg_fault_lifecycle": f"event_id IN ({events})",
                "mfg_device_customer_relation": "`role`='using_customer' AND customer_id IN ('C04','C07','C10')",
                "service_ticket": f"work_order_id IN ({orders})",
                "product": "1=1",
            }
            for table, predicate in filters.items():
                await c.exec_driver_sql(
                    f"CREATE TABLE {COHORT}.{table} LIKE {DATABASE}.{table}"
                )
                where = (
                    "" if table == "product" else "scope_id='demo-service' AND "
                ) + predicate
                await c.exec_driver_sql(
                    f"INSERT INTO {COHORT}.{table} SELECT * FROM {DATABASE}.{table} WHERE {where}"
                )
            await inspect(c)
    finally:
        await d.close()


async def inspect(c):
    rows = (
        await c.exec_driver_sql(
            f"SELECT scope_id,customer_id FROM {COHORT}.mfg_customer"
        )
    ).all()
    if set(rows) != {(SCOPE, k) for k in CUSTOMERS}:
        raise ValueError("客户范围校验失败")
    print(COHORT, sorted(rows))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--grant", action="store_true")
    run_async(run(p.parse_args().grant))
