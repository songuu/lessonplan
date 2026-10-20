# 教案助手 Langfuse 接入与管理端执行日志方案

日期：2026-10-20。版本：v2。状态：已实现并完成本机自托管验收；生产容量与真实模型费用未作验收。替代同日 LangSmith 历史方案。

用户明确要求开源、免费和直接使用 Langfuse，并要求通过 `/admin` 查看相关后端执行日志。因此采用 **Langfuse 开源自托管版本 + 独立本地执行日志**。不依赖 LangSmith、LangChain、LangGraph 或企业版授权；自托管仍占用本机/服务器的计算与存储资源。

Langfuse 官方提供开源 Docker 自托管，其少数附加功能需要企业授权。本项目使用基础追踪、观测、模型用量、成本查看和项目密钥，不依赖这些付费附加功能。[官方自托管说明](https://langfuse.com/self-hosting)

## 架构

```mermaid
flowchart LR
    A[API 与 ARQ Worker] --> B[进程内观察器]
    B --> C[有界异步本地日志队列]
    C --> D[(业务库执行日志表)]
    D --> E[管理员查询 API]
    E --> F[/admin/execution-logs]
    B --> G[有界 Langfuse 发送器]
    G --> H[自托管 Langfuse]
    H --> I[Langfuse 追踪界面]
```

观察器与业务流程分离，默认只记录元数据。关闭或未配置 Langfuse 时，本地日志继续生成、持久化和展示。日志与追踪不会修改模型预算、调用回执、租约或正式版本。

部署使用独立 Compose project，包含 Langfuse Web/Worker、PostgreSQL、ClickHouse、Redis 和 S3 兼容存储。只把管理界面绑定到本机端口，其余数据服务留在隔离网络中。不复用已有教案或其他项目的数据库/Redis，不改动它们的容器。[官方部署拓扑](https://langfuse.com/self-hosting/deployment/docker-compose)

## 追踪身份与范围

| 层级 | 观测范围 |
| --- | --- |
| `lesson.prepare` | API 中实际知识检索和准备；使用教案 thread/session 关联，未创建业务 Run 时不冒充执行日志 |
| `lesson.execute` | Worker 领取租约后的执行、外层超时、取消、恢复和已提交结果 |
| workflow/runtime | 一键、结构化、模块、活动、润色、导出、文档解析和后台审查 |
| generation/quality | 生成尝试、质量校验、质量修正、格式回退 |
| `llm.request` | 每次真实主/备 Provider 请求，映射为 Langfuse generation |
| receipt/checkpoint | 原始响应保存、完整回执重放、断点保存和恢复 |
| `business.commit` | 业务事务提交，不把模型返回当作正式成功 |

业务 Run ID、epoch、trace、span 分别记录。根 trace 由部署标识、Run 和 epoch 派生，恢复批次建立新 trace；同一教案使用同一 thread/session 关联。Langfuse 使用 32 位十六进制 trace ID 和 16 位 observation/span ID，适配器负责与本地 UUID 的稳定映射。原始 UUID 仍可用于 `/admin` 排障。

内部代码显式建立父子上下文，不自动采集整个函数参数，不接受浏览器指定的 trace parent。API 准备阶段与 Worker 是独立追踪，读取已冻结快照不能伪装为再次检索。

## 用量与结果

每次真实请求独立记录模型、路由、主备、耗时、首个非空正文增量时间和 Provider 实际返回的 token。mock 不创建付费 generation。未知 usage 不填零，不自动声称已知费用；自定义模型价格在 Langfuse 配置，估算费用与 Provider 账单区分。

回执重放和断点复用属于普通步骤，带原调用 ID、capture epoch 和复用标志，不再次创建 generation 或累计 token。质量失败保留在质量步骤，模型成功但质量不合格不会被误记为网络故障。取消、租约丢失、结果未知和业务提交失败分别记录。

首字时间只在第一次非空正文 delta 时记一次；每个 token 不产生新 span。正常一键路径只有一次实际请求时，也只有一次 generation。

## 本地日志与管理接口

新增迁移 `0006_execution_logs.py` 和 `ai_execution_logs` 表，存储追加式 start/end/event 记录。字段包括递增 sequence、唯一 event_id、Run、epoch、trace/span/parent、步骤名、类型、状态、级别、时间、耗时和白名单元数据。

本地写入使用独立短事务和有界队列，不把监控 I/O 放入业务事务。旧 epoch 可以追加自己实际发生的诊断事实，但没有任何业务写权限。软删除教案和无有效业务 Run 的数据跳过。重复事件不重复落库。

| 接口 | 功能 |
| --- | --- |
| `GET /api/v1/admin/execution-logs` | 按 Run、workflow、级别、状态筛选，使用 sequence 游标分页 |
| `GET /api/v1/admin/observability` | 查看 Langfuse 安全配置状态、本地写入器状态和计数；不返回密钥 |

`/admin` 增加“执行日志”入口，独立页面为 `/admin/execution-logs`。页面包含筛选、时间线、epoch/调用身份、用量与首字、错误码、刷新、增量分页、显式自动刷新和 trace ID 复制。未配置 Langfuse 不阻止本地查询。既有任务恢复页保留回执、告警和业务事件。

接口沿用管理员权限：未登录返回 401，教师返回 403。API 返回时再次白名单清洗。页面不渲染任意 metadata HTML，不读取服务器任意日志文件，不提供原始 Prompt/正文/堆栈或监控凭证下载。

## 配置和免费部署

| 配置 | 用途 |
| --- | --- |
| `LANGFUSE_ENABLED` | 是否向自托管 Langfuse 发送，默认 false |
| `LANGFUSE_BASE_URL` | 显式自托管地址，无云端地址兜底 |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 项目密钥，Secret 使用敏感字段封装 |
| `LANGFUSE_PROJECT` | 可选管理端项目显示名 |
| `OBS_DEPLOYMENT_ID` / `OBS_RELEASE` | 稳定部署身份与独立发布版本 |
| `OBS_TRACE_SAMPLE_RATE` | 远端确定性采样，本地日志独立记录 |
| `OBS_CAPTURE_MODE` | 当前为 metadata，其他模式需独立明确范围 |
| `OBS_EXPORT_QUEUE_SIZE` / `OBS_MAX_EVENT_BYTES` | 队列和单条数据上限 |
| `OBS_EXPORT_TIMEOUT_SECONDS` / `OBS_SHUTDOWN_TIMEOUT_SECONDS` | 网络发送与停机等待边界 |

新增 `compose.langfuse.yaml`、初始化脚本和部署说明。脚本生成强随机配置写入忽略文件，不把默认密码、项目 Secret 或初始化账号写入仓库，也不覆盖用户现有配置。headless 初始化用于隔离的本地实例，不创建付费订阅。生产环境另行配置域名/TLS、备份和保留策略。

客户端 SDK 当前锁定 `langfuse==4.18.0`。适配器采用官方支持的原生 OTLP 路径，使用该版本的语义字段和 OpenTelemetry 编码器，不初始化全局 provider 或叠加 SDK 后台队列。完成记录由本项目有界发送器上传；准确开始/结束时间、首字和用量有实际版本的契约测试验证。[原生 OTLP 接入](https://langfuse.com/integrations/native/opentelemetry)

## 故障和数据边界

默认只允许受控标识、枚举、计数、时间和指纹；课题、教师姓名、文件名、资料/正文、选区、反思、密钥与原始异常都不上传或写入执行日志。数据进入队列前清洗，管理响应再清洗。

本地和远端队列均有界，满载时丢弃诊断并计数，不阻塞生成。发送有限超时与重试，云端/自托管不可用时熔断并输出受控本地诊断。关闭先完成业务任务，再在预算内排空。硬崩溃可能损失尚在内存中的诊断，因此数据库回执、终态和版本仍是业务事实依据。

<!-- design-authority:start -->
## 验收条件

- **AC-01**：监控开关不改变模型请求次数、重试预算、SSE、执行权、版本和终态语义。
- **AC-02**：无丢报的采样执行拥有根 trace 和实际步骤层级，每个真实请求独立映射为 generation。
- **AC-03**：不同 epoch 分开追踪，同一教案可关联；不同用户和并发任务的上下文不串线。
- **AC-04**：回执重放和断点复用不再次累计模型用量或费用。
- **AC-05**：传输、质量、取消、结果未知、租约与正式提交结果保持独立且准确。
- **AC-06**：实际本地/远端 payload 和管理响应不包含禁止采集的隐私或凭证。
- **AC-07**：队列满、发送失败和关停超时有界退化，不改变业务异常和取消语义。
- **AC-08**：API 与所有 Worker 生命周期正确，禁用远端时不请求监控平台。
- **AC-09**：用量未知、mock、未定价和价格估算可区分，主备与重放无重复统计。
- **AC-10**：提供本地测试、开关对照性能和真实自托管读取验证的证据；未验证项单列。
- **AC-11**：API 准备与检索记录实际阶段，不改幂等摘要或 ARQ 契约，不信任公网 parent。
- **AC-12**：管理员可以通过 `/admin` 查看持久化的执行步骤、模型、重试、恢复和错误；教师无权查看。
- **AC-13**：整个接入不安装/依赖 LangSmith，不需要 Langfuse 企业版授权，部署与业务资源隔离。
- **AC-14**：未配置或禁用 Langfuse 时，本地日志仍可落库、分页、筛选、刷新与跨应用重启查询。
<!-- design-authority:end -->

## 实施任务与证据

| 任务 | 文件范围 | 依赖 | 风险 | 证据 |
| --- | --- | --- | --- | --- |
| 观察器与 SDK | `app/observability/*`、SDK/单元测试、依赖锁文件 | 无 | L2 | AC-02/03/06/07/08/09 |
| 本地日志 | models、0006、execution_logs service、管理员接口及测试 | 观察器事件契约 | L2 | AC-06/12/14 |
| 运行接入 | factory、Worker、Dispatcher、Runtime、Gateway、回执日志及集成测试 | 观察器/本地写入器 | L2 | AC-01/02/04/05/08/09 |
| 管理界面 | App、Pages、ExecutionLogs、types、styles、前端测试 | 管理 API 契约 | L2 | AC-12/14 |
| API 检索 | lessons API、knowledge 检索与幂等回归 | 观察器 | L2 | AC-03/11 |
| 自托管 | Compose、初始化脚本、部署说明与测试 | 免费功能/拓扑核对 | L2；实际启动 L3 | AC-10/13 |
| 联合验收 | 观测/回执/恢复测试、浏览器、迁移、SDK mock 和真实自托管 | 上述实现 | L2；迁移/外部写入 L3 | 全部 AC |

测试先覆盖失败行为，再实现。按新增观测测试→回执与恢复定向回归→全量后端、Ruff、资产验证→前端测试和构建→隔离迁移→浏览器→真实 Langfuse 读取的顺序扩大范围。已有用户文件和未提交改动保留，不自动提交、推送或写学习记录。

本地实际迁移前检查连接目标、活动任务和备份；先在独立 SQLite/PG 环境验证 upgrade/downgrade。验证用合成账号和内容，不终止已有用户 Worker。性能比较使用相同假模型/负载的开关对照，不把外部模型网络抖动当成埋点开销。

## 回滚

关闭 `LANGFUSE_ENABLED` 可停止远端发送，本地日志继续。若需完整回退，可回滚镜像和代码；新日志表独立，保留日志数据，无需改动教案、调用回执和版本。真实数据库不自动 downgrade 或删除卷。已写入 Langfuse 的数据按自托管保留策略处理。

## 实施结果（2026-10-20）

观察器、原生OTLP导出、本地日志、执行接入、管理员API/UI和隔离自托管部署均已落地。后端全量289 passed/7 skipped，追加最新SDK/Gateway等定向39 passed/1 skipped；真实PG独立并发5 passed，前端全量66 passed及构建通过。PG测试在取消序列化锁时准确复现游标竞态，恢复后通过，临时验证库已清理。迁移往返与check通过，业务库备份后0005→0006无模型漂移，API与七类Worker健康。

真实自托管canary读回3个观察并验证父子关系；完整mock教案任务有40条本地日志、20个远端观察、completed终态，零真实Provider请求。浏览器验证管理员入口、空状态、筛选、分页、详情、32hex复制和恢复深链接。源码与实际SDK payload测试覆盖禁止采集字段、部分/未知usage、重放、故障、采样和有界退化；独立代码、Python、TypeScript及安全审查无剩余可行动P1/P2。

进程内开关对照使用相同1ms合成负载、每轮200次执行、5轮中位数；关闭远端1.655ms/次，启用真实OTLP编码+MockTransport确认1.675ms/次，增量0.020ms/次。该结果不包含业务数据库、网络或模型，不能外推生产时延/吞吐。实际用量与首字的编码契约已测；本轮没有重新调用收费模型、核对真实账单或执行生产容量测试。详细命令、私有证据位置和后续配置见[验收记录](../verification.md)及[部署说明](../Langfuse自托管部署.md)。

## 参考

- [Langfuse 自托管与功能边界](https://langfuse.com/self-hosting)
- [Docker Compose 部署](https://langfuse.com/self-hosting/deployment/docker-compose)
- [官方 Compose 配置](https://github.com/langfuse/langfuse/blob/main/docker-compose.yml)
- [Headless 初始化](https://langfuse.com/self-hosting/administration/headless-initialization)
- [手动 SDK 埋点](https://langfuse.com/docs/observability/sdk/instrumentation)
- [SDK 高级配置](https://langfuse.com/docs/observability/sdk/advanced-features)
