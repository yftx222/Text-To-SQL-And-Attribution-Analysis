# Harness 与售后本体评测

### 单题修复验收

新批次首测用 `evals.live --first-pass`，按原题评分，首测目录不放 `counterexample_plan.json`。该标记写入批次清单，报告和独立回放器均跳过反例。在单独的错题修复目录登记反例，只选首测失败题重跑。原批次的记录保留；下文的反例回放命令用于错题修复验收。

正式 18 题批次保存运行前后快照和反例计划后，运行 `python -m evals.live_report <实验目录>` 即可自动复核。评估器从终答、下载的报告和提交的查询证据提取断言，绑定本次执行的 SQL 后在 Doris 的隔离样本上回放。机器提取记录保存在 `automatic_reviews.json`，手工标注仍单独保存在 `reviews.json`。未识别的答案格式或列别名、无法定位的主查询以及缺少的证据均保留待复核。批次报告分别给出数字正确率、SQL 口径和最终验收结果。

先写出错误做法在哪种输入上会算错，并独立算出预期结果。SQL 题在实验目录的 `counterexample_plan.json` 登记样本。例如，`{"CASE-01":[{"fixture":"sessions","expected":[1,2]}]}` 表示分子为 1、分母为 2。也可用 `tables` 提供独立小表，每张表填写列名与样本行。换问法时用 `blind_cards.json` 登记问句与答案，再用 `evals.live --blind` 启动新会话。

复核时在本组 `reviews.json` 记录真实主查询的 `answer_sql` 和与预期同序的 `counterexample_columns`。运行 `python -m evals.counterexamples <实验目录> --group first`，再运行 `python -m evals.live_report <实验目录> --group first`。反例使用同一条 SQL，替换全部物理表，结果绑定到试次、日志和样本。终答中确认的错误可在 `final_error` 填写原文 `quote` 和核算理由 `reason`。

最终验收要求原题和反例均通过。缺少反例、证据不匹配或回放出错，反例栏显示待验证。当前自动反例回放支持单行数值 SQL；澄清、权限和报告交付题需要对应的受控场景，不能凭原题通过就结案。

### Langfuse 调用链与评分

评测服务通过 LangChain 回调把同一回合的模型、工具、父子调用关系及模型返回的 Token 用量写入本地。完成逐题裁决后，运行 `python -m evals.live_report <实验目录> --group first --langfuse`，通过 OTLP/HTTP 将调用树送到自托管 Langfuse v4，并把该题的业务正确性和首次交付评分绑定到最后一个回合。此路径按批导出。服务不可达时，已落盘的结果可用 `python -m evals.langfuse_export <实验目录> --group first` 重新同步。使用相同的 trace、span 与 score ID。

客户端读取本地 `conf/.env.langfuse` 中的 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`。网页展示调用次数、层级、耗时和已知用量。缺少结束事件的历史调用标记为 `missing_end`，不能用于耗时统计。业务正确性取自本地评分器和证据；缺少供应商单价时，不能从 Token 数直接报告成本。导出时仅组装白名单字段，问题、回答、SQL、CSV、工具参数和异常原文留在本地。

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

### 过程指标与离线复核

在 backend 目录运行。正常生成 `live_report` 时会一并生成 `process_metrics.json`。回看已保存的批次可以单独调用评分器，使用本地文件即可。

```text
python -m evals.process_score <实验目录> --group first --out <新结果.json>
python -m evals.process_score <实验目录> --group first --review-template <新模板.json>
```

逐题记录包含角色调用次数、SQL 前可见的元数据、通道命中、客户端超时、各阶段耗时和 Token。融合结果对字段、指标分别按题取平均。正式答案使用哪次 SQL，靠复核中的 `answer_sql` 和工具返回的执行 ID 对齐。未确认的主查询保持待定。关键路径取外层图每个执行步中耗时最长的节点，缺少节点起止的旧记录显示为空。SQL 阶段计时包含流式读取与 CSV 写入。

默认 18 题要求实际取数，ATT-01 还要求取数在正式发布前发生。可在实验前通过实验目录的 `process_rules.json` 按题覆盖，例如 `{"ORD-01":{"behavior":{"required":["execute_sql"],"forbidden":["publish_artifact"]},"recall_required":true}}`。故障、权限、审查用例的预期见 rubric.md。受控执行器可提供同格式的 case 日志和组 manifest 供离线评分。

将填好的模板保存为组目录的 `process_reviews.json`。每个判断填写真实运行的证据文件、SHA-256、行号或报告位置及理由。额外追问按题判，结论支持按交付内容中的每条可验证结论判。权限项核对当前角色可见的终答、工具结果和附件。故障项核对指定故障确实发生及原目标是否完成。发布项按同一版本计一次，`requests` 记录实际提交次数。空值表示待裁决；有证据表明未触发场景时填 `not_exposed`。报告同时列已裁决分母、待裁决数和未触发数。

费用使用实验目录的 `prices.json`。填写 `version`、`currency` 和 `models`；每个实际模型名下填写每百万 Token 的 `uncached_input`、`cache_read` 和 `output` 单价。成本包含失败尝试，除以首次正确交付数。价格缺失、收费桶无法拆分或重试用量无法确认时，保留已知 Token，完整成本显示为空。

### Evaluation-Platform D2

使用现有评测部署 `python -m evals.serve <新日志目录> --port <端口>`。启用回传前，将中台发布制品 `evaluation_platform_trace-0.2.0-py3-none-any.whl` 安装到该服务的虚拟环境，再配置以下环境变量。

- `DATAAGENT_TRACE_PLATFORM_URL` 为 API 根地址。生产使用 HTTPS，本机联调可用回环 HTTP。
- `DATAAGENT_TRACE_USER` 为获准的专用普通账号。该账号和合成数据的只读配置沿用 D1。
- 在中台选择 DataAgent build 2，开启 callback 模式，并把项目版本 ID 加入 Worker 的 `EVAL_TRACE_TARGET_VERSIONS`。

评测服务在每轮对话的后台任务中记录调用结构和计数。结束时由 Adapter 核对历史和文件，再请求评测服务使用 SDK 批量回传并封口。多轮和最后的封口请求须路由到同一评测进程。本地 JSONL 继续由 `evals.serve` 写入。省略回传 URL 时仅使用本地采集。独立运行 `pytest tests/evals/test_platform_trace.py` 可验证账号、会话和回调隔离，也可验证接收服务离线时的行为。
