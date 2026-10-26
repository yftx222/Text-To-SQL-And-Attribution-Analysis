# DataAgent

后端位于 `backend/`，前端位于 `frontend/`。

```text
code/
├─ backend/       Python 后端、依赖、配置、测试、资源与部署文件
├─ frontend/      React / TypeScript 前端
├─ dbmock/        独立数据生成子模块，仍由根目录 .gitmodules 管理
├─ .venv/         已有后端环境，继续复用
├─ logs/          原有 JSONL 日志输出位置，运行时创建
├─ services.ps1   Windows 本机服务启动、停止和状态查看
└─ Makefile       开发命令
```

- [后端安装、配置与启动](backend/README.md)
- [后端目录结构与迁移说明](backend/后端目录结构与迁移说明.md)
- [架构与协作总览](backend/docs/00_架构与协作总览.md)
- [接口索引](backend/docs/08_接口索引.md)

## Windows 一次启动全部本机服务

先启动虚拟机数据库，再在本目录的 PowerShell 执行：

```powershell
.\services.ps1          # 启动 BGE、后端、Worker、Beat、前端
.\services.ps1 -Status  # 查看本项目进程状态
.\services.ps1 -Stop    # 停止上述五个本机服务
```

若执行策略阻止运行，可使用 `powershell -ExecutionPolicy Bypass -File .\services.ps1`；停止时在命令末尾加 `-Stop`。脚本复用 `code/.venv` 和已安装的前端依赖，在后台运行并等待服务就绪；重复执行会复用现有服务。启动失败会停止本次新建的服务，保留之前已运行的服务。

页面地址为 `http://127.0.0.1:7001`。日志在 `code/logs`，每个服务分别写入 `<服务名>.log` 和 `<服务名>-error.log`。`-Status` 显示进程是否运行；启动过程另外检查 HTTP 或任务服务的就绪状态。停止按本项目路径识别进程，不按进程名批量停止 Python/Node。运行中的请求和任务会随停止结束。

脚本不会启动本机 Docker，也不会自动改动或启停虚拟机数据库。数据库和沙箱连接虚拟机 `192.168.77.100`，向量服务使用本机已有的 BGE 模型。

## 虚拟机数据库脚本

虚拟机已安装 `/root/dataagent-db.sh`，本地源码保存在 `deploy/dataagent/dataagent-db.sh`：

```bash
/root/dataagent-db.sh start               # 启动本项目需要的五个数据库容器并等待就绪
/root/dataagent-db.sh status              # 查看数据库容器状态
/root/dataagent-db.sh stop                # 只停 Doris FE/BE 和 Elasticsearch
/root/dataagent-db.sh stop --with-shared  # 额外停止共用 PostgreSQL、Redis
```

PostgreSQL 容器为 `ecommerce-cs-postgres`，Redis 为 `Attribution-redis`，与旧项目共用。`--with-shared` 会同时使依赖它们的旧项目断开数据库。脚本只启动/停止已有容器，保留现有内存配置、数据库文件和卷，不执行初始化或重新造数。

建议顺序：虚拟机 `start` → 本机启动；本机 `-Stop` → 虚拟机 `stop`。详细连接配置见 [本地模型与虚拟机 Docker](backend/docs/本地模型与虚拟机Docker.md)。
