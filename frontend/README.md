# 教案助手 Web

React、TypeScript、Vite 教师端与管理员界面。所有业务数据来自 `/api/v1`，没有演示统计卡或浏览器本地假后端。开发环境的 mock 文本由后端显式标注。

## 启动与验证

```powershell
cd frontend
npm ci
npm run dev
npm run typecheck
npm test
npm run build
```

Vite 默认将 `/api` 转发至 `http://127.0.0.1:8000`。可在 `.env.local` 设置 `VITE_API_PROXY`，独立部署可设置 `VITE_API_BASE`。FastAPI 能直接托管 `frontend/dist`。

## 内容与恢复边界

- Tiptap 3 富文本编辑，官方 Markdown 扩展序列化。源码模式可精确选择 Markdown 范围。富文本仅在文本唯一匹配时启用选区润色，避免替换错误位置。
- Markdown 和 KaTeX 最终 HTML 都通过 DOMPurify；无 mhchem 等扩展。无法渲染的普通公式保持源码。
- JWT 仅存当前标签页 sessionStorage。SSE 使用 fetch Bearer；50 秒静默重连，携带 `Last-Event-ID`，按 ID 去重，并用 GET Run 恢复权威终态。
- 一键与结构化备课实时显示模块名称、已生成数量、质量检查和当前课堂环节；默认跟随当前生成模块。手动查看其他模块后可点击“跟随当前模块”。两种模式的正文均按SSE增量渲染，大纲按完整环节逐段显示，正式版本读回前保留预览。
- 未保存正文独立保存在当前标签页的工作副本中。候选只有教师采用后才进入编辑器，正式保存仍经过后端乐观锁。预览/导出使用正式版本。
- 结构化教学过程先确认可排序的大纲，再生成 activities。课后反思使用独立版本与保存入口。

## 安全版本偏差

源方案指定 KaTeX 0.16.22。实现升级至 **0.18.2**，原生核心命令的产品约束保持一致。原因：[KaTeX 官方 GHSA-238p-pmpm-9mq7](https://github.com/KaTeX/KaTeX/security/advisories/GHSA-238p-pmpm-9mq7) 明确所有 `>=0.11.0,<0.18.2` 版本受已有原型污染导致信任绕过影响，没有 0.16 系列修复版。所有公式明确 `trust:false`，输出再经 sanitizer。Tiptap、路由和 Vitest 同步使用已修复版本。2026-10-16 `npm audit`：0 vulnerabilities。

## 浏览器验收路径

1. 注册合成教师 → 一键生成七模块 → 手工保存 → 观察下游需复核状态。
2. 整模块/选区润色 → 候选隔离 → 采用工作副本 → 显式保存；历史版本恢复。
3. 结构化备课 → 确认模块 → 教学过程大纲增删、排序和改时长 → 确认大纲 → activities → finalize。
4. 完整预览 → DOCX/PDF 异步状态 → 下载；失败/取消保留现有正文。
5. 课后反思保存及候选采用、个人库上传/解析/原件下载、偏好保存。
6. 管理员概要、用户启停、运行、问题与审计；普通教师无管理入口。
7. 刷新/断开 SSE 后恢复；检查不同模块未保存工作副本互不覆盖；键盘操作和窄屏。

最终静态包需与 API 同源托管或正确配置 CORS、SPA fallback 和反向代理 SSE 缓冲。Google 字体加载失败时回退本机中文字体，不影响功能。
