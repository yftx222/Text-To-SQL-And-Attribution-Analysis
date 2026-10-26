# 本机应用、本地 BGE、虚拟机 Docker

当前部署分工：

| 位置 | 服务 |
|---|---|
| 本机 | 前端 7001、后端 7000、本地 BGE 7002、Celery Worker/Beat |
| 虚拟机 192.168.77.100 | PostgreSQL、Redis、Elasticsearch、Doris、分析沙箱 Docker |

日常启停可在 `code` 目录运行 `.\services.ps1`、`.\services.ps1 -Stop` 和 `.\services.ps1 -Status`。虚拟机数据库使用 `/root/dataagent-db.sh start|stop|status`；普通 `stop` 保留共用 PostgreSQL、Redis，只有 `stop --with-shared` 才同时停止它们。完整用法见 [启动说明](../../README.md)。

## 本地 BGE

`conf/app_config.yaml` 的 `embedding.local_model_path` 指向已有的 BGE-M3 模型目录。向量服务使用目录内的 `onnx/model.onnx` 和关联权重文件，不下载模型，也不调用 SiliconFlow。

先在后端目录安装可选依赖并启动：

```powershell
$env:UV_PROJECT_ENVIRONMENT = Join-Path (Split-Path (Get-Location).Path) ".venv"
uv sync --group local-embedding
uv run --group local-embedding python -m scripts.serve_embedding
```

服务仅监听 `127.0.0.1:7002`，模型只加载一份，供后端和 Worker 通过 `/v1/embeddings` 共享。输出为 1024 维 CLS + L2 归一化向量。本次基础测试默认每批 8 条、CPU 线程 2、每条最多 512 token；需要处理更长的文本可通过 `--max-length` 调整，最大不应超过模型支持范围。

## 虚拟机 Docker

`sandbox.docker_host` 显式设置为 `ssh://dataagent-vm`，代码不会通过环境变量或 Docker Desktop 默认上下文选择 Docker 引擎。

本机 SSH 配置中的 `dataagent-vm` 别名指向 `root@192.168.77.100`，使用本机已授权的 `ecommerce_sync_ed25519` 密钥。验证：

```powershell
ssh dataagent-vm "docker version"
```

后端使用 Docker SDK 内置的 Paramiko SSH 通道读取上述主机别名和密钥配置，避免 Windows 下 SSH 子进程通道在读取命令输出时阻塞。
远程命令输出结束后直接释放 SSH 通道，并限制连接池数量，避免 SDK 默认的流取消行为报错或耗尽虚拟机 SSH 会话。

虚拟机仅需以下运行资源：

```text
/opt/dataagent/sandbox/          Dockerfile、沙箱依赖文件
/opt/dataagent/skills/           分析与可视化技能
```

`sandbox.skill_host_path` 是技能在虚拟机上的目录。后端仍从本地 `resources/skills` 读取文件；容器挂载使用虚拟机目录，不能将 Windows 路径传给远程 Docker。

在虚拟机预先构建 `dataagent-sandbox:latest`。本次基础测试配置最多一个运行中的用户沙箱、1GB 内存、1 核 CPU，专家任务串行执行。

## 启动其余服务

在后端目录分别执行，终端均复用上面的虚拟环境位置：

```powershell
uv run --group local-embedding python -m dataagent.main
uv run --group local-embedding celery --app dataagent.engines.common.tasks.celery_app:celery_app worker --pool=solo --concurrency=1 -l INFO
uv run --group local-embedding celery --app dataagent.engines.common.tasks.celery_app:celery_app beat -l INFO --schedule ../celerybeat-schedule
```

前端在 `code/frontend` 执行 `npm run dev`。登录凭据读取 `conf/.env` 的 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD`；管理员由项目自带的初始化脚本创建。语言模型仍使用 `.env` 中的 DeepSeek 密钥。基础数据库测试已关闭没有配置密钥的 Tavily 搜索。

## 当前测试数据与登录

浏览器访问 `http://127.0.0.1:7001`。本次已经创建管理员，并绑定 `dataagent_test` 角色；该角色使用 `dataagent_reader` 查询身份，只能读取 Doris 的 `ecommerce` 业务库，工作负载组为 `normal`。

`conf/meta_config.yaml` 已通过合并模式导入：45 张表、958 个字段、114 个指标。字段和指标的首次语义索引均已成功完成。之后变更元数据时，Worker 会调用本机 BGE 更新索引，期间应保持 BGE 与 Worker 运行。

业务数据由 dbmock 按 2026 年 9 月、5% 基础测试规模生成。可测试：“查询订单明细表 dwd_trade_order_detail_di 的记录总数”。本次只读 SQL 实测返回 301 条订单明细。无需再次初始化或重新生成业务数据。

本次验收已通过登录、DeepSeek 问数、Doris 只读查询、虚拟机结果文件写入和最终回答；另连续验证 12 次远程命令输出与通道释放，相关自动化测试共 92 项通过。

本机进程日志位于 `code/logs`：`backend-server.log`、`embedding-server.log`、`celery-worker.log`、`celery-beat.log`。模型加载和首次索引会消耗本机内存；数据库与沙箱消耗虚拟机内存。
