---
type: sprint
status: completed
---

# 教案助手完整实现

用户请求：直接参照根目录《教案助手完整技术方案.md》和《教案助手AI Agent开发指南.md》，以 `/sprint --auto` 完整实现整体功能。

## 启动事实

- 2026-10-16：仓库仅有 README 与用户提供的两份设计文档，无现有应用实现。
- 两份用户文档保留原样；文档提及的历史代码基线尚不能视为本仓库事实。
- 启动阶段：Think。按当前可执行宿主推进，未绑定外部验收 Harness；实时阶段以Sprint pointer为准。
- 授权范围：本地代码、资产、文档和测试；未授权生产部署、远程写入、提交或推送。

## 阶段记录

### Think 验收（2026-10-16）

- route: `architectural-like`。理由：从零新建全栈系统，涉及身份、版本、异步任务、AI、文件和租户边界。
- 做：文档 V1 中的教师完整闭环、管理端、受控 AI 工作流、PostgreSQL/Alembic、Redis/ARQ 七类队列、SSE、质量/日志/Outbox、个人教案库、DOCX/PDF、配置和部署工件。
- 非目标遵照原文：知识图谱、长期教师画像、自主工具平台、教师质量评分展示；生产部署和付费真实模型调用不在当前授权内。
- 可撤销假设：补齐中文教师 Web 界面；本地显式 mock Provider 用于开发，生产配置必须真实 Provider 且 fail-fast；本仓库新迁移从 0001 开始，不伪造缺失的 0031 历史。

可观察成功标准：
1. WHEN 教师登录并创建教案 THE SYSTEM SHALL 按冻结模块配置生成，持久化唯一 Run 终态和可重放 SSE；同键异参拒绝、同键同参复用。
2. WHEN 结构化推进、确认、编辑、恢复或润色 THE SYSTEM SHALL 校验版本，隔离候选，冻结已确认依赖，保留教学过程两阶段及下游失效事实。
3. WHEN 预览、导出、上传或保存反思 THE SYSTEM SHALL 使用正式版本与独立领域事实，校验所有权，生成可实际读取的文件。
4. WHEN 取消、队列失败、Provider/fallback/质量失败发生 THE SYSTEM SHALL 收口终态、禁止晚到提交、保留失败事实，不能返回假成功。
5. WHEN 验收 THE SYSTEM SHALL 提供自动化测试、接口与运行文档、可启动 UI、部署模板和逐项证据，明确环境未验证项。

风险与未知：当前未确认 Docker daemon、真实 PG/Redis、模型与 COS 凭证。优先完成不依赖外部账号的实现和确定性验证，真实服务验证单独留证；不据此缩减功能实现。

<!-- design-authority:start -->
## Design Authority（shadow）

- 问题与用户价值：教师通过可追踪、可编辑和可恢复的工作流完成备课与课后反思。
- 范围 / 非目标：遵照 Think。实现全栈 V1；不进行未授权外部发布。
- 方案比较：选择 FastAPI + SQLAlchemy async + PostgreSQL + Alembic + Redis/ARQ + React/TypeScript。避免另起微服务或通用 Agent 框架；SQLite 仅显式用于本地便携模式与确定性测试，不证明 PostgreSQL 证据。
- 组件接口：`app/core` 配置/鉴权/错误，`app/db` ORM/session，`app/api` 协议，`app/services` 事务/状态，`app/ai` Gateway/Runtime/质量，`app/integrations` 文档/存储，`app/tasks` 队列/Worker；`frontend` 教师与管理 UI。
- 状态与错误：数据库 Run 和 Event 是权威；Run 状态 CAS 与唯一终态、模块乐观锁、持久化 dispatch Outbox、稳定 job ID、重复投递、取消晚到提交保护。Candidate 不直接覆盖版本。错误响应统一受控 code/message/request_id。
- 文件出口：快照冻结版本组合，导出异步且校验文件可读；本地存储为默认可运行 Adapter，生产可选择 COS。
- 恢复：所有新建本地文件可移除回滚；数据库只针对显式独立库执行 Alembic。未授权 production migration、账号凭证、付费 Provider 或部署均不执行。
- Acceptance Criteria：
  - AC-1: WHEN 教师登录并创建教案 THE SYSTEM SHALL 按冻结模块配置生成，持久化唯一 Run 终态和可重放 SSE；同键异参拒绝、同键同参复用。
  - AC-2: WHEN 结构化推进、确认、编辑、恢复或润色 THE SYSTEM SHALL 校验版本，隔离候选，冻结已确认依赖，保留教学过程两阶段及下游失效事实。
  - AC-3: WHEN 预览、导出、上传或保存反思 THE SYSTEM SHALL 使用正式版本与独立领域事实，校验所有权，生成可实际读取的文件。
  - AC-4: WHEN 取消、队列失败、Provider/fallback/质量失败发生 THE SYSTEM SHALL 收口终态、禁止晚到提交、保留失败事实，不能返回假成功。
  - AC-5: WHEN 验收 THE SYSTEM SHALL 提供自动化测试、接口与运行文档、可启动 UI、部署模板和逐项证据，明确环境未验证项。
