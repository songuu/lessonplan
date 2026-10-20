# 本地交付验收记录

日期：2026-10-16。范围：两份用户原文V1功能及后续知识检索、质量重试、模块恢复的本地实现与验收。后续真实模型验收已获授权，材料均为合成资料；未执行生产发布或COS外部写入。早期记录保留其当时边界，以追加验收为准。

## 证据集合

- E0：实现计划、代码、OpenAPI、数据库/状态/错误/运行时/队列/接口文档。
- E1：追加SSE修复后的Python全量72 passed / 1外部opt-in skipped（117.75秒）；前端22/22测试、TypeScript和生产构建通过。基线依赖审计零漏洞，本次没有修改依赖。Ruff、37资产校验和diff check通过。
- E2/E3：V1基线在独立PostgreSQL16.15与Redis7.4.5复验通过（Redis DB8事前为空，12.25秒），Alembic空库0002/升级/无漂移、七类真实ARQ Worker、loopback HTTP、实时PubSub/重放/通知故障降级/丢失Job复投/取消/导出/上传/删除/补偿等通过。14 WorkerRun、311 Event；含Finalize为15终态，sample成功12+取消2+失败1精确一致。详见[独立运行证据](external-runtime-evidence.md)及其源码指纹。本次SSE显示修复未重跑该PG/Redis环境，新增3项独立真实TCP SSE提前交付测试。
- 浏览器：注册、一键七模块、手动保存及stale、润色候选隔离/采用/保存、Word实际下载、独立反思、教学过程教师修改43分钟大纲/排序/活动/确认/Finalize、预览、个人库DOCX上传及实际私有缩略图。个人库结构化摘要曾触发React渲染错误，已修复、加入页面回归并在最新构建重新验证。截图已通过工具展示，没有落盘截图文件。

## 需求与验收映射

| 场景 | AC | 实现 / 证据 |
|---|---|---|
| 注册、登录、注销撤销、档案、角色与所有权 | 1/3/4 | core/security、auth、test_auth/test_admin_and_library |
| 7默认/5可选/自定义、别名与顺序、配置冻结 | 1/2 | core/catalog、module_registry、test_platform/test_runtime |
| 一键逐模块、临时/最终、自检后唯一终态 | 1/4 | WorkflowExecutor、SSE Schema、test_workflows、E3 |
| 蓝图复用、确认依赖、大纲/活动教师门与Finalize | 2 | runtime、interactive API、test_workflows、E3及浏览器 |
| 编辑三重锁、恢复、下游失效、撤销隔离 | 2 | ModuleVersion、test_workflows、前端review回归 |
| 整模块/选区候选、人工采用、独立反思 | 2/3 | PolishSession/Message、Reflection、测试及浏览器 |
| Markdown/KaTeX/公式/化学式/IPA、DOCX/PDF | 3 | DocumentAdapter、test_documents、Poppler可视检查、实际下载 |
| 个人库解析、年级课型、chunks、私有缩略图与冻结风格 | 3 | parse_document、0002、test_admin_and_library、E3 |
| 幂等、并发、取消、晚到提交、异常code=None | 1/4 | DB约束、test_reliability/test_security_regressions、E3 |
| 真回执、dispatch Outbox、丢失job恢复、七队列心跳 | 4 | Dispatcher、Maintenance、E3 |
| 管理审计、调用fallback、问题/样本分母、产品埋点 | 4/5 | admin、CallLog、Issue、AnalyticsEvent、E3精确计数 |
| 受控资产、服务探针、迁移与部署工件 | 5 | validator、release_bundle、Compose/CI/Docker、文档 |

## 独立审查与修复

code-reviewer、python-reviewer、typescript-reviewer、security-reviewer分别审查。已修复Run/Outbox及Snapshot/Export FK写入顺序、锁顺序、跨workflow幂等键、超时语义、取消关联状态、outline重确认、下游失效、丢失job恢复、专用模型路由、JSON截断、文件提交补偿、样本统计隔离，以及前端3P1+2P2编辑/异步竞态。安全8项回归和对应独立复核通过。最后的SSE正常EOF重连埋点遗漏通过先红后绿修复，终态EOF不误报，已纳入最终16项前端测试，独立定向5/5复核通过。当前没有未闭合finding，未发现可操作问题；下面仍保留验证边界。

## 既有本地运行（2026-10-16记录）

最终前后端在 `http://127.0.0.1:8000` 运行。当前已切换 Docker PG17 + Docker Redis8.4 + ARQ + 真实模型 `deepseek-flash`；`/health` 为 `healthy`、`arq`、`lesson_plan_ai`，PG、Redis 及七类独立 Worker 均通过检查。原 SQLite 模式账号与教案保留。服务为本机回环监听，不是外部发布；日常启动见[本地流程](本地Docker完整流程.md)。

