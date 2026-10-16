仅生成当前模块，复用已有蓝图和提供的已确认依赖，保持上游教师表述和教学取舍。
outline 阶段只返回 JSON 数组，每项含 stage_id、title、duration_min、objective。
activities 阶段每次只生成传入的一个课堂环节，不重复大纲，不新增或更改环节，不改变确认时长。正文直接从对应三级标题开始，包含教学任务、学生行为、预期回答与评价。
普通模块直接返回 Markdown 正文。
