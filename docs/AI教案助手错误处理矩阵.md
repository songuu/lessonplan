# 错误与恢复

| 类别 | HTTP/Run结果 | 用户行为 / 工程事实 |
|---|---|---|
| 未登录/过期/注销/停用 | 401受控码 | 重新登录，不泄露token |
| 他人资源 | 404 NOT_FOUND | 不暴露存在性 |
| 普通教师管理接口 | 403 FORBIDDEN | 无写入 |
| 字典/模块/输入错误 | 422 | 修改输入；不创建Run |
| VERSION_CONFLICT | 409 | 保留工作副本，刷新对比后重试 |
| IDEMPOTENCY_CONFLICT | 409 | 同键异参或跨workflow复用拒绝 |
| RUN_ACTIVE / OUTLINE_NOT_CONFIRMED | 409 | 等待/先确认，不跳过人工门 |
| REQUEST_TOO_LARGE / REQUEST_BODY_TIMEOUT | 413 / 408 | 解析前拒绝，关闭/不创建临时文件 |
| QUEUE_UNAVAILABLE | Run failed | 明确失败，保留Outbox及问题事实；新key重试 |
| Provider超时/空/截断/格式错 | Run failed或主备成功 | 公共错误受控，保留attempt与fallback，不显示Provider原文 |
| 质量/危险公式 | Run failed或保存422 | 不提交版本，保留诊断；普通渲染问题只告警 |
| USER_CANCELLED / DOCUMENT_DELETED | Run cancelled | 唯一终态，阻止晚到版本/文件成功 |
| WORKFLOW_TIMEOUT / WORKER_INTERRUPTED / WORKER_LOST | Run failed | 不伪装教师取消；记录真实状态 |
| 未分类ORM/内部异常 | WORKFLOW_FAILED | 兼容异常code=None，失败收口仍可完成 |
| 缩略图失败 | 文档解析可ready+warning | 不宣称存在缩略图；提交失败/删除清理PNG |
| 普通公式不支持 | 保留源码+warning | 不触发整模块重生成，导出记录警告 |

失败事件可携带state_preserved/temporary_content_available，前端保留已有正文。错误关联run_id/request_id，Issue metadata只保存工作流、队列、job、模块与阶段等受控上下文。