<!-- design-authority:end -->

<!-- acceptance-contract:start -->
- [x] AC-1: WHEN 教师登录并创建教案 THE SYSTEM SHALL 按冻结模块配置生成，持久化唯一 Run 终态和可重放 SSE；同键异参拒绝、同键同参复用。
- [x] AC-2: WHEN 结构化推进、确认、编辑、恢复或润色 THE SYSTEM SHALL 校验版本，隔离候选，冻结已确认依赖，保留教学过程两阶段及下游失效事实。
- [x] AC-3: WHEN 预览、导出、上传或保存反思 THE SYSTEM SHALL 使用正式版本与独立领域事实，校验所有权，生成可实际读取的文件。
- [x] AC-4: WHEN 取消、队列失败、Provider/fallback/质量失败发生 THE SYSTEM SHALL 收口终态、禁止晚到提交、保留失败事实，不能返回假成功。
- [x] AC-5: WHEN 验收 THE SYSTEM SHALL 提供自动化测试、接口与运行文档、可启动 UI、部署模板和逐项证据，明确环境未验证项。
<!-- acceptance-contract:end -->

## 执行任务与依赖

| ID | 目标 / 文件所有权 | 依赖 | 风险 | 完成证据 |
|---|---|---|---|---|
| T1 | Python 工程、配置、ORM、Alembic、认证、字典；`pyproject.toml`, `app/core/`, `app/db/`, `app/api/auth.py`, `app/api/catalog.py`, `migrations/`, `tests/test_auth.py` | 设计冻结 | L3 | AC-1/3/4；鉴权、隔离与迁移测试 |
| T2 | AI/受控资产、解析、质量、导出/上传解析/存储；`app/ai/`, `app/integrations/`, `config/`, `schemas/`, `prompts/`, `policies/`, `scripts/validate_assets.py`, 对应 unit tests | 设计冻结 | L3 | AC-2/3/4；fake Gateway、主备、质量、文件读回 |
| T3 | 中文教师/管理 UI；`frontend/` | 设计冻结 API 契约，联调依赖 T1/T4 | L2 | AC-1..5；typecheck、build、浏览器流程 |
| T4 | 用例事务、生成 API、SSE、ARQ、Outbox、文件与管理 API；`app/services/`, `app/api/lessons.py`, `app/api/admin.py`, `app/tasks/`, `app/factory.py`, `main.py`, `tests/test_workflows.py` | T1/T2 | L3 | AC-1..4；集成/失败/取消/重放/版本测试 |
| T5 | 运行/接口/证据文档、Compose/Docker/env、E2E、验收修复；`README.md`, `docs/`, `deploy/`, `scripts/`, `tests/e2e/` | T1..4 | L3 | AC-5；lint、迁移、pytest、UI 与明确未验证项 |

并行边界：T1/T2/T3 仅按冻结接口分派不同文件所有者，不把 L3 标为 `[P]` 安全认证；跨组件契约和所有权调整由主 agent 串行集成。技能 runtime-portability 明确要求在原生 spawn 可用时分派；独立 code review 在集成后执行。

### 冻结接口（revision 1）

