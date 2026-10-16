---
type: sprint
status: completed
---

# 知识库、检索、质量重试与模块断点恢复

用户要求：继续完整执行，补齐独立知识库管理、知识检索、通用质量失败重试、Worker 崩溃后的模块级断点续跑。

## Think 验收

- route: `architectural-like`。新增知识子系统，并改变生成任务恢复、事务与并发所有权。
- 做：教师私有知识库/文档管理与检索界面、文件解析分块、带出处的自动检索上下文；通用有界质量/格式重试；持久蓝图/模块/活动 checkpoint；带租约和 fencing 的崩溃恢复与幂等交付；现有两种生成模式及 SSE 保持可用。
- 可撤销默认：优先实现无额外模型凭证依赖的中文/英文关键词相关性检索；不将现有个人风格库冒充知识库。未配置 embedding 服务时不声称向量检索。知识库按教师私有隔离，不扩展公开共享或远程网页抓取。
- 数据边界：保留已有 PostgreSQL 数据、账号、教案与文件；只做向前兼容迁移。沿用本地 Docker PG/Redis 和已授权真实模型配置；真实调用仅使用合成材料。无提交、推送、生产发布或自动学习写入。

可观察成功标准：

1. WHEN 教师维护知识库 THE SYSTEM SHALL 支持独立创建/编辑/删除知识库、上传/查看/删除文档、解析状态与可检索片段，阻止越权访问。
2. WHEN 教案生成需要知识 THE SYSTEM SHALL 从当前教师选定知识库检索相关片段，冻结本次引用与出处并传入模型；无匹配清晰返回空结果，材料作为数据而非系统指令。
3. WHEN 生成内容未通过格式或业务质量校验 THE SYSTEM SHALL 在明确次数/总调用/时间预算内携带受控反馈重试，清除废弃流片段，保留所有调用与失败事实，预算耗尽后返回明确失败。
4. WHEN Worker 在模块或课堂环节完成后崩溃 THE SYSTEM SHALL 复用已持久化且指纹一致的 checkpoint，仅生成未完成部分；租约失效后的旧 Worker 不得写事件、版本或终态。
5. WHEN 重投、恢复、取消或全部完成 THE SYSTEM SHALL 保持唯一正式版本/终态、连续 SSE 重放及任务幂等，并以自动回归和真实独立进程 kill/restart 演示证明。

风险：租约误判与僵尸写入、单次整课流如何及时 checkpoint、引用文件删除后的冻结语义、重试对调用预算和流式 UI 的影响。Plan 阶段冻结接口与写路径约束后实施。

<!-- design-authority:start -->
## Design Authority（shadow，revision 1）

