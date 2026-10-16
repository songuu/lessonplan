# 部署与发布边界

`compose.yaml`是完整本地开发拓扑，含PG/Redis/API/七Worker/singleton迁移。已验证Compose配置可解析；实际容器构建/启动需可用Docker daemon。默认mock不是生产发布配置。

当前本机开发使用 `compose.local.yaml` 管理独立持久化 Redis，并复用 `rag-nextjs` 的 Docker PG17 实例（独立 `lesson_assistant` 数据库）。API/七 Worker 在宿主机运行，真实模型完整 canary 已通过；配置、启动及数据保留步骤见[本地 Docker 流程](../docs/本地Docker完整流程.md)。此拓扑与上面的自包含应用容器拓扑分别管理。

生产需要外部或受管理的PostgreSQL/Redis、Secret注入、HTTPS入口、备份恢复策略与独立存储。必须ENVIRONMENT=production、TASK_QUEUE_MODE=arq、真实Provider全部路由、强JWT；本地文件存储需共享持久卷，或启用COS额外依赖。镜像构建时必须排除所有嵌套.env；Dockerfile使用非root用户。

发布前冻结：代码与工作树文件hash、镜像digest、uv/npm锁文件、Alembic head、asset bundle hash、测试报告与真实模型Eval。镜像build once并按digest提升，不用mutable latest。迁移由单独Job/全局锁执行，禁止多副本启动时竞争迁移。

按D0-D8执行：范围/授权→安全配置→制品/测试→独立库迁移演练→发布兼容矩阵→镜像/config readback→合成教师业务canary→观察扩流→证据关闭。七类Worker需各自探针；API readiness不替代Worker/Provider/COS验证。Secrets更新需触发进程重建并核对revision。

回滚：优先提升上一不可变镜像；Schema采取expand/forward fix。0002 downgrade会丢失分块/缩略图/样本列，运行前备份，禁止未经授权在真实数据上试回滚。发布停止条件包括错误率/queued卡住/心跳缺失/重复副作用/质量与成本退化。灾备需另行演练RPO/RTO，不能由本地exit0证明。

本任务未执行生产部署、云数据库写入或COS写入；按用户授权执行了本地 Docker PG/Redis 与真实模型调用，证据见[验收记录](../docs/verification.md)。本地链路验证与生产发布证据分别记录。
