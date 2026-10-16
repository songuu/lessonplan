# 本地 PostgreSQL / Redis / ARQ 验证证据

验证日期：2026-10-16。证据等级：E2 + E3；模型明确使用本地 mock，不是 E4。

## 已验证结果

- PostgreSQL 16.15：在工作区创建全新独立 cluster，仅监听 `127.0.0.1:15432`，专用账号与数据库均为 `lesson_test`。首次迁移前 public 表数量为 0；Alembic `upgrade head` 后为 21，版本为 `0002`。最终验证确认现有 schema 与仓库 head 一致，`alembic check` 返回 `No new upgrade operations detected`。
- Redis 7.4.5：便携 Windows MSYS2 版本，仅监听 `127.0.0.1:16379`。各轮使用事前 `DBSIZE=0` 的专用数据库 15 至 8，没有执行 `FLUSHDB`、`FLUSHALL` 或删除未知数据。中途两次验证脚本自身断言问题已修复，冻结后端的最终全流程独立运行通过。
- 使用真实 loopback HTTP、真实 asyncpg PostgreSQL 连接、真实 Redis 与七个真实 ARQ `Worker`。七 Worker 在同一个 Python 进程中独立消费对应队列；所有 `/health` 心跳通过。
- 一键生成、同键同参并发复用、同键异参 HTTP 409、数据库事件连续序号与唯一终态、`Last-Event-ID` 精确重放通过。
- 向真实 Redis 重复投递同一个业务 Run，Run CAS 防止重复执行，事件集合完全不变。
- 在 Worker 暂停领取期间，只删除刚创建的一个已 published/queued Run 对应的精确 Redis Job 键与 sorted-set 项；真实 maintenance Worker 的 reconcile 以相同稳定 Job ID 恢复入队，Outbox attempts 增至 2，任务完成。
- HTTP SSE 在 Run 未终态前连接；Redis `PUBSUB NUMSUB` 证明应用已订阅。独立订阅者收到纯 `changed` 唤醒消息；实时流包含连续事件及唯一完成终态，连接结束后该通道订阅数为 0。
- 显式注入 Pub/Sub 订阅失败时，真实 HTTP SSE 自动退回 PostgreSQL 轮询，仍得到完整唯一终态。这是订阅能力故障注入，不冒充 Redis 服务整体宕机测试。
- 真实导出队列完成 DOCX，存储字节读回 hash 一致；上传同一 DOCX 后，文档解析队列完成；分块持久化、教师鉴权缩略图端点返回真实可读 PNG，通过字节签名与 hash 检查。
- 真实 HTTP 匿名上传返回 401；已认证的 chunked 请求累计超限返回 413。两种请求都没有进入 multipart parser，证明 ASGI 边界先完成鉴权和累计体积限制。
- 真实解析队列生成临时 PNG 后，在 `commit_document` 前暂停；HTTP 删除 PersonalLesson 成功并取消对应 Run，释放队列后只能保留唯一 `run_cancelled`。个人教案记录消失，存储文件集合恢复原样，无孤儿原文件或 PNG。
- 注入带 `code=None` 的 `StaleDataError`，Run 以 `WORKFLOW_FAILED` 唯一失败收口，已经生成的临时 PNG 被补偿删除；失败记录保留而不伪造成功。
- 润色队列只形成候选，不覆盖模块正式版本。
- 结构化队列完成目标、大纲、活动三个 Run；教师确认大纲的 13 + 31 = 44 分钟被严格保留，没有被初始 40 分钟覆盖；活动环节 ID 与顺序一致；复用已有蓝图；只有大纲时 finalize 返回 409。
- 运行中的真实 ARQ Job 被教师取消后，晚到结果不能提交模块版本；唯一终态为 `run_cancelled`。该项使 ARQ 记一次预期取消失败，不是业务成功或失败混淆。
- 内部审查队列完成，maintenance Worker 消费 Outbox 并写入 Redis Stream。
- 最终独立 asyncpg 连接读回指定 14 个 Worker Run：11 completed、2 cancelled、1 个预期故障 failed；311 个事件、11 条模型调用日志、32 条质量记录、14 条 Outbox 记录。包括同步 finalize 在内，该教师共 15 个终态 Run；sample 分母记录精确等于 15，总成功数 12、取消数 2、失败数 1，没有因重复投递多计。

原始结构化证据保存在 `.cache/infrastructure/evidence-initial.json`（空库迁移）和 `.cache/infrastructure/evidence.json`（冻结后端最终复验，Redis DB 8，耗时 12.25 秒，含停止回执）。最终运行日志为同目录 `test-output-frozen-final.log`；上一轮安全验证另保存在 `evidence-security-final.json`。不包含 API Key 或真实 Provider 上下文。缓存目录属于本机临时证据，不应提交二进制压缩包和数据库文件。测试业务预算为 30 秒，ARQ Job 上限为 60 秒，为业务持久化失败收口留出余量。