- 选择：保留 FastAPI/PG/Redis/ARQ 与现有 Runtime，不引入新的调度框架或 embedding 服务。独立知识库使用中文双字词/英文词 BM25 相关性检索；返回真实片段与定位，不宣称向量召回。知识库内容冻结入草稿，避免删除/更新改变已创建教案与恢复上下文。
- 知识接口：`/knowledge-bases` GET/POST；`/knowledge-bases/{id}` PATCH/DELETE；`/knowledge-bases/{id}/documents` GET/POST multipart；`/knowledge-documents/{id}` GET/DELETE；`/knowledge-documents/{id}/chunks` GET（offset/limit，items/total）；`/knowledge-bases/{id}/search` POST（query/top_k，items/method）；失败文档可 POST `/knowledge-documents/{id}/reindex`。上传返回文档与 run_id，经 thumbnail 队列异步解析。
- 片段投影：id/document_id/base_id/filename/base_name/ordinal/text/content_hash/score；知识库 title/description；文档 status(uploaded/processing/ready/failed)、filename/chunk_count/error_message。统一现有 data 包装、404所有权、上传前认证与字节预算。
- 生成输入：`knowledge_base_ids: list[str] | null`，null 默认全部当前教师知识库、[]明确停用；草稿保存解析后的 IDs 和 `knowledge_context_snapshot`。快照只包含选中且ready文档的有界相关片段，供Runtime作为不可信参考材料使用；生成结果可显示来源。
- Runtime接口：`generate(..., checkpoint_sink=None, resume=None)`；`checkpoint_sink(key, payload)`，resume为checkpoint key→payload。keys为blueprint、module:<module_id>、activity:<module_id>:<stage_id>、budget；payload含value、calls、calls_started、attempt_counts。Service验输入/配置/依赖/大纲/知识/资产指纹，Runtime再次验完整内容。正常一键仍1次调用；只有未完成模块重生，闭合且通过质量门的模块立即checkpoint。
- 通用重试：默认每单元最多额外1次（可配0..3），涵盖蓝图/模块/大纲/单活动/润色的格式、schema与业务硬门错误；修正反馈使用受控错误码，不发送异常或危险原文。网络主备由Gateway负责，不做无差别重试。次数、调用账本和已开始调用预算跨恢复累计。
- 重试/恢复事件：沿用module_progress，phase=quality_retry_started（module_id,attempt,max_attempts）、generation_attempt_reset（scope=module或all，module_id）、worker_recovering、checkpoint_restored。恢复正文通过content_delta replace_previous=true回放；不新增侧栏确认状态文案。
- Run执行权：epoch+owner+数据库时钟租约，心跳独立Session。所有Worker写事务保持Draft→Run锁序并验证epoch/owner/status/有效租约；过期心跳不能复活。恢复将同Run原子切为新epoch queued，保留checkpoints，按epoch生成新ARQ job ID并Outbox投递，不删除旧Redis job keys。取消/超时/预算耗尽收口唯一终态。
- 文件解析：knowledge_document_parse在thumbnail队列、Run可无draft。服务使用同一execution fence提交片段/状态。上传文件与删除补偿沿用storage/Outbox；检索仅ready且未删除的私有内容。
- 恢复边界：只添加表/列，迁移前备份PG与停止旧版本进程；不降级删除新表。崩溃中的外部模型调用可能重发，不承诺模型调用恰好一次；正式版本与终态必须恰好一次。本地启动器可重启崩溃Worker，API退出则停止整组。
- Acceptance Criteria：
  - AC-1: WHEN 教师维护知识库 THE SYSTEM SHALL 支持独立创建/编辑/删除知识库、上传/查看/删除文档、解析状态与可检索片段，阻止越权访问。
  - AC-2: WHEN 教案生成需要知识 THE SYSTEM SHALL 从当前教师选定知识库检索相关片段，冻结本次引用与出处并传入模型；无匹配清晰返回空结果，材料作为数据而非系统指令。
  - AC-3: WHEN 生成内容未通过格式或业务质量校验 THE SYSTEM SHALL 在明确次数/总调用/时间预算内携带受控反馈重试，清除废弃流片段，保留所有调用与失败事实，预算耗尽后返回明确失败。
  - AC-4: WHEN Worker 在模块或课堂环节完成后崩溃 THE SYSTEM SHALL 复用已持久化且指纹一致的 checkpoint，仅生成未完成部分；租约失效后的旧 Worker 不得写事件、版本或终态。
  - AC-5: WHEN 重投、恢复、取消或全部完成 THE SYSTEM SHALL 保持唯一正式版本/终态、连续 SSE 重放及任务幂等，并以自动回归和真实独立进程 kill/restart 演示证明。
<!-- design-authority:end -->

<!-- acceptance-contract:start -->
- [x] AC-1: WHEN 教师维护知识库 THE SYSTEM SHALL 支持独立创建/编辑/删除知识库、上传/查看/删除文档、解析状态与可检索片段，阻止越权访问。
- [x] AC-2: WHEN 教案生成需要知识 THE SYSTEM SHALL 从当前教师选定知识库检索相关片段，冻结本次引用与出处并传入模型；无匹配清晰返回空结果，材料作为数据而非系统指令。
- [x] AC-3: WHEN 生成内容未通过格式或业务质量校验 THE SYSTEM SHALL 在明确次数/总调用/时间预算内携带受控反馈重试，清除废弃流片段，保留所有调用与失败事实，预算耗尽后返回明确失败。
- [x] AC-4: WHEN Worker 在模块或课堂环节完成后崩溃 THE SYSTEM SHALL 复用已持久化且指纹一致的 checkpoint，仅生成未完成部分；租约失效后的旧 Worker 不得写事件、版本或终态。
- [x] AC-5: WHEN 重投、恢复、取消或全部完成 THE SYSTEM SHALL 保持唯一正式版本/终态、连续 SSE 重放及任务幂等，并以自动回归和真实独立进程 kill/restart 演示证明。
<!-- acceptance-contract:end -->

## 契约投影与任务

| 投影 | before | after | 消费者/一致性验证 |
|---|---|---|---|
| PG/Alembic | 0002，无知识/执行权列 | 0003，独立知识表、RunCheckpoint、Run租约、Draft知识快照 | ORM/API/Worker，迁移与隔离回归 |
| Runtime | generate一次返回、无恢复参数 | 可选checkpoint_sink/resume、有界单元重试 | Service、受控Gateway/流式测试 |
| 队列 | 固定job ID、过期直接失败 | 每epoch稳定ID、租约恢复+Outbox | ARQ CLI、本地启动器、硬kill演示 |
| SSE/UI | 全局reset与固定phase | 作用域reset、重试/恢复进度、引用 | React测试、TCP事件回放 |

