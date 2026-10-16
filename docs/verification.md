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

## 最终本地运行

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

## 未验证与实施取舍

- 已执行真实Provider最小完整canary；尚未系统评估教学质量、成本分布与时延分布，也未验证COS网络读写。Gateway主备故障情形使用受控MockTransport验证。
- 没有E5生产部署、真实容量或系统跨进程压力测试；早期E3七Worker在同一Python进程，后续已验证API与Worker分离和实际Worker kill/restart后的模块续跑。这不等于生产容量承诺。
- 本轮Docker PG/Redis真实运行通过，应用API/Worker仍在Windows宿主机运行；未构建或发布应用镜像。早期临时原生PG/Redis已安全停止；没有安装系统服务。
- SQLite仅便携模式；生产fail-fast要求PG+ARQ+真实模型+强JWT。/health的inline模式Redis不适用，不能当真实Redis证据。
- KaTeX由原文0.16.22升至修复官方漏洞的0.18.2，仍限制原生核心命令并清洗最终HTML。DOCX缩略图是明确标识的解析预览；复杂公式保留源码与告警，扫描PDF及旧.doc明确拒绝。
- 物理投影取舍见[数据库契约](后端数据库契约与实现对照V1.md)。用户两份原稿原样保留；未提交、推送、写入学习库或发布外部状态。