本次复验包含 SSE Event Schema 入库验证与风格注册表更新后的完整真实异步闭环。运行时强制 `AI_PROVIDER=mock`、`STORAGE_BACKEND=local`、`ENVIRONMENT=test`，没有继承真实模型或 COS 路径。最终后端源码、受控资产、迁移、依赖锁与验证脚本的组合 SHA256 为 `d13c9da60cf60914dbf834f5d7a656cbd92f8d11a5bef2cb2868e4ee5bd3001f`；逐文件 hash 保存在 `evidence.json.frozen_backend_fingerprint`，便于确认该证据对应的工作区版本。

## 可重复执行

启动独立 PostgreSQL/Redis 后，选取一个未使用的 Redis 数据库。脚本会拒绝非 loopback 地址、默认服务端口、非 `lesson_test` 数据库和已有 Redis 键的数据库；不会清理或重置数据库。

```powershell
$env:LESSON_EXTERNAL_DATABASE_URL='postgresql+asyncpg://lesson_test@127.0.0.1:15432/lesson_test'
$env:LESSON_EXTERNAL_REDIS_URL='redis://127.0.0.1:16379/7'
.venv\Scripts\python.exe scripts/test_external_runtime.py
```

`tests/test_external_runtime.py` 默认只运行地址安全检查，真实环境测试需要显式设置上述环境变量。每轮业务数据使用新随机 ID，不覆盖旧记录。

## 来源与完整性

- PostgreSQL 包从 [EDB 官方二进制页面](https://www.enterprisedb.com/download-postgresql-binaries) 核对为 Windows x64 `16.15`，实际下载 `postgresql-16.15-5-windows-x64-binaries.zip`，373,254,386 字节。实际 SHA256：`43BB45F173A6F08CF1D29A97A6D8DEB119E8E8093A24C00D2D1001A0CCAA8281`。此值是下载后实测 hash，未声称有独立发布方 checksum 核验。
- Redis 包来自 [redis-windows 7.4.5 发布页](https://github.com/redis-windows/redis-windows/releases/tag/7.4.5)，`Redis-7.4.5-Windows-x64-msys2.zip`，11,365,825 字节。SHA256 `E617E1E1631F134AB4C9A630F14D6D39103BB79DCB6179C5D0C7258E211F915D` 与发布页精确一致。这是用于本地验证的 Windows 社区构建，不代表生产 Redis 发行验证。

## 进程所有权与停止

冻结后端最终复验结束后，两个服务已在 `2026-10-16T10:20:20+08:00` 安全停止。停止前核对了本轮 PID、完整可执行路径和启动时间 UTC ticks：PostgreSQL 主进程 PID 31116，Redis PID 30288；随后 PostgreSQL `pg_ctl -m fast -w stop` 与 Redis `SHUTDOWN NOSAVE` 成功，确认两个 PID 不再存在、15432/16379 端口均不再监听。`.cache/infrastructure/instance.json` 的两个服务状态与 `evidence.json` 的 `infrastructure_cleanup` 均为 `stopped`。无遗留 Worker 或 HTTP 服务。上一轮实例身份保存在 `instance-before-final.json`；所有 PostgreSQL 测试数据均保留，未删除测试库。

下列命令仅供以后启动新测试实例后参照使用；已停止实例不得再次按旧 PID 操作。因服务在提权进程启动，读取进程身份与停止也需要相同权限。

```powershell
$taskRoot=(Resolve-Path '.cache/infrastructure').Path
$identity=Get-Content -LiteralPath (Join-Path $taskRoot 'instance.json') -Raw | ConvertFrom-Json
$pgProcess=Get-Process -Id $identity.postgresql.pid -ErrorAction Stop
if ($pgProcess.Path -ne $identity.postgresql.path -or $pgProcess.StartTime.ToUniversalTime().Ticks -ne ([DateTime]$identity.postgresql.started_at).ToUniversalTime().Ticks) { throw 'PostgreSQL process ownership mismatch' }
& (Join-Path $taskRoot 'postgresql/pgsql/bin/pg_ctl.exe') -D (Join-Path $taskRoot 'pgdata') -m fast -w stop
$redisProcess=Get-Process -Id $identity.redis.pid -ErrorAction Stop
if ($redisProcess.Path -ne $identity.redis.path -or $redisProcess.StartTime.ToUniversalTime().Ticks -ne ([DateTime]$identity.redis.started_at).ToUniversalTime().Ticks) { throw 'Redis process ownership mismatch' }
& (Join-Path $taskRoot 'redis/Redis-7.4.5-Windows-x64-msys2/redis-cli.exe') -h 127.0.0.1 -p 16379 shutdown nosave
```

没有安装系统服务，没有修改系统启动项；ARQ Worker 与临时 HTTP 服务已在每轮脚本 `finally` 中退出。

## 当前证据边界

- 尚未取得真实 Provider、COS、生产部署、跨进程压力和操作系统级 Worker kill/restart 证据。
- 宿主 Docker daemon 不可用；这些结果来自显式独立的原生进程，不冒充 Docker/部署验证。