| 任务 | 文件所有权 | 依赖/风险 | 验收 |
|---|---|---|---|
| T1 数据与契约 | root: models.py、config.py、0003、资产manifest | 先执行；L3，备份后才迁移实际PG | AC-1/4，schema/回归 |
| T2 Runtime重试与checkpoint [P] | runtime: app/ai/runtime.py与新增test_runtime_recovery.py | T1接口冻结；L2纯工作流，独立文件 | AC-3/4，先红后绿、旧runtime回归 |
| T3 知识子系统 | root: api/knowledge.py、services/knowledge.py、api/lessons.py、factory、上传边界、知识tests | T1；L3鉴权/文件/检索 | AC-1/2，越权/解析/冻结/空结果/引用 |
| T4 执行租约与恢复 | platform: tasks/queue.py、workers.py、services/lessons.py、新recovery测试；root: run_local.py/实际演示 | T1→T2接口；L4，全部写路径fence审计通过后实际验证 | AC-4/5，并发/取消/预算/进程kill |
| T5 教师UI [P] | frontend: App/Pages/Lesson/types/styles及Knowledge页面、前端tests | Plan API冻结；L2，独立文件，不改Editor语义 | AC-1/2/3/5，渲染/交互/作用域reset |

Plan inline核对：无未定义占位符；所有权不相交（models/config由root先完成）；认证与fence高风险有明确测试/备份gate；无生产或外部发布。后续revision需显式记录。

## Revision 2：实施中的一致性收敛

- 已存在checkpoint但输入/依赖/受控资产指纹不一致时，受控失败 `CHECKPOINT_CONTEXT_CHANGED`；不静默丢弃账本再获得新的调用/重试预算。无checkpoint的新任务仍按正常流程执行。
- PostgreSQL租约用 `clock_timestamp()`，避免事务等待行锁期间将旧的事务开始时间误作当前时间；SQLite用数据库毫秒时间。总运行预算从首次started_at累计。
- UI仅把完整模块checkpoint计为模块完成；恢复单个activity只更新环节进度。后端实际 quality_retry_started→generation_attempt_reset 顺序下，reset仅清废弃正文，保留本轮次数与重试状态。
- 自动回归之外，root将用合成教师、知识资料与真实模型创建任务，在至少两个模块checkpoint落库后核验进程身份再硬停止本地one-click Worker，观察启动器重启、租约恢复、剩余生成、SSE及唯一版本/终态。观察脚本不自行杀进程，操作与身份核验单独留证。

## Work 实施与并发修正

- T1..T5已实现，实际PG已备份并升级0003；原20张业务表的原列投影行数/hash不变。私有知识解析、真实检索/引用、重试账本、epoch/租约/持久断点及UI联动均已接入。
- 真实PG验证补齐SQLite不能证明的两类锁竞态：知识库父表改NO KEY UPDATE避免与解析子表FK互锁；心跳改PG先SELECT Run FOR UPDATE再读时钟、只执行一次实际UPDATE，避免旧双UPDATE触发父Draft FK回查与Draft→Run writer反转。确定性交错原实现40P01、修复正常完成，并有旧机制对照；专用临时库均清理。
- 最终代码硬停止恢复通过：`.cache/worker-recovery/fe9e9d89f7db4c1ba469068cb1c7a95b/evidence.json`，2个已验证模块复用、7个正式版本、epoch1/recovery1、1567连续SSE与唯一成功终态。
- 独立审查覆盖通用代码、Python、TypeScript及安全；最终锁修复另经两位定向复核通过。完整回归和两模式真实流程的最终结果在Review前记录。
- 完整后端157 passed/3 opt-in skipped（267.71秒），其中新增2项PG对照已在专用真实PG单独通过；前端34/34、tsc、Ruff、37资产、Compose/diff检查均通过，源码证据索引141文件/head0003。

## Review 验收结论

未发现可操作的未关闭问题。两位审查者定向复核最终PG心跳锁修复；此前Python/TypeScript/安全审查的finding均已闭合。所有实际测试与过程证据见 `docs/verification.md`，未将agent自报替代root执行结果。

| AC | 最终证据 |
|---|---|
| AC-1 | 私有知识CRUD/解析/越权/删除竞态回归；浏览器检索截图与真实thumbnail解析 |
| AC-2 | 不可信材料/空检索/来源冻结回归；真实恢复canary知识来源快照 |
| AC-3 | Runtime重试/预算/回调/活动恢复测试；Service持久账本与候选恢复测试 |
| AC-4 | epoch/租约/晚到写入/输入指纹回归；实际硬停止后2个模块ID/hash复用 |
| AC-5 | 157项完整回归、2项真实PG并发；硬停止恢复1567连续事件及7版本/唯一终态；最终真实两模式11个Run、12次调用、两份Word全部通过 |

最终真实完整流程证据 `.cache/local-real-flow/38ec4d5b37fb455fa1a269ae24753133/evidence.json`；健康检查为healthy、PG/Redis/七Worker均正常。保持既有数据、模型配置和用户设计文档。没有生产容量或COS实测结论；外部Provider在途调用的最终费用仍不可由本地恢复保证。

## Compound 收尾

no-op：遵守用户AGENTS中“Do not auto-write learning, run Compound”边界，不创建solution/学习候选，不修改全局记忆或共享runtime。已验证事实、已关闭根因与未知项均已在本计划及本次交付验收文档中记录。Sprint目标和AC-1..5全部完成；没有提交、推送或生产发布。
