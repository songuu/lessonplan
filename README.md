# 教案助手

面向 K12 教师的备课工作台：React/TypeScript 界面，FastAPI 后端，持久化草稿、版本、运行事件、润色候选、快照与导出。

## 本地运行

需要 Python 3.11+、uv、Node.js 22+。在仓库根目录运行：

```powershell
uv sync --frozen
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv run alembic upgrade head
Set-Location frontend
npm ci
npm run build
Set-Location ..
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

打开 [备课工作台](http://127.0.0.1:8000)，注册自己的账号。接口说明：[OpenAPI](http://127.0.0.1:8000/docs)。开发界面可在 `frontend` 运行 `npm run dev`，Vite 代理 `/api` 到 8000。

默认配置为 **SQLite + inline + mock 演示模式**；演示正文有明确标识，不调用真实模型。生产模式拒绝演示配置。应用启动不自动建表，迁移必须显式执行。

管理员显式初始化，无默认凭证：

```powershell
uv run python scripts/manage.py create-admin --email admin@example.com --name 管理员
```

密码隐藏输入；已有账号需显式 `--promote` 才能提升权限。

## 功能

- 一键整课、逐模块结构化生成，7 默认 / 5 可选 / 自定义模块配置冻结。
- 教学过程大纲编辑、排序、确认、逐环节生成和蓝图复用。
- 富文本编辑、公式预览、保存版本、历史恢复和乐观锁。
- 整模块 / 选区 AI 润色候选，教师明确保存后更新正式正文。
- SSE 正文、断点重放、运行恢复、取消和失败保留正文。
- 预览快照、异步 DOCX/PDF 导出、权限校验下载。
- 个人教案上传解析、冻结风格参考、独立课后反思和润色。
- 独立私有知识库、文档分块与 BM25 检索，生成时冻结参考片段及出处。
- 个人设置、模块偏好、管理端用户/运行/问题/审计、受控埋点。
- OpenAI-compatible 主备模型、质量门、安全修补、后台跨模块审查。
- PostgreSQL/Alembic、Redis/ARQ 七类隔离 Worker、dispatch Outbox、稳定 job ID。
- 有界格式与质量失败重试、持久模块/活动断点、执行租约及 Worker 崩溃续跑。

## 真实 AI 与队列

当前本机已接入与 `rag-nextjs` 相同的 Docker PostgreSQL 17 实例（`127.0.0.1:5432`），教案助手使用独立的 `lesson_assistant` 库。Redis 使用本项目独立 Docker 容器和持久卷（`127.0.0.1:56379`）。已有账号、教案与版本已保留；模型配置从忽略的 `.env` 读取。

日常启动与停止（API 和七个 Worker 为独立进程）：

```powershell
docker compose -f compose.local.yaml up -d redis
uv run python scripts/run_local.py
# 另一终端请求优雅停止；Docker 服务和数据保留
uv run python scripts/run_local.py stop
```

前置配置、首次迁移和真实模型验收见[本地 Docker 完整流程](docs/本地Docker完整流程.md)。`compose.local.yaml` 只管理 Redis，现有 `rag-system-postgres-1` 继续由原项目管理。

知识库操作、检索方式、重试预算和断点恢复边界见[知识检索与断点恢复](docs/知识检索与断点恢复.md)。已有数据库先备份并停止旧应用进程，再执行 `alembic upgrade head` 升级至 `0004`。管理员可在 `/admin/recovery` 查看投递、断点、累计预算和告警，操作说明见[任务恢复管理与告警](docs/任务恢复管理与告警.md)。

真实凭证通过忽略的 `.env` 或运行环境注入：`AI_PROVIDER=lesson_plan_ai`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`。也可分别设置 `LLM_ONE_CLICK_MODEL`、`LLM_STRUCTURED_MODEL`、`LLM_MODULE_OPERATION_MODEL`、`LLM_REVIEW_MODEL`。备用路由为 `LLM_FALLBACK_*`。教师端不提供模型选择。

`TASK_QUEUE_MODE=arq` 时启动 Redis 与七类 Worker：

```powershell
uv run arq app.tasks.workers.OneClickWorkerSettings
uv run arq app.tasks.workers.StructuredWorkerSettings
uv run arq app.tasks.workers.ModulePolishWorkerSettings
uv run arq app.tasks.workers.ExportWorkerSettings
uv run arq app.tasks.workers.ThumbnailWorkerSettings
uv run arq app.tasks.workers.InternalReviewWorkerSettings
uv run arq app.tasks.workers.MaintenanceWorkerSettings
```

完整本地容器拓扑：`docker compose up --build`。包含独立 PostgreSQL/Redis、singleton 迁移、API 和七类 Worker，依赖端口仅监听本机。生产发布见 [部署说明](deploy/README.md)。COS 可选依赖 `uv sync --extra cos`，设置 `STORAGE_BACKEND=cos` 与服务端环境凭证。

## 验证

```powershell
uv run pytest -q --basetemp=.cache/pytest
uv run ruff check .
uv run python scripts/validate_assets.py
docker compose config --quiet
Set-Location frontend
npm test
npm run build
```

证据与未验证项见 [验收记录](docs/verification.md)。便携测试使用独立临时 SQLite，真实服务测试只允许显式独立可销毁环境。

- [教师端 HTTP/SSE](docs/前端接口对接文档.md)
- [管理端接口](docs/管理端前端接口对接文档.md)
- [状态与版本](docs/就绪状态与流转规则.md)
- [数据库契约](docs/后端数据库契约与实现对照V1.md)
- [AI 运行时](docs/AI算法运行时接入说明.md)
- [知识检索与断点恢复](docs/知识检索与断点恢复.md)
- [任务恢复管理与告警](docs/任务恢复管理与告警.md)
- [实施计划](docs/plans/2026-10-16-lesson-assistant.md)

两份根目录设计文档保持原稿。它们描述的历史代码与 `0031` 迁移不属于此仓库，本实现从 `0001` 开始。知识图谱、长期画像、教师质量评分属于原文非目标。旧二进制 `.doc`、扫描 PDF 明确拒绝；复杂不支持公式保留源码并记录警告。教师需核对教材事实与导出结果。
