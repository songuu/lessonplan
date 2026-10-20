# Langfuse 本机自托管与教案助手接入

本项目使用 Langfuse 开源自托管版。无需 Langfuse Cloud 账号或企业许可证；仍需自行提供 Docker 主机、存储和备份。该配置用于本机开发和验收，单实例不提供高可用。

`compose.langfuse.yaml` 是独立项目 `lesson-assistant-observability`，包含 Langfuse v4 Web、Worker、PostgreSQL 17、ClickHouse 25.12、Redis 和 MinIO。全部数据库、队列及数据卷独立于教案业务系统。仅 Web 绑定 `127.0.0.1:3100`，不公开 PostgreSQL、Redis、ClickHouse、MinIO 或 Worker 端口。

配置依据 2026-10-20 查阅的 [Langfuse 官方 Compose](https://github.com/langfuse/langfuse/blob/main/docker-compose.yml)、[无界面初始化](https://langfuse.com/self-hosting/administration/headless-initialization) 和 [自托管加固文档](https://langfuse.com/self-hosting/configuration/hardening)。Web/Worker 使用官方 v4 镜像；MinIO 使用本机已有的标准 `RELEASE.2024-12-18T13-15-44Z` 镜像。生产升级前应固定已验收镜像摘要并备份，避免把 major tag 的变化当作自动升级授权。

## 1. 准备私有配置

在仓库根目录执行。以下命令不会启动 Docker：

```powershell
.venv/Scripts/python.exe scripts/setup_langfuse.py --headless --dry-run
.venv/Scripts/python.exe scripts/setup_langfuse.py --headless
```

如需指定管理员邮箱，可增加 `--admin-email owner@example.com`。默认账号邮箱为 `admin@lesson-assistant.local`；密码随机生成，仅写入私有配置。脚本生成：

| 文件 | 内容 |
| --- | --- |
| `.cache/langfuse.env` | 独立数据库、Redis、MinIO、加密与会话密钥，以及可选初始管理员、组织、项目和 API 密钥 |
| `.cache/langfuse-app.env` | 教案助手使用的项目 API 密钥、本地地址和仅元数据采集设置 |

两份文件均被现有 `.gitignore` 的 `.cache/` 忽略，脚本不会打印密钥或覆盖已有文件。生成后请用本机编辑器查看初始管理员密码，勿将文件内容粘贴到日志、工单或聊天中。Windows 文件权限继承所在目录 ACL，需确保工作区目录仅对受信任账号开放；支持 POSIX 权限的平台使用文件 `0600`。

已有任意一份配置时，脚本会拒绝重新生成，以免替换运行中数据库的密码或加密密钥。保存现有文件并继续使用，不要通过删除配置来“重新初始化”已有数据卷。

不传 `--headless` 时，只生成基础设施密码，应用上报保持关闭。可启动后在 UI 创建账号、组织、项目，再将项目密钥填入 `.cache/langfuse-app.env` 并设 `LANGFUSE_ENABLED=true`。Headless 模式会关闭开放注册；手动初始化模式保留本机注册入口。两种模式均关闭遥测和注册引导外联。

## 2. 审查并启动独立服务

配置验证使用 `config --quiet`，避免将展开后的密码输出到终端：

```powershell
docker compose --project-name lesson-assistant-observability --env-file .cache/langfuse.env --file compose.langfuse.yaml config --quiet
docker compose --project-name lesson-assistant-observability --env-file .cache/langfuse.env --file compose.langfuse.yaml up -d
docker compose --project-name lesson-assistant-observability --env-file .cache/langfuse.env --file compose.langfuse.yaml ps
```

也可在首次生成配置时显式传 `--headless --start`，让初始化脚本请求启动。默认无 `--start` 时只生成文件；`--dry-run --start` 仍然不会启动。Docker 命令失败时配置保留，脚本不回显可能含密钥的子进程输出。

不要把此文件与业务 `compose.yaml` 合并，也不要对其他项目执行停止、重建或清卷。首次启动需要拉取镜像和执行 Langfuse 自带迁移，应等待服务就绪：

```powershell
Invoke-WebRequest http://127.0.0.1:3100/api/public/health
```

打开 [本机 Langfuse](http://127.0.0.1:3100)，使用初始管理员登录。API 健康正常只表示服务可访问，仍需通过实际追踪读回验证上报成功。

该拓扑仅公开 UI/API，适用于本项目元数据追踪。浏览器直接上传媒体到 MinIO、面向公网的对象下载不在此配置范围；不能因为媒体地址不可访问而直接公开内部对象存储端口。

## 3. 为 API 与 Worker 注入应用配置

在启动教案助手的同一个 PowerShell 进程内加载私有配置，不输出值：

```powershell
Get-Content -LiteralPath .cache/langfuse-app.env | Where-Object { $_ -and -not $_.StartsWith('#') } | ForEach-Object {
  $langfuseSetting = $_ -split '=', 2
  [Environment]::SetEnvironmentVariable($langfuseSetting[0], $langfuseSetting[1], 'Process')
}
.venv/Scripts/python.exe scripts/run_local.py start
```

业务数据库、模型地址、JWT 和 Redis 设置继续来自项目已有 `.env`。该命令不改写它。已有运行实例需按原有方式平滑停止后，从加载配置的进程重新启动，使 API 和全部 Worker 获得相同配置。不能只重启 API。

关键应用配置如下，密钥由脚本或 UI 生成，不使用公共示例值：

| 配置 | 本机含义 |
| --- | --- |
| `LANGFUSE_ENABLED` | Headless 默认为 `true`；未创建项目密钥时保持 `false` |
| `LANGFUSE_BASE_URL` | 宿主机进程使用 `http://127.0.0.1:3100` |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 与独立实例中项目匹配的凭证 |
| `LANGFUSE_PROJECT` | 管理端显示的项目名；不是云端项目选择开关 |
| `OBS_DEPLOYMENT_ID` | 同一部署持久保留，用于恢复执行的追踪关联 |
| `OBS_CAPTURE_MODE` | `metadata`，不采集 Prompt、知识原文和教案正文 |

若业务 API/Worker 运行在 Docker Desktop 容器内，其 `127.0.0.1` 指向容器本身。可为这些进程配置 `LANGFUSE_BASE_URL=http://host.docker.internal:3100`，并从该业务容器验证连通性；不应修改浏览器登录使用的 `NEXTAUTH_URL`。Linux 主机需要相应 host-gateway 解析或经审查的内部网络连接。不要通过开放全部端口解决容器地址问题。

Compose 不导入 AWS 凭证、Langfuse Cloud 或 LangSmith 配置，也不配置付费 AI 功能连接。进程级 SDK 关闭云端上报后，本地执行日志仍可使用。

## 4. 验证链路

1. 登录教案助手管理员，在 `/admin` 选择“执行日志”，或打开 `/admin/execution-logs`。非管理员不得读取日志 API。
2. 发起一个合成教案任务，按任务 ID 筛选。时间线应包含执行开始、实际发生的步骤和终态；实际模型请求显示耗时、首字延迟、已知 token 等受控指标。
3. 详情显示本地 UUID、Langfuse 32位 Trace ID、Span ID、父 Span ID 和执行批次；使用“复制 Langfuse Trace ID”到本机 Langfuse 查找。回执复用应标为复用，不能产生新的真实模型请求或新增 token。
4. 管理端显示“已配置”只说明配置状态。只有在本机 Langfuse 查询到对应追踪及请求指标后，才算上报验收通过。
5. 停止独立 Langfuse Web/Worker 后，再运行合成任务。业务仍应完成，本地执行日志应继续可读；恢复监控平台后验证新的追踪。

初始本地日志可能为空，历史任务不会自动补传。页面默认手动刷新，可显式开启每 5 秒增量刷新；加载更多保留先前记录。若质量校验失败但传输成功，需分别检查质量步骤和实际模型请求，不能将 HTTP 成功等同于业务完成。

仅验证监控链路时，可用合成追踪避免调用模型。默认dry-run不发网络请求，显式live会发送三个无正文的合成步骤，并通过v2 Observations API读回身份和父子关系：

```powershell
.venv/Scripts/python.exe scripts/check_langfuse_flow.py
.venv/Scripts/python.exe scripts/check_langfuse_flow.py --live --env-file .cache/langfuse-app.env
```

成功证据写入 `.cache/langfuse-verification.json`。使用官方原生OTLP路径及v4实时入库header；Langfuse v4的JSON入队确认与标准protobuf响应均支持。确认成功仍不替代读回验证。

`/observability` 的队列/错误计数属于接收请求的 API 进程，本地日志表汇总API/Worker的实际执行记录。Worker发送故障应结合其受控进程日志和Langfuse读回核对。当前未自动删除本地日志；生产使用前需根据存储容量确定日志与Langfuse数据保留、备份策略，不要直接清理业务回执表。

## 5. 保留数据与停止

仅停止本项目，保留全部数据卷：

```powershell
docker compose --project-name lesson-assistant-observability --env-file .cache/langfuse.env --file compose.langfuse.yaml down
```

不要附加 `--volumes`。恢复时沿用原私有配置再执行 `up -d`。Langfuse 的 PostgreSQL、ClickHouse、MinIO 数据卷和加密密钥需一起备份；备份只涵盖监控系统，不替代教案业务数据库备份。修改密钥需要对应系统的轮换流程，不能仅重新运行初始化脚本。

应用回滚只需关闭 `LANGFUSE_ENABLED` 并重启 API/Worker；无需删除本地执行日志或监控数据。

## 6. 验证证据范围

初始化脚本测试覆盖随机密码、项目密钥匹配、无界面模式、默认不启动、dry-run、禁止覆盖、路径重定向、写入失败回滚和敏感输出隔离。运行：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_langfuse_setup.py -q --basetemp=.tmp/pytest-langfuse-setup
```

Compose 静态校验、真实服务健康、项目初始化和追踪读回属于独立验收步骤。未运行某一步时，不应把脚本单测通过表述为完整平台已部署。

2026-10-20 本机已经完成上述部署和读回：Web版本4.64.0、Python依赖锁定4.18.0；合成canary三个观察父子关系一致，独立mock教案任务有40条本地日志、20个远端观察，终态completed且未产生真实模型调用。业务库备份后升级0006，API与七类Worker重启健康；真实Provider账单及生产容量不在本轮证据中。详见[验收记录](verification.md)。

当前运行进程已加载 `.cache/langfuse-app.env`，没有改写业务 `.env`。以后停止/重启应按第3节加载同一份配置，或由你将相应应用配置加入忽略的 `.env`。初始管理员邮箱为 `admin@lesson-assistant.local`，密码在 `.cache/langfuse.env` 的 `LANGFUSE_INIT_USER_PASSWORD`；不在仓库或终端中输出。
