# 恢复调度、管理与告警

## Think

用户目标：继续补齐恢复能力，优先完成首次入队失败自动重试、任务恢复管理页面与告警；本地真实流程验证，并在 DBX 观察实时数据变化。

route: architectural-like。变更横跨持久任务调度、管理 API、管理员 UI、迁移及故障演示，需要冻结共享接口与并发约束。

产品边界：保持 PostgreSQL 为任务事实源、Redis/ARQ 为投递执行层；管理员查看恢复详情、有限重试/取消与告警处理，教师无全局管理权限。告警采用持久化站内记录与结构化日志；外部通知目的地未配置，不引入外部发送。保留用户数据、配置和无关工作树修改，不提交或推送，不写全局学习记录。

成功标准：
- AC-1: WHEN 首次或恢复投递失败 THE SYSTEM SHALL 持久化尝试次数、受控错误、下一次重试时间并按有界退避补投；到达尝试或排队期限上限后只收敛一次失败终态，重启不重置预算，取消不复活。
- AC-2: WHEN 管理员查看或操作恢复任务 THE SYSTEM SHALL 提供分页筛选、投递/租约/断点/调用预算与事件历史，只有符合条件的任务可重试或取消；操作有权限、并发与审计保护，正常有效执行者不得被强抢。
- AC-3: WHEN 投递失败、执行租约过期、恢复耗尽或排队积压 THE SYSTEM SHALL 持久化可去重告警并显示状态、次数与时间，管理员可确认；条件消失后自动恢复关闭，保留历史事实与审计。
- AC-4: WHEN 演示故障恢复 THE SYSTEM SHALL 在独立本地测试和真实 PG/Redis/模型流程中验证首次失败自动重投、管理页面、告警生命周期及 Worker 硬停止续跑，在 DBX 留存对应数据变化，最后恢复正常服务。

风险：外部模型在途请求仍可能重复计费；管理重试不能静默重置旧 Run 的执行/质量预算；调度及告警扫描需要防止并发重复和队首饥饿；真实迁移前备份并停止旧进程；故障注入仅限合成演示请求，不影响其他任务。

<!-- design-authority:start -->
## Design Authority（shadow，revision 1）

- 选择保留 ARQ/PG 执行权机制。初次失败与恢复失败统一为持久化投递重试；复用 Outbox.available_at 作为下一次到期时间，补充 last_attempt_at、last_error_code、last_error_at、dispatch_deadline_at。尝试次数在外部投递前提交；数据库条件领取到期意图，稳定 Job ID 处理未知入队结果；超时领取可恢复。总投递期限从首次意图建立起累计，恢复不重置任务累计执行预算。
- 默认配置：dispatch_max_attempts=6、dispatch_retry_base_seconds=2、dispatch_retry_max_seconds=60、dispatch_timeout_seconds=5、dispatch_deadline_seconds=300、queue_backlog_seconds=60；指数退避带有界抖动。现有 published 意图仍需对账 Redis Job 状态，扫描采用到期排序，避免前100个已发布排队记录阻塞新意图。
- 新 RecoveryAlert 表：recovery_alerts；id/created_at，dedupe_key(unique)，run_id(nullable FK)，code，severity，message，status(open/acknowledged/resolved)，occurrence_count，first_seen_at/last_seen_at，acknowledged_at/acknowledged_by，resolved_at，metadata_json。站内告警与安全结构化日志；原始异常、DSN、模型密钥和教师正文不得输出。
- 告警信号：DISPATCH_RETRY、DISPATCH_EXHAUSTED、WORKER_LEASE_EXPIRED、WORKER_RECOVERY_EXHAUSTED、QUEUE_BACKLOG。投递恢复后关闭投递失败告警，任务接管恢复后关闭租约告警；耗尽保留待处置，管理员可确认/关闭并审计。周期扫描队列年龄并保留去重历史。并发首次触发需要数据库唯一性保护。
- 新后端服务 app/services/recovery.py 提供 signal_alert(session,code,message,run=None,severity='warning',metadata=None,dedupe_key=None)、resolve_alerts(session,run_id,codes=None)、scan_alerts(app)。如实现需精确调整签名，由 owner 在 Work 开始时先通知 root，API消费者随之适配，不偷偷产生两种契约。
- 管理 API（统一 data 包装）：GET /admin/recovery-runs?status=&search=&offset=0&limit=20&attention_only=false 返回 {items,total,offset,limit,server_time}；summary={id,draft_id,topic,user_id,workflow_type,status,error_code,execution_epoch,recovery_count,heartbeat_at,lease_expires_at,created_at,started_at,finished_at,checkpoint_count,dispatch,can_retry,can_cancel}；dispatch={id,status,attempts,next_attempt_at,last_attempt_at,last_error_code,deadline_at} 或 null。
- GET /admin/recovery-runs/{id}?after=0&limit=50 返回 {run:summary,checkpoints,calls,events,event_total,alerts}；checkpoint={id,key,created_at,input_fingerprint,content_hash}，call仅暴露id/model_name/provider/status/created_at/call_id/call_outcome/validation_outcome/validation_codes/execution_epoch；事件仅暴露sequence/event_type/created_at和受控phase/module_id/checkpoint_key/attempt/max_attempts/execution_epoch/code，不返正文。
- POST /admin/recovery-runs/{id}/retry body={expected_epoch,expected_attempts,reason}；只允许 queued 的待重投任务或租约过期且预算未耗尽任务。不能抢占有效租约，不能复活终态，不能重置预算。返回更新后 summary；状态过时409。已结束任务从教案页发起新生成。POST .../cancel body={expected_epoch,reason} 对活动任务取消并保证旧owner无后续写入。两类操作均审计。
- GET /admin/recovery-alerts?status=&offset=0&limit=20 返回 {items,total,offset,limit}；alert=上表字段（metadata仅受控键）；POST /admin/recovery-alerts/{id}/acknowledge 和 /resolve body={reason}，记录操作者和审计。所有管理接口复用 admin_user，教师403。
- GET /admin/recovery-overview 返回 {queued,running,retry_pending,expired_leases,open_alerts,failed}。页面 /admin/recovery 独立于旧通用管理表格，增加管理入口，5秒轮询可关闭，筛选/分页/详情/操作/确认/关闭告警；进行中请求清理与过时响应保护。生成页识别 dispatch_retry_scheduled/dispatch_published 阶段。
- 数据变更为加列/加表，真实迁移前先备份本地 lesson_assistant 并停旧进程，禁止对 rag_system 写入或删除历史数据；恢复采用旧制品+前向修复，不自动降级删表。
- 验收语义由 AC-1..AC-4 定义；本地真实演示使用合成账号、公开资料和真实模型，故障注入限定该演示任务。独立管理员演示账号仅用于本地验收，保留正常账户权限。
<!-- design-authority:end -->

