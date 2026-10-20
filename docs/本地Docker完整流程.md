# 本地 Docker 与真实模型

当前开发拓扑复用 `rag-nextjs` 的基础设施方式：Docker PostgreSQL 17、回环端口、持久卷。PG 容器为 `rag-system-postgres-1`，容器内端口5432。教案助手使用同一实例、相同连接用户与认证配置，使用独立数据库 `lesson_assistant`。

2026-10-20本机Windows将5405–5504列为保留端口，5432无法绑定（10013），Docker原5432映射不能生效。当前通过本项目独立访问桥 `lesson-assistant-pg-access-91cd4908` 发布 `127.0.0.1:15432`，固定转发到原PG容器5432；已用PG集群标识核对同一实例。桥使用已有Node镜像，非root、只读、无数据卷、只发布回环端口，随Docker自动恢复。模型、JWT及其他配置保持原样，仅 `.env` 中数据库端口改为15432。DBX连接相应使用主机 `127.0.0.1`、端口 `15432`、数据库 `lesson_assistant`，用户名和密码沿用原连接。

`rag-nextjs` 当前没有 Redis。本项目的 `compose.local.yaml` 单独启动 `lesson-assistant-local-redis-1`，监听 `127.0.0.1:56379`，开启 AOF，使用独立命名卷。首次验证使用本机已有的 `redis:alpine3.22` 镜像，实际版本 8.4.0。

## 首次配置

确认已有 PG 容器正在运行。在该实例创建一次专用空数据库（已经创建过则跳过）：

```powershell
docker exec rag-system-postgres-1 sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE lesson_assistant"'
docker compose -f compose.local.yaml up -d redis
```

忽略的 `.env` 中设置：

```dotenv
ENVIRONMENT=development
# 用户名和密码沿用 rag-nextjs 的本地连接；保留必要的 URL 编码。
DATABASE_URL=postgresql+asyncpg://<用户名>:<密码>@127.0.0.1:15432/lesson_assistant
REDIS_URL=redis://127.0.0.1:56379/0
TASK_QUEUE_MODE=arq
AI_PROVIDER=lesson_plan_ai
# 保留自己已填写的 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL。
```

不要把凭证复制进源码或文档。修改 `.env` 后重启 API 和 Worker。应用本身不会自动建表，首次启动前执行：

```powershell
uv run alembic upgrade head
```

如果需要保留原 SQLite 数据，先停止旧 API，并等待所有 queued/running 任务完成。目标 PG 必须没有业务数据，运行：

```powershell
uv run python scripts/migrate_local_sqlite.py
```

迁移入口仅允许本机 `lesson_assistant` 库，拒绝带 query 的连接地址。它用 SQLite backup API 创建包含已提交 WAL 页的备份，按外键顺序复制 20 张业务表，在同一事务内逐行核对；遇到非空目标、未完成任务或不一致即拒绝/回滚。原 SQLite 和 `var/storage` 保留。当前本机迁移已经完成，日常启动不要重复执行。

## 日常启动

```powershell
docker compose -f compose.local.yaml up -d redis
uv run python scripts/run_local.py
```

启动器读取现有 `.env`，启动 API 和七个独立 ARQ Worker，不覆盖模型配置。进程日志位于 `.cache/local-runtime/<实例>/`。打开 [备课工作台](http://127.0.0.1:8000)，[健康检查](http://127.0.0.1:8000/health) 应为 `healthy`、`arq`、真实 Provider，PG/Redis 和七类 Worker 检查均通过。

本机若手工停止过访问桥，先运行 `docker start lesson-assistant-pg-access-91cd4908`。桥的当前容器ID和受控参数记录在 `.cache/llm-receipts-main-20261020T023254Z-91cd4908/postgres-access.json`。不要修改Windows全局端口保留或重建共享PG数据卷来恢复本项目访问。

前台 Ctrl+C 或在另一终端运行以下命令，会停止接收新任务并等待已有 Worker 任务完成；Docker PG/Redis 和数据继续保留：

```powershell
uv run python scripts/run_local.py stop
```

需要停止 Redis 容器时使用 `docker compose -f compose.local.yaml stop redis`。现有 PG 由 `rag-nextjs` 管理。不要使用删除持久卷的命令。

## 真实完整流程验收

启动所有服务后，可显式执行：

```powershell
uv run python scripts/check_local_flow.py --real-model
```

该命令会消耗真实模型 token，创建合成验收教师及两份七模块数学教案，不读取现有教师的私有教案。它验证一键整课、结构化逐模块确认、教学过程大纲和逐环节正文、SSE 连续序号与断线重放、Redis 通知、PG 正式版本、预览、Word 导出下载。脱敏证据保存在 `.cache/local-real-flow/<实例>/evidence.json`，不保存登录令牌或模型密钥。

SSE 接口始终从 PG 的持久事件表读取正文和状态。Redis Pub/Sub 只唤醒订阅者；断线不会取消后台任务，`Last-Event-ID` 重连可从 PG 补齐事件。本地真实 canary 只证明当前链路可用，不能代替容量测试或教学质量评估。

## 知识库与崩溃恢复

当前迁移head为 `0005`，现有本地业务库已完成升级。已有数据库升级前保留备份并优雅停止旧应用，升级后再启动API/Worker。知识资料在独立[知识库页面](http://127.0.0.1:8000/knowledge)维护；新教案默认检索当前教师全部知识库，也可指定或停用。模型原始回执与结果不确定场景见[模型调用回执与恢复](模型调用回执与恢复.md)。

Worker异常退出时本地启动器会重启该类Worker；Maintenance在租约过期后保留已验证模块/活动断点并投递新epoch。复用条件、重试预算及观察脚本的硬停止核验要求见[知识检索与断点恢复](知识检索与断点恢复.md)。已有真实硬停止恢复证据；完整流程脚本本身不会终止任何Worker。
