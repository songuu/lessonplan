# AI运行时接入

业务Service拥有数据库事务、候选采用与终态；`LessonRuntime`不读写数据库。入口：`app/ai/runtime.py`；Gateway：`app/ai/gateway.py`；资产：`AssetBundle`。

`generate(input,modules,blueprint?,dependencies?,stage?,outline?,event_sink?,checkpoint_sink?,resume?)`返回模块、蓝图、calls、findings、资产指纹。一键正常路径只调用一次one_click模型，按冻结module_id边界流式拆分；无效边界依次JSON和DAG批次兜底，关闭临时流并保留风格上下文。结构化缺蓝图时使用structured路由生成一次Blueprint，之后复用；稳定上下文来自已确认相关依赖及草稿冻结的知识资料。教学过程outline与activities分离，后者严格沿用教师环节/顺序/时长。

`polish`只返回候选，scope=selection只发送选区文本；最终保存仍校验当前版本。`review`使用独立后台Run，不自动改正文、不阻塞ready或导出。

生成、润色和后台审查均支持可选的 `checkpoint_sink(key,payload)` 与 `resume`。Service负责持久化、指纹和执行权；Runtime验证可复用结果，仅生成缺失单元。格式或业务硬门失败默认额外修正1次，预算与调用账本跨恢复累计。事件或持久化回调失败直接传播，不能转为质量重试；详见[知识检索与断点恢复](知识检索与断点恢复.md)。

模型路由：one_click、structured（含BlueprintPlanner）、module_operation（润色）、review。优先各专用模型，通用LLM_MODEL兜底。真实Provider必须显式配置；生产校验所有路由、强JWT、PG与ARQ，拒绝mock。Gateway支持OpenAI-compatible SSE/JSON、预算、超时、截断/空响应/拒绝、主备尝试和token用量。主失败备成功记录逻辑success、primary_attempt_outcome=failed、fallback_used=true，问题事实不会被最终成功掩盖。

渐进Prompt只加载当前workflow、学科指南和模块片段；字典value转换到中文label后匹配质量模式。37项受控资产由config/asset_manifest.json登记，加载即校验hash、JSON Schema、consumer与依赖DAG，Runtime返回实际consumed路径。SSE事件入库前通过stream_event.schema校验。风格注册表控制匹配权重/最低分，保留近邻个人来源，低于阈值才使用学段系统风格。验证命令`uv run python scripts/validate_assets.py`。

质量：Schema、明确课题/学段/学科/课型冲突、安全公式/HTML/实验风险、教材原文边界、教学过程规范化。SafePatch只做白名单低风险修改。普通未知公式/未闭合定界符保留正文并记录math_render_warning，不自动重生成；危险命令阻断保存/交付。规则不等于学科专家终审，真实模型质量须另做E4评测。

前端KaTeX因官方安全公告升级0.18.2，仍限定原生核心命令，trust=false并清洗最终HTML；不加载mhchem。DOCX常用公式原生OMML，PDF常用公式独立排版；未知复杂公式保留精确源码并写导出警告。个人库PDF缩略图为原始首页，DOCX为明确标注parsed_preview的解析正文预览，不宣称保留原始Word分页。
