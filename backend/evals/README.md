# Harness 与售后本体评测

以下命令在 `backend` 目录内用项目 Python 环境运行。

| 目标 | 运行方式 |
|---|---|
| 检查评分器、委派预算、交付校验与故障规则 | `python -m pytest -q tests/evals tests/assistant/test_harness_limits.py tests/assistant/test_delivery_gate.py tests/query/test_ontology_recurrence.py` |
| 查看 200 题的口径与参考答案 | [评测题库](rubric.md)。用 `python -m evals.catalog catalog.json` 导出含问句的目录。 |
| 用正式聊天接口跑一题 | 配好开发服务和本地登录凭据后，启动 `python -m evals.serve ../logs/eval-demo`。在第二个终端运行 `python -m evals.live ../logs/eval-demo --case ORD-01`。回放记录可用于复核工具调用和最终回答。 |
| 独立复算设备故障复发 | `python -m scripts.development.manufacturing_pilot ../logs/service-pilot.sqlite`。使用新的文件名，脚本会生成样本和核对结果。 |

售后试点的设备规格取自[新威产品目录记录](../conf/ontology/neware_products.json)，客户、工单与故障事件由样本生成器构造。三客户范围定义在 [manufacturing_cohort.py](../scripts/development/manufacturing_cohort.py)，复发规则定义在 [manufacturing_service.yaml](../conf/ontology/manufacturing_service.yaml)。

在新的开发库部署试点，依次运行以下命令。发布者填写实际操作者标识。最后重新加载后端，使用已映射且具有只读权限的试点角色验收。

```text
python -m scripts.development.manufacturing_doris --install
python -m scripts.development.manufacturing_cohort --install
python -m scripts.development.manufacturing_cohort --grant
python -m scripts.development.manufacturing_definition --revision 2 --actor <发布者>
```

在已部署的环境用 `python -m scripts.development.manufacturing_doris --check` 核对 Doris 与本地标准答案。用 `python -m evals.ontology_compare --help` 查看同题对照参数，分别测试生成 SQL、生成 JSON 和调用正式工具时的表现。