- 所有 JSON API 返回 `{data: ...}`；错误 `{error:{code,message},request_id}`。
- auth：`POST /api/v1/auth/register|login` `{email,password,name?}` -> `{access_token,user}`；`GET/PATCH /me`；Bearer JWT；logout 撤销。
- 创建：`POST /lesson-drafts/generations/one-click|interactive` `{request_id,topic,school_stage,grade,subject,lesson_type,total_duration_min,requirements?,modules?}` -> `{draft_id,run_id}`；header `Idempotency-Key`。
- `GET /lesson-drafts` -> 列表；`GET /lesson-drafts/{id}` -> 草稿、modules（当前正文/状态/版本）、版本号；`DELETE` 软删。
- `POST /lesson-drafts/{id}/interactive/step|confirm|finalize`；step 带 module_id/stage；confirm 带 module_id/expected_content_version/expected_current_module_version_id/expected_module_config_version_id，可带 outline。
- `PATCH /lesson-drafts/{id}/modules/{module_id}` `{teacher_view_content,expected_content_version,expected_current_module_version_id,expected_module_config_version_id}`；GET versions；POST restore。
- `GET /generation-runs/{id}`；`GET /generation-runs/{id}/events` SSE，Bearer；`POST .../cancel`。
- `POST /module-polish-sessions` `{draft_id,module_id,scope,selection_start?,selection_end?}`；`POST .../{id}/messages` `{instruction,working_content}` -> run；GET session -> candidate；采用仍经 PATCH。
- `POST /lesson-drafts/{id}/preview` -> 快照；POST exports `{format:'docx'|'pdf'}` -> export；`GET /lesson-exports/{id}`、`.../download`。
- `GET/PUT /lesson-drafts/{id}/post-lesson-reflection`；POST `.../polish` -> run/candidate，正式保存独立。
- `GET/POST /personal-lessons` multipart；DELETE；原文件 download。
- `GET /lesson-dictionaries|profile-options|me/lesson-module-config`；PUT 模块偏好；POST analytics/events；GET admin/overview|users|runs|issues|audit；PATCH admin/users/{id}。
- API 消费模型唯一从 `app.db.models` 导入；AI 运行时通过数据类接收普通 dict，不拥有 DB 事务。

测试策略：测试先覆盖状态/鉴权/版本/幂等/故障；使用临时 SQLite 文件获得 E1 便携集成反馈，同时提供 PG/Redis opt-in 测试。生产配置校验、依赖真实连通性、文件实际读取与 UI 构建分别验证。Docker CLI 已验证存在，但 daemon 当前不可连接；真实 PG/Redis/E3 暂未知。

Plan inline 检查：无未决占位实现；状态/字段/响应一致；范围遵循原文；外部验证授权边界已明确。无 Harness 绑定，当前宿主验收并进入 Work。

### Work 验收（2026-10-16）

- T1–T5已实现并完成组合验证。Python全量65 passed / 1 opt-in skipped；前端16项测试、TypeScript、生产构建通过，依赖审计0漏洞。37项受控资产校验通过。
- 真实PostgreSQL/Redis上执行七类ARQ Worker、幂等/重放/唯一终态、取消/晚到写入、PubSub故障降级、Job丢失复投、DOCX导出与个人库解析/缩略图/删除补偿。具体版本、计数与环境边界见[验收记录](../verification.md)和[独立运行证据](../external-runtime-evidence.md)。
- 浏览器覆盖教师注册、两种生成方式、43分钟教师确认大纲、模块编辑/版本与润色、预览/实际Word下载、独立反思、DOCX上传与私有缩略图。管理端和租户边界另有自动化测试。
- 已对代码执行独立后端、Python、TypeScript和安全审查，并按失败测试修复已发现的状态/并发/安全问题。最终Review继续核对证据和最后界面修复。
- 生产Provider/COS、容器构建与部署、生产容量和跨进程故障测试未验证；本次交付为完整本地V1实现，不冒充生产验收。用户原稿保留，无commit/push或学习库写入。

### Review 验收（2026-10-16）

- 基线为原始README与两份用户原稿；新建应用代码经后端通用、Python、TypeScript、安全四个独立审查面检查，所有发现均已修复并复核关闭。最后的个人库摘要类型与SSE正常EOF重连埋点回归均已通过。
- 主宿主组合回归65项Python通过/1外部opt-in跳过、16项前端通过；独立真实PG/Redis冻结后端复验通过。Ruff、受控资产、Compose清单、diff check通过，本地最新主页/JS/ready均HTTP 200。
- 审查结论：未发现可操作问题，无未闭合finding。剩余风险仅限[验收记录](../verification.md)明确列出的真实Provider/COS、容器/生产部署及容量验证边界，无外部Harness绑定或待请求Receipt。

### Compound 收尾（2026-10-16）

- 已验证事实：T1–T5与五项AC均完成，测试、独立审查、真实PG/Redis和本地最终应用证据已记录。
- 推断：真实Provider和COS适配器可按配置接入；受控网络测试支持协议正确性，不能推断真实内容质量、成本或生产容量。
- 未知项：真实服务凭证与canary、容器实际构建、生产部署和容量测试；均不属于本次本地实现交付的执行范围。
- 学习沉淀采用no-op：本次架构与修复事实已纳入交付文档；遵循用户写入边界，不新增solution、行为学习候选、规则或共享runtime资产。新增0，更新0；无提交、推送或外部同步。
- Solution index: unchanged 0 entries -> docs/solutions/index.jsonl; Claude projection: unchanged; AGENTS projection: disabled
- Learning candidates: proposed 0; needs-review 0; evaluated 0; shadow 0; approve/promote: not run
- 以上索引路径为未创建的no-op目标；计划和验收文档属于本次授权的项目交付记录。
