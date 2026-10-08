# Workflow Builder "AI 构建" — 聊天式端到端构建（范围 B）

日期：2026-09-17
状态：已批准，进入实现

## 目标
让用户在 `/my-space/workflow/:id` 的「AI 构建」侧边栏里，用自然语言描述工作流，
对话即可产出一份可落到画布的工作流：无需看裸 JSON、失败有预览、可迭代修改。

## 根因（已实现现状诊断）
1. **Prompt/校验自相矛盾**：`workflow_builder_system_prompt` 规则 3 只允许 `trigger_chat`，
   但 `validate_flow` 允许 4 种触发器；用户要「每周一 09:00」定时触发时模型被迫自造类型
   （如 `定时触发器`），`is_trigger_type` 数出 0 个触发器 → "exactly one trigger node"。
2. 模型输出类型字符串/标签易漂移（中文标签、别名）。
3. 失败后无修复，只把错误文本追加到气泡，流结束，需用户手动再发。
4. 前端把 AI 输出的 `:::decision-summary` 当裸 JSON 渲染（一大坨等宽面板），无预览/应用入口。

## 方案（范围 B，已对稳定 SSE 链路去风险）
保持后端「解析 → 校验 → 落库 → `flow_saved` SSE」主链路不变（避免触碰 v3 流的 3 个发射点
与 stable-answer 门），在落库前增加**确定性归一+修复**，并把聊天气泡从裸 JSON 改为
**工作流预览卡片**。

### 1. 后端 prompt 修正 `backend/app/services/agent_prompts.py`
- 规则 3：允许全部 4 种触发器（`trigger_chat/manual/schedule/webhook`），给出
  `trigger_schedule` 的 cron 字段示例（覆盖「每周一 09:00」= `0 9 * * 1`）。
- 新增类型别名归一说明：模型可用中文标签/别名，系统会映射到注册表 type。

### 2. 后端归一+修复 `backend/app/services/workflow_runtime.py`
- 新增 `normalize_workflow_flow(flow)`：
  - 类型别名表（定时触发器/cron/手动触发/聊天触发/输出/大模型/智能体… → 注册表 type）。
  - 补 edge id（e1..eN）。
  - 补缺失 `position`（按 BFS 深度从左到右布局）。
  - 触发器计数：0 个 → 前置一个 `trigger_chat` 并连到首节点（覆盖最常见失败）。
- `save_flow_from_decision_summary`：先 `normalize_workflow_flow` 再 `validate_flow`；
  仍 `flow_invalid` 时再修一次后复验，仍失败才返回错误（即确定性「自动重试」）。

### 3. 前端预览卡片 `frontend/src/components/workflow/WorkflowBuilderChat.jsx`
- 新增 `parseDecisionSummary(content)` 提取 `:::decision-summary` JSON。
- 含 decision-summary 的助手气泡改渲染 `<WorkflowPreviewCard>`：
  标题 + 节点列表（类型图标/标签）+ 连线链 A→B→C + 校验徽标 +
  「应用到画布 / Open on canvas」按钮（调 `onFlowSaved()` 重绘画布，后端已落库）。
- 保留 `flow_saved` 检测触发 reload。

## 不做（YAGNI，留待 C 期）
多轮增量只改一个节点、节点参数 AI 代填、运行报错回聊修复。

## 影响面
- 低风险：prompt 文本、workflow_runtime 纯函数、前端组件渲染。
- 不触碰 v3 流 SSE 发射点与 stable-answer 门。
- 现有「后端直存」路径保留；预览卡片为展示层增强。