源码索引为本机`.cache/release-bundle.json`，记录允许列表文件、迁移head `0003`及37项受控资产指纹；不包含凭证、业务数据库或上传文件。它是交付证据索引，不代表已构建发布镜像。

## 追加验收：两种生成模式实时SSE（2026-10-16）

- 修复前：目录缺少当前Run的实时模块状态，页面停留在最初选中模块；结构化大纲等待完整JSON才显示。新增前端5项与后端3项测试先复现失败，再实现修复。
- 一键与结构化统一消费模块开始/正文增量/临时完成/质量检查/正式完成事件，显示真实模块名称、顺序和已生成数量，默认跟随当前模块。教师可手动查看之前模块，再恢复跟随。
- 大纲按完整JSON环节增量转译为Markdown；JSON控制语法不进入正文。活动显示环节序号，重试仅替换当前环节，保留已完成环节。JSON兜底按完整模块增量发布，分批兜底直接传递正文增量。
- Provider重试清除废弃尝试；正式版本读取成功前保留流正文。独立审查发现首次读回失败后手动重试不能退出预览，已用失败回归修复；无未关闭P1/P2。
- `tests/test_streaming_generation.py`共7项，包括3项真实loopback HTTP：暂停Provider调用时，客户端已读到`content_delta`，Run仍为running、模块没有正式版本；释放后收到唯一完成终态。使用临时SQLite和本地受控Gateway，没有调用真实Provider。
- `frontend/src/streaming.test.tsx`共6项，覆盖两模式增量渲染、模块跟随与手动查看、attempt reset、正式版本延迟读取与失败后重试。全量前端22项通过。
- 浏览器在最终构建上完成一键七模块，以及结构化大纲→确认→三个活动环节→正式编辑器。演示模型很快，浏览器截图核对的是完成后接续；生成尚未结束时的真实交付由上述TCP测试证明。截图位于本机`.cache/streaming-final-ui.jpg`。

## 追加演示：独立ARQ进程与SSE双链路（2026-10-16）

`scripts/demo_arq_sse.py`在隔离PG16.15/Redis7.4.5上完成实际运行。API PID25536、one-click Worker PID12600、structured Worker PID32760，各自独立于驱动进程；不改变8000端口应用。使用显式本地mock，增量延时60ms用于观察连接断开。

API在Worker启动前71ms返回queued任务；同请求复用Run。SSE接收事件1..5后主动关闭，独立PG查询序号由5增长至16，Run仍running；Last-Event-ID=5重连后收到6..60共55条，合并严格为1..60。最终7个正式模块版本、唯一完成终态；结构化3个Run分别记录2/1/3次模型调用，最后完成整份教案。Pub/Sub实际仅收到changed，正文从PG Event回读。

本轮独立审查发现并修复演示脚本总截止时间、Redis证据脱敏和异常子进程收尾3项P2；新窄回归2/2通过，最终真实重跑三子进程均协作退出0、cleanup_errors为空。临时PG/Redis按PID/路径/启动时间核验后停止，停止回执追加在本机`.cache/arq-sse-demo/c5a3f2a1c99841fbbfed78702e47583f/evidence.json`。未执行真实模型调用、容量压测或操作系统级Worker崩溃恢复。