<!-- acceptance-contract:start -->
- [ ] AC-1: WHEN 首次或恢复投递失败 THE SYSTEM SHALL 持久化尝试次数、受控错误、下一次重试时间并按有界退避补投；到达尝试或排队期限上限后只收敛一次失败终态，重启不重置预算，取消不复活。
- [ ] AC-2: WHEN 管理员查看或操作恢复任务 THE SYSTEM SHALL 提供分页筛选、投递/租约/断点/调用预算与事件历史，只有符合条件的任务可重试或取消；操作有权限、并发与审计保护，正常有效执行者不得被强抢。
- [ ] AC-3: WHEN 投递失败、执行租约过期、恢复耗尽或排队积压 THE SYSTEM SHALL 持久化可去重告警并显示状态、次数与时间，管理员可确认；条件消失后自动恢复关闭，保留历史事实与审计。
- [ ] AC-4: WHEN 演示故障恢复 THE SYSTEM SHALL 在独立本地测试和真实 PG/Redis/模型流程中验证首次失败自动重投、管理页面、告警生命周期及 Worker 硬停止续跑，在 DBX 留存对应数据变化，最后恢复正常服务。
<!-- acceptance-contract:end -->

## 文件所有权与验证

| 任务 | Owner / 文件 | 依赖、风险 | 证据 |
| --- | --- | --- | --- |
| T1 调度与告警 | backend: app/tasks/queue.py、app/tasks/workers.py、app/core/config.py、app/db/models.py、migrations/versions/0004*、app/services/recovery.py、对应新测试 | 契约冻结；L4，先失败测试后实现 | 退避/崩溃窗口/取消/并发/耗尽/告警去重回归 |
| T2 管理 API | root: app/api/admin.py、新管理 API 测试 | T1模型与服务；L3 | 鉴权、白名单、CAS、审计、分页及管理动作 |
| T3 前端 [P] | frontend: frontend/src/Recovery.tsx、recovery.test.tsx、App.tsx、Pages.tsx、types.ts、styles.css、Lesson.tsx | 冻结API独立fixture；L2，不编辑Editor | 页面交互、空/错误/过时请求、重试/取消与告警测试，build |
| T4 集成与真实演示 | root: 文档、.cache独立演示工具，必要启动脚本修正另审 | T1..T3、备份与审查通过；L4 | 全回归、真实PG/Redis/模型、浏览器与DBX截图、正常运行恢复 |

Plan inline核对：无占位接口；文件所有权不重叠；所有高风险操作在备份/无其他活动任务/代码审查后执行；已有管理员鉴权复用，外部通知不发送。普通教学资料与历史任务保留。