完整操作步骤、代码定位和当前实现与原描述的差异见[队列说明](生产并发与队列隔离说明.md#独立进程与断线恢复演示2026-10-16)。交互回放的8步、前后切换、首尾边界及宿主状态恢复通过JSDOM检查，并经独立JS审查；该回放使用已取得的证据，不连接运行中的服务。

## 追加验收：本地 Docker 与真实模型完整链路（2026-10-16）

- 复用 `rag-nextjs` 的 `rag-system-postgres-1`（PG17，127.0.0.1:5432），新建独立 `lesson_assistant` 库；未改动 `rag_system`。`compose.local.yaml` 启动独立 Redis8.4 容器（127.0.0.1:56379），AOF 与独立持久卷已启用。API 和七 Worker 在宿主机独立进程运行，未声称应用镜像已构建。
- 原 SQLite 2 个账号、8 份教案、43 个模块版本及其余业务数据通过 backup API 留存备份并复制；旧 API 停止后再次逐行核对 20 张业务表全部一致。JWT、模型设置与本地文件存储保留。
- 真实模型列表接口确认配置模型存在。合成教师完整验收产生两份七模块数学教案，包含一键整课、结构化所有模块确认、大纲及三个课堂环节、整课完成、预览、两份 Word 导出下载并验证 DOCX 内部文档。共 11 个运行、12 个真实模型调用，全部成功；本机脱敏证据 `.cache/local-real-flow/13d62fbaee644090943b5d7d425b6ca3/evidence.json`。
- 一键正常路径仅一次真实模型调用，约 15.8 秒，1174 条持久事件。SSE 首次收至序号5后断开，PG 序号由7增长至128，重连严格补齐序列；Redis 实际收到 `changed`。
- 独立审查补强验收：生成任务必须有非空模型调用回执和正文增量；Pub/Sub 无通知/错误通知不能以 PG 轮询通过替代；收到首批增量时独立 PG 查询必须仍为 running 且正式版本数为0。新增窄回归6/6，增强后的真实一键短程重验通过，证据 `.cache/local-real-flow/c8e65bf63a7f4441a1c1d4325406654f/evidence.json`。
- 修复 ARQ 读取配置类自身 `__dict__` 而遗漏基类配置的问题，保证 CLI、Docker 与本地启动器入口一致。七个真实 Worker 构造回归通过，未连接真实模型。启动器停止验证中八个子进程全部退出，随后重启服务并完成上述短程重验。
- 本轮后端全量88 passed、1 skipped；其后新增纯验收回归6 passed，合计94个不同测试通过。Ruff与本地Compose配置检查通过。两位独立审查者的启动、目标URL与验收假阳性问题均已关闭。未修改前端，本轮没有重复运行前端测试。

## 追加验收：独立知识库、质量重试与模块断点（2026-10-16）

- 新增教师私有知识库 CRUD、异步资料解析/重建索引、片段分页、BM25检索及来源展示。真实合成资料经thumbnail Worker解析，检索命中原文；生成草稿冻结同一来源并传给模型。浏览器完成登录、可检索状态和带文件/片段出处的检索，截图位于 `.cache/knowledge-final-ui.png`。
- 通用格式/业务硬门失败修正覆盖蓝图、模块、大纲、活动、润色与审查；默认额外1次，调用/次数/时间预算跨恢复累计。持久checkpoint及恢复再次校验，事件回调失败不冒充模型质量错误。自动回归覆盖次数耗尽、当前活动重试、旧正文重放及不重复正式版本。
- PG业务库迁移前备份至 `var/backups/pg-before-0003-20261016T041716Z.dump`（643106字节，已验证可读）；0002→0003成功、Alembic无漂移。原20张业务表的原列投影行数与hash全部保持不变。脱敏回读位于 `.cache/pg-0003-readback.json`。
- 两类真实PG并发问题均经唯一可销毁数据库先红后绿验证：知识库删除/解析的旧FOR UPDATE死锁→父表NO KEY UPDATE正常完成（`.cache/postgres-knowledge-lock-evidence.json`）；流式事件与心跳的旧双UPDATE引起FK父表锁反转→PG先锁Run再采时、单次UPDATE正常完成。新增确定性交错及旧机制对照2/2通过（`.cache/postgres-heartbeat-control.json`），所有临时库已移除。
- 最终代码上实际硬停止one-click Worker PID25172，启动器自动重启为19936；同一Run从epoch0恢复为1且recovery_count=1。已完成2模块checkpoint的ID/hash均未变化，最终7个正式版本，1567条SSE事件连续且只有1条run_completed。真实模型回执有中断事实及后续成功调用，未使用mock。证据 `.cache/worker-recovery/fe9e9d89f7db4c1ba469068cb1c7a95b/evidence.json` 含身份核验和独立硬停止回执。
- code-reviewer、python-reviewer、typescript-reviewer、security-reviewer已完成独立审查；心跳修复另经两位审查者定向复核，无未关闭的可行动P1/P2。
- 最终后端全量 `157 passed, 3 skipped`（267.71秒）；3项为显式外部环境opt-in，其中本轮新增的两项PG并发对照已另外连接专用隔离库通过2/2。前端全量34/34、TypeScript、Ruff、37受控资产、两份Compose配置和diff检查通过。源码索引为141个允许列表文件、迁移head0003。
- 修复后重新执行真实模型完整canary：11个Run、12次真实模型调用全部成功，完成两份七模块教案、结构化确认、大纲与三个活动环节、预览，以及两份合法DOCX（39343/42522字节）。一键共1334条事件；断线游标5时Run仍running且正式版本0，离线PG序号6→87，重连严格补齐，实际收到Redis changed。证据 `.cache/local-real-flow/38ec4d5b37fb455fa1a269ae24753133/evidence.json`。最终 `/health` 为healthy，PG、Redis及七类Worker均正常。

## 追加页面现场演示：上传至重试与崩溃续跑（2026-10-16）

本轮由浏览器实际点击执行，使用既有合成教师 `知识库界面验收`。界面停留在完成结果，产品源码、模型配置未改变。

| 步骤 | 页面操作与实际结果 |
|---|---|
| 上传与检索 | 新建“现场演示｜图形与几何”，通过文件选择器上传 `长方形与正方形_公开教学资料.md`，异步解析为1个片段；检索“先折后量再举反例 长方形”命中原文与文件/片段出处 |
| 参与生成 | 新建三年级数学教案，只选本轮知识库；详情展示冻结来源K1；正式正文包含蓝色方格纸、折纸测量、反例辨析和[K1]引用 |
| 质量失败重试 | Run `b0a7d4d2-5152-4a1d-913b-888b28f62da4`：首个真实Provider响应的教材分析被受控替换为高中物理内容，真实质量门拒绝；页面实际显示“第2/2次”、6/7模块；第二次真实模型调用修正成功。其余6个checkpoint ID/hash保持不变，最终7版本、1147连续事件，调用validation_outcome分别failed/passed |
| Worker崩溃续跑 | Run `1994a020-10c4-429e-b935-c1c6f6381d6e`：页面2/7模块时核验进程身份并硬停止PID328，启动器重启为21600；同Run epoch0→1、recovery_count=1，原2个checkpoint ID/hash不变，最终7个正式版本和唯一完成终态；独立真实HTTP SSE验证1..1314连续 |

质量失败是明确标注的进程内受控响应注入，不声称真实模型自然返回了错误；校验、修正调用、预算、持久化和SSE全部沿用产品实现。观察暂停仅供截取真实状态，有截止时间，不伪造事件。崩溃为独立核验可执行文件、完整命令行、运行实例、启动时间并持有句柄后的实际OS硬停止。作用域按随机marker及Run的ContextVar隔离，经两位独立审查和纯内存并发回归，未标记请求复用module_id不会触发故障。

本轮证据目录 `.cache/page-demo/`：上传/检索/知识选择/冻结来源/真实重试/正文采用/硬停止前/恢复后完成截图，`quality-before.json`、`quality-after.json`、`crash-before.json`、`crash-after.json`、`kill-receipt.json`、`sse-recovery.json`。`08-checkpoints-restored.png`截取时两个模块已恢复并继续原模块进度，未保留下短暂的“已恢复”提示；恢复事件本身见持久phase与SSE记录。质量与恢复的观察暂停均到时自动继续，此后写入release文件不作为任务推进的必要条件。

演示后已优雅停止全部临时进程，恢复普通 `scripts/run_local.py` 入口，并实际核验one-click命令行不含page-demo。最终健康检查healthy，PG/Redis及七类Worker均正常；知识库、两份教案及演示证据保留。

## 追加验收：模型调用回执与恢复重放（2026-10-20）

- 新增0005 `llm_invocations`：请求前提交唯一意图，完整模型原文及hash独立短事务提交；最终 `applied` 与业务结果同事务。恢复沿用原调用身份、累计调用预算及质量尝试，覆盖一键批量、结构化、蓝图、活动、润色和审查。
- `tests/test_invocation_integration.py` 使用真实服务事务/租约及受控CountingGateway，覆盖响应回执已提交、首模块断点前、业务Commit前、Commit成功但确认丢失四个窗口：总模型调用均为1，最终7个正式版本、唯一成功终态、事件序号连续。第五项在传输中断且无完整回执时，恢复为 `LLM_OUTCOME_UNKNOWN`，不发第二次请求并留下持久告警。这里使用取消异常模拟进程突然退出，不声称执行了本轮真实Provider或OS硬停止。
- 存储测试覆盖并发领取、回执不可变、上下文隔离、旧执行者只能归档原请求、当前业务fencing及事务回滚；管理API教师403，标量投影不读取/返回模型正文、Prompt或凭证。前端展示回执状态/指纹/用量/时间，缺少回执的旧接口兼容，SSE重放阶段不会误标模块完成。
- 独立复审发现“一键首批质量失败、最后一次修正成功并保存、模块断点前崩溃”时旧batch覆写新质量结果。新增精确回归先红后绿修复；恢复采用原修正回执、0新模型请求，真实失败重试额度不放宽。general/Python两位审查者复核关闭，前端TypeScript独立审查通过，无未关闭P1/P2。
- 后端全量 `216 passed, 7 skipped`（459.78秒）；该全量收集后新增上述崩溃回归，最终版本5个回执专项文件 `40 passed`（63.88秒）。外部数据库测试在默认全量中按opt-in跳过。前端54/54、类型检查、生产构建、Ruff、37受控资产和 `git diff --check` 通过；未修改依赖，既有Lesson大包体提示仍存在。
- 最后核对用量发现Gateway原先将Provider缺失usage填为0，与新增管理页“实际用量”的语义不符。补充JSON/SSE共8项先红后绿回归，改为只保留Provider明确上报的非bool、非负整数；缺失显示未知，真实0保留，usage不能覆盖请求元数据。该修复后的Gateway/原Runtime/回执Runtime共 `47 passed`（1.05秒），证据 `final-gateway-tests.xml`；两位审查者再次定向复核通过。正式CallLog旧整数字段保持缺失默认0的既有兼容行为，新回执元数据不会伪造未知值。
- 另用已缓存的原生PostgreSQL16.15启动全新随机集群，仅监听127.0.0.1:16432；新库 `lesson_recovery_verify_5cc8f2f3e9f04465a3e088a3acc9f16a` 中完成升级head、无漂移检查、降至0004、再次升级与检查，全部返回0。真实PG调用并发领取/重复回执、业务回滚/新epoch重放及原有投递/告警并发共4/4通过（0.74秒）。按新目录、PID文件、启动时间及 `SHOW data_directory` 核验后精确停止，PID文件已移除，所有证据保留在 `.cache/llm-receipts-20261020-a61d/native-pg-5cc8f2f3e9f04465a3e088a3acc9f16a/report.json`。这是独立PG16验证，不代表现有Docker PG17业务库已升级。前两轮仅受沙箱令牌与Windows继承管道影响，未执行迁移；最终启动脚本经复审修正为文件日志，并安全停止每个已启动的自建实例。
- 当前Docker Desktop启动因Secrets Engine的旧 `engine.sock` 无法访问/重命名失败，Docker引擎管道不可用，127.0.0.1:5432拒绝连接。本轮未迁移 `lesson_assistant`、未改 `.env`、未重建Docker容器或卷，API与Worker未重启。0005已具备迁移源文件；业务库应用需在Docker恢复后核对无活动任务、备份并升级。本轮未恢复DBX或浏览器自动操作。

设计、回执状态、迟到响应权限与可在DBX执行的只读SQL见[模型调用回执与恢复](模型调用回执与恢复.md)。完整回执已经可靠提交才能保证重放；模型已完成但所有可靠存储均无回执时，当前Provider无已验证的幂等/结果查询接口，因此停止自动重复付费调用。该边界不能由ARQ、Redis锁或单个本地事务消除。

### 同日恢复执行：业务库迁移与真实模型完整流程

- 用户启动Docker后，原PG17容器与Redis均健康；宿主5432仍拒绝连接。检查确认Windows排除端口区间5405–5504包含5432，独占bind返回10013。已先备份 `lesson_assistant`，确认0活动生成任务和0执行中数据库查询；一次原容器短暂重启未恢复端口，随后采用本项目独立访问桥 `127.0.0.1:15432 → rag-system-postgres-1:5432`。桥为非root、只读、无数据卷、固定上游及回环发布；Docker中原PG集群标识与TCP连接精确相同。`.env`仅改DATABASE_URL端口5432→15432，逆改该一处后与原文件SHA完全一致；模型、JWT及其他设置保持不变。
- 迁移前备份 `var/backups/llm-receipts-before-0005-20261020T023254Z-91cd4908.dump`（1,863,956字节），SHA及 `pg_restore --list` 校验通过。复审后的脚本持有普通启动器同一OS锁，确认8000无监听、0活动任务、原库0004及同集群后，完成0004→0005与Alembic无漂移检查；25张原业务表、34,257行的原列计数/hash全部保持不变。没有对业务库做降级或在 `rag_system` 写入数据。
- 普通 `scripts/run_local.py` 已恢复运行，实例 `da9cdea69a5d4bd8b521fa17f5050ae7`。最终 `/health` 为healthy/arq/lesson_plan_ai，PG17.11、Redis与七类独立Worker均健康。端口桥容器 `lesson-assistant-pg-access-91cd4908` 随Docker自动恢复；当前DBX应连接127.0.0.1:15432，数据库及认证沿用原lesson_assistant连接。
- `scripts/check_local_flow.py --real-model` 完整通过：11个前台运行、12次真实模型调用，两份七模块教案及合法Word下载（39,001/42,266字节）。一键1,008条持久事件；游标6断开时仍running且正式版本0，PG离线序号7→75，重连连续补齐；实际收到Redis changed通知。证据 `.cache/local-real-flow/75a60dbde01c4e219029fc5c7fef393a/evidence.json`。
- 独立PG回读本次合成教师共12个运行全部完成（含额外后台/收尾运行），12条真实调用日志与12条 `applied` 回执一一计数一致，原始响应hash非空且保存时间不晚于应用时间；每个Run唯一终态、事件序列连续。12次连续只读采样与最终回读确认同一调用 `5acc1783-0918-4597-ae81-1c35f1782615` 实际经历 `in_flight → response_saved → applied`。本轮未硬停止Worker，也未恢复DBX/浏览器自动操作；崩溃窗口证明仍以上述已通过专项测试及独立PG事务验证为准。
- 本轮迁移、端口身份、原数据hash、启动健康及最终回执证据在 `.cache/llm-receipts-main-20261020T023254Z-91cd4908/`，最终摘要为 `complete-flow.json`。新增端口桥经general/TypeScript复审及32MiB真实TCP慢读验证，迁移脚本经Python两次复审。保留已有无关修改，未提交、推送或写全局学习记录。

## 追加验收：Langfuse 开源自托管与 /admin 执行日志（2026-10-20）

根据用户的平台修正，使用开源自托管Langfuse，现行方案为[接入方案v2](plans/2026-10-20-langfuse-observability.md)。运行依赖不包含LangSmith、LangChain或LangGraph。独立Compose项目 `lesson-assistant-observability` 包含Web/Worker、PG、ClickHouse、Redis、MinIO；只公开127.0.0.1:3100，未重建或改写已有业务/其他项目容器。Langfuse Web现场版本4.64.0，客户端依赖锁定4.18.0。

| 验证 | 实际结果 |
|---|---|
| 后端全量 | `.venv/Scripts/python.exe -m pytest -q --basetemp .tmp/pytest-langfuse-final`：289 passed、7 skipped，677.85秒；跳过项需显式独立外部环境 |
| 最后修复定向 | observability/core、SDK、exporter、Gateway与新PG测试：39 passed、1 skipped；SDK JSON确认兼容和Gateway未知结果分类在修复前先复现失败 |
| 真实PG并发 | 新建专用 `lesson_recovery_verify_logs_*` 库；移除锁时准确复现“较晚writer越过未提交游标”，恢复实现后cursor/dispatch/invocation合计5 passed（1.10秒）；临时库已清理 |
| 迁移 | 隔离SQLite与PG升级、检查、降至0005、再升级通过；实际业务库备份后0005→0006，`alembic check`无漂移 |
| 前端 | `npm test -- --run`：7文件66项通过；`npm run build`和TypeScript通过，保留既有Lesson大chunk提示 |
| 静态验证 | `ruff check app tests scripts`、`git diff --check`、37份受控资产、业务与Langfuse Compose quiet检查通过 |
| 自托管真实canary | `scripts/check_langfuse_flow.py --live`通过OTLP发送并通过v2 Observations API读回3个观察，两个子节点parent ID一致；零模型请求 |
| 完整合成业务链路 | 独立9011 SQLite/mock服务，Run `546ac9bd-4cc0-491b-935d-c85e20b8f40e`最终completed；40条本地步骤日志、20个Langfuse观察，远端根状态completed，零GENERATION/真实Provider请求 |
| 浏览器 | 验证/admin入口、空日志、按Run与completed筛选、增量分页、详情、Langfuse 32hex复制、恢复深链接自动打开对应任务；验收截图 `.cache/observability-admin.jpg` |
| 本机运行 | 优雅重启本项目API与七类Worker，`/health`200 healthy，`/admin/observability`已启用且configured，本地writer运行正常，管理员日志API200，未登录401 |

Langfuse返回的JSON入队确认在HTTP200下有效，原先只解析protobuf会错误重试；现在两种格式均支持，仍检查部分拒绝、响应大小和固定超时。Web显式 `HOSTNAME=0.0.0.0`，解决仅绑定容器hostname时回环健康检查拒绝的问题；宿主暴露仍是127.0.0.1。日志根步骤结束时以已提交Run终态判断成功，无法确认则interrupted；质量结果、传输结果、已知/未知或部分token与重放分开记录。PG日志写入的500ms事务锁仅作用诊断事务，用于保证sequence顺序与提交顺序一致。

性能开关对照为进程内微基准：相同1ms合成工作，每轮200个执行、5轮中位数，保留相同本地元数据sink；远端关闭1.655ms/执行、远端开启真实OTLP编码+MockTransport确认1.675ms/执行，增量0.020ms。它不包含业务数据库、外部网络、真实模型，不是端到端容量或生产时延证明。安全证据 `.cache/observability-benchmark.json`。

本机私有初始化配置为 `.cache/langfuse.env` 和 `.cache/langfuse-app.env`，均被git忽略且未打印密钥。没有改写用户 `.env`；当前API/全部Worker通过进程环境加载后者。后续重启需按[部署说明](Langfuse自托管部署.md)加载相同配置，或由用户补入 `.env`。业务备份 `.cache/backups/lesson-assistant-before-0006-20261020T093809Z.dump`（2,113,064字节，SHA256 `709e53eddb6a4a47d3d77cd39ee49fe7b2e2608e538536a361901a847132182d`）包含私有业务数据，只保存在忽略目录。初始管理员密码只在私有初始化配置中。

纯合成远端证据 `.cache/langfuse-verification.json`；完整mock链路 `.cache/observability-ui-verification.json`；本机运行只读回查 `.cache/observability-runtime-verification.json`；真实PG RED/GREEN与迁移 `.cache/execution-log-pg-20261020/evidence.json`。Python/TypeScript/安全/general独立审查及最后定向复审无剩余可行动P1/P2。临时浏览器CLI在此Windows环境守护进程启动失败，改用内置浏览器完成实际页面验收；没有把CLI失败当成产品故障。

本轮未重新调用收费模型、核对Provider账单或验证生产容量；真实用量、模型字段、首个非空delta及层级的SDK编码由实际锁定依赖与MockTransport契约验证。已有任务不补传；内存队列在硬崩溃时可能丢诊断。计数是当前API进程状态，不是Worker全局聚合。本地日志暂未自动清理，生产数据保留与备份策略需另行设置。原有无关修改保留，未提交或推送。

## 追加验收：结构化模块导航与单任务互斥（2026-10-20）

修复结构化目录可跳到未解锁后序模块、旧URL终态Run覆盖当前活动Run、快速连点重复提交，以及润色会话在另一个模块生成中仍可创建的问题。目录按确认顺序解锁，已完整生成的内容可回看；任务期间旧模块只读，前序重生待确认时下游历史也可只读回看。恢复的润色候选保留，但只读或执行中不能采用。后端在Draft锁下拒绝活动任务期间新开润色，会话消息/重新生成沿用既有create_run互斥；confirm与step共享前序确认门，幂等重放保留。

- 前端回归先复现5项失败，后补历史回看/恢复候选两项失败再修复；最终新增导航测试10项，前端全量8文件76项通过。TypeScript与生产构建通过，保留既有Lesson chunk体积提示。
- 后端新增准入回归3项（queued/running两个参数场景及顺序确认）先红后绿。组合 `tests/test_workflows.py tests/test_platform.py tests/test_module_admission.py` 为14 passed（130.53秒），涵盖原结构化活动、大纲确认、版本/约束、已有会话可读、同键重放和任务结束恢复。Ruff与diff检查通过；没有改数据库schema或重新执行全后端长套件。
- 独立9012 SQLite/mock浏览器验收使用合成教师、三模块教案 `0c2e4469-8464-4976-83ab-a92ee9e1e92b`。首模块未确认时后两项disabled；确认后仅解锁第二项；第二项运行中回到首模块，实际 `contenteditable=false`、重生/润色禁用、第三项仍锁定。直连HTTP重生和润色均409 RUN_ACTIVE，数据库该教案恰有1个活动任务。释放有截止时间的合成暂停后第二项completed，首模块版本保持，活动任务为0，编辑器与重生/润色恢复。零真实Provider调用。
- 证据 `.cache/module-navigation-evidence.json`，截图 `.cache/module-navigation-busy.jpg`、`.cache/module-navigation-busy-footer.jpg` 和 `.cache/module-navigation-completed.jpg`。general、Python、TypeScript独立复审及历史候选门控定向复审无剩余P1/P2。

本轮只更改模块交互/API准入，不改变模型请求协议、预算、断点恢复或SSE事件契约；无schema迁移、真实模型费用或生产容量验收。原有业务教案与账号不作为故障注入对象。临时验收环境完成后关闭，本机正式服务使用现有私有配置平滑重启。

### 同日补充：已确认模块回看后继续

用户截图指出第二模块已确认时，“确认，继续下一步”仍被禁用。新增两项回归及生成中继续按钮断言，先复现3项失败，再修复已确认分支：显示“继续下一步”，仅进入下一个启用模块，不重复POST确认；最后一个已确认模块明确显示终态。未确认、大纲确认、脏编辑、顺序门禁和任务互斥保持原规则。

最终前端8文件78项、TypeScript、生产构建和diff检查通过；general/TypeScript只读复审无P1/P2。隔离9012实际页面确认第二模块后刷新、回看第二模块，再点击继续，成功进入第三模块；只读数据库对比确认content_version=5、2个Run、2个模块版本及完整状态/版本指针均不变。没有真实模型调用。证据 `.cache/module-confirmed-evidence.json`，截图 `.cache/module-confirmed-continue.jpg`、`.cache/module-confirmed-next.jpg`。

检查正式服务时发现旧本地启动组已退出，当前仅有19:45启动的独立Uvicorn API、无Worker；当时0活动Run。保留API进程31032，通过既有 `run_local.py --child` 补启七类Worker，私有进程记录 `.cache/local-runtime/worker-repair.json`；全部健康且队列深度0。未修改 `.env`、数据库schema或用户教案。本次仅前端重新构建，8000已返回新版资源入口。

### 同日补充：进入新模块自动生成

进入已解锁、无正式正文且仍pending的结构化模块时自动调用既有step入口；确认后继续、回看已确认模块再继续、目录进入均适用。沿用同步AI请求门控和前序确认限制，按教案/模块记录一次自动尝试；启动中显示进度，失败后保留显式重试入口。已有版本、脏编辑和润色候选不自动覆盖，教学过程只自动生成outline。

新增自动启动回归先复现6项失败；复审进一步按真实接口补出失败/取消教案从历史重新打开，以及active_run_id缺失时终态早于正文读回的重复生成风险，均先红后绿关闭。最终导航29项、前端全量8文件95项、TypeScript、生产构建及diff检查通过；general/TypeScript定向复审无剩余P1/P2。没有后端协议、schema或依赖变更，未重复执行后端长套件。

独立9012 SQLite/mock页面只点击第二模块的“继续下一步”，第三模块自动从“正在启动模块生成”运行至待确认正文。Run `ee9aef1c-7965-4b1e-8d22-1edc2e5afe47` completed；刷新并回看后数据库仍恰好只增加1个Run和1个模块版本，前两模块版本指针保留。零真实Provider调用。证据 `.cache/module-auto-evidence.json`，截图 `.cache/module-auto-generated.jpg`。8000已返回最终新版资源入口，API/七类Worker均healthy；临时页面与mock服务验收后关闭。

## 追加验收：Langfuse full 输入输出采集（2026-10-20）

按用户明确要求启用 `OBS_CAPTURE_MODE=full`。`llm.request` 采集实际 Provider 消息与成功接收的模型原文；业务根节点输出使用该 Run 已提交的版本和快照，后续编辑不会改写历史结果。内部步骤按明确的业务投影提供正文或摘要，失败/不完整响应不伪造成功输出，回执重放不新增真实调用或 token。内容只进入远端已结束 observation，本地日志和 `/admin` 保持元数据。

- 内容采集先完成 RED/GREEN 回归，再经 general、Python、安全独立审查。修复审查发现的认证/Cookie头、敏感 URL query、短密钥前缀遮蔽及正则性能问题；加入幂等脱敏、循环/深度/节点限制和每字段默认64KiB UTF-8 JSON上限，超长内容保留预览与截断标记。没有调用任意对象的字符串转换或遍历方法。
- 最终 `test_observability_content/capture/full_flow/exporter/sdk/gateway` 与 `test_gateway_receipts` 七个文件共97 passed（71.55秒），覆盖真实锁定SDK的OTLP字段、实际MockTransport消息/原始响应、metadata隔离、回执重放、已提交根结果及诊断失败隔离。Ruff与diff检查通过，无未关闭P1/P2。
- 真实自托管 full canary 经v2 API读回3个观察，Trace `3d9755818bec5b4e94082a7732ab7a46` 的根/子输入输出及凭证探针脱敏通过；另以metadata覆盖运行同一canary，确认不发送输入输出。两次均零Provider请求，证据 `.cache/langfuse-full-verification-final.json` 和 `.cache/langfuse-metadata-verification-final.json`。
- 实际登录Langfuse，打开full合成节点详情，页面可见中文Input与Output，截图 `.cache/langfuse-full-input-output.jpg`。模型请求的真实消息与原始回复由上述MockTransport契约测试验证，本轮未再次调用收费模型。
- 确认无活动Run后，使用原有本地启动器平滑重启本项目API和七类Worker，实例 `574d0b09294444c19f64a594a7e06006`。管理员API确认capture_mode=full、configured/enabled/running=true；最终 `/health` 为healthy且七队列深度0。私有 `.cache/langfuse-app.env` 已同步full，用户 `.env` SHA256保持不变；证据 `.cache/langfuse-full-runtime.json`。
- 额外后端全量回归在知识库用例长时间没有推进后中止，未获得完整结果，不报告全套通过。该用例单独复查1 passed（59.20秒），证据 `.cache/full-capture-isolated-knowledge.xml`；全量停顿原因尚未确认。专项最终结果见 `.cache/full-capture-focused.xml`。

本轮无schema或依赖变更；旧metadata记录不会自动补齐正文，没有实际输入输出语义的内部节点仍可为空。脱敏不等于移除所有业务隐私，full会向本机Langfuse发送选定的业务内容。默认配置仍为metadata；当前实例已显式启用full，后续重启需依照[部署说明](Langfuse自托管部署.md)加载同一私有配置。

## 未验证与实施取舍

- 已执行真实Provider最小完整canary；尚未系统评估教学质量、成本分布与时延分布，也未验证COS网络读写。Gateway主备故障情形使用受控MockTransport验证。
- 没有E5生产部署、真实容量或系统跨进程压力测试；早期E3七Worker在同一Python进程，后续已验证API与Worker分离和实际Worker kill/restart后的模块续跑。这不等于生产容量承诺。
- 本轮Docker PG/Redis真实运行通过，应用API/Worker仍在Windows宿主机运行；未构建或发布应用镜像。早期临时原生PG/Redis已安全停止；没有安装系统服务。
- SQLite仅便携模式；生产fail-fast要求PG+ARQ+真实模型+强JWT。/health的inline模式Redis不适用，不能当真实Redis证据。
- KaTeX由原文0.16.22升至修复官方漏洞的0.18.2，仍限制原生核心命令并清洗最终HTML。DOCX缩略图是明确标识的解析预览；复杂公式保留源码与告警，扫描PDF及旧.doc明确拒绝。
- 物理投影取舍见[数据库契约](后端数据库契约与实现对照V1.md)。用户两份原稿原样保留；未提交、推送、写入学习库或发布外部状态。
