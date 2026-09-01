/**
 * 战颅系统 · 智能体架构定义 (Synoxia Agent Architecture)
 * 基于 Google Agent Development Kit (ADK) 架构理念
 *
 * 【本文件是智能体配置架构的单一事实来源】
 * 拓扑编排、提示词分层、基座模型策略、技能集成逻辑、完整度评估均在此定义。
 * 修改智能体架构行为时，只需调整本文件即可。
 *
 * 参考:
 *   - https://adk.dev/agents/                 (Agent = Model + Instruction + Tools)
 *   - https://adk.dev/agents/workflow-agents/ (Sequential / Parallel / Loop 编排)
 *
 * ADK 核心理念:
 *   单 Agent (model + instruction + tools) 可演化为多 Agent Workflow，
 *   以突破上下文窗口与指令复杂度限制，并实现确定性 / 非确定性任务混合编排。
 */

// ─── 基座模型策略 ────────────────────────────────────────────
// 战颅统一采用企业特色模型，不开放用户选择，由平台运维统一管理。
export const ENTERPRISE_MODEL = {
  value: 'enterprise',
  label: '企业特色模型',
  labelEn: 'Enterprise Model',
};

export function resolveModelLabel(model, lang) {
  return lang === 'en' ? ENTERPRISE_MODEL.labelEn : ENTERPRISE_MODEL.label;
}

// ─── 提示词工程 · 五层系统指令 (ADK Instruction) ─────────────
// ADK 中 Agent 的 instruction 是其行为宪法，需覆盖：核心目标、人格、行为约束、
// 工具使用时机与目的、输出格式，并辅以 Few-shot 示例与状态变量。
// 战颅将其拆为五层，每层附带企业级 guide 清单，确保指令细节完整可落地。
//   L1 角色定位   → 身份 · 人格 · 使命 · 受众
//   L2 边界约束   → 红线 · 能力边界 · 合规 · 人工介入 · 隐私
//   L3 推理流程   → 强制思维链 · 决策分支 · 澄清 · 异常降级 · 自检
//   L4 职能与工具 → 核心职责 · 工具清单与用途 · 调用条件 · 参数规范 · 编排
//   L5 输出规范   → 格式 · 字段约束 · 示例样本 · 引用溯源 · 边界输出
export const PROMPT_LAYERS = [
  {
    key: 'prompt_identity',
    zh: '角色定位', en: 'Identity',
    descZh: '定义智能体的身份、人格、专业领域与核心使命，是路由调度的依据',
    descEn: 'Define identity, persona, domain and mission; basis for routing',
    guideZh: ['身份与人格', '专业领域与资深程度', '核心使命与价值', '语气与交互风格', '服务对象与场景'],
    guideEn: ['Identity & persona', 'Domain & seniority', 'Core mission & value', 'Tone & style', 'Audience & scenarios'],
    phZh: '## 身份\n你是【XX 领域】的资深专家，具备 N 年实战经验，服务于【生产/质量/运维…】场景。\n\n## 使命\n负责…，目标是…。\n\n## 语气\n专业、简洁、可执行，优先给出下一步行动。',
    phEn: '## Identity\nYou are a senior expert in [XX domain], serving [scenario].\n\n## Mission\nResponsible for…, aiming to…\n\n## Tone\nProfessional, concise, actionable.',
  },
  {
    key: 'prompt_boundary',
    zh: '边界约束', en: 'Constraint',
    descZh: '红线与能力边界，明确不可执行项、合规要求、隐私约束与人工介入触发条件',
    descEn: 'Red lines, boundaries, compliance, privacy and human escalation triggers',
    guideZh: ['红线清单', '能力边界（可/不可）', '范围外拒绝或转交', '人工介入触发条件', '数据与隐私约束', '监管合规要求'],
    guideEn: ['Hard red lines', 'Capability boundary (can/cannot)', 'Out-of-scope handling', 'Human escalation triggers', 'Data & privacy constraints', 'Regulatory compliance'],
    phZh: '## 红线（绝对不可）\n- 不得越权修改生产数据\n- 不得输出涉密/PII 信息\n\n## 能力边界\n可以：…\n不可：…\n\n## 人工介入触发\n- 置信度 < 80% 时转人工\n- 连续失败 3 次时转人工',
    phEn: '## Red lines (never)\n- No unauthorized data changes\n- No PII/classified output\n\n## Boundary\nCan:…  Cannot:…\n\n## Escalate when\n- confidence < 80%\n- 3 consecutive failures',
  },
  {
    key: 'prompt_reasoning',
    zh: '推理流程', en: 'Process',
    descZh: '强制思维链路：分析 → 拆解 → 执行 → 验证 → 输出，含决策分支与自检',
    descEn: 'Forced chain: analyze → decompose → execute → verify → output, with branching & self-check',
    guideZh: ['强制思维链（CoT）', '决策分支与走向', '澄清策略（信息不足时提问）', '异常处理与降级推理', '输出前自检校验'],
    guideEn: ['Mandatory CoT', 'Decision branching', 'Clarification strategy', 'Exception & fallback reasoning', 'Pre-output self-check'],
    phZh: '面对任务时按以下步骤推理：\n1. 分析：识别意图与关键参数，判断是否在能力范围内\n2. 拆解：将任务分解为可执行子步骤\n3. 执行：逐步调用工具/技能，记录中间结果\n4. 验证：校验结果完整性、准确性与一致性\n5. 输出：按输出规范组装最终回复\n\n信息不足时主动提问澄清，而非臆测。',
    phEn: 'Follow these steps:\n1. Analyze: identify intent & key params, check scope\n2. Decompose: break into sub-steps\n3. Execute: invoke tools, record intermediates\n4. Verify: check completeness, accuracy, consistency\n5. Output: assemble per output spec\n\nAsk clarifying questions when info is insufficient.',
  },
  {
    key: 'prompt_tools',
    zh: '职能与工具', en: 'Capability & Tools',
    descZh: '核心职能定义 + 技能(tools)调用策略：用途、判断条件、参数规范、异常降级与编排',
    descEn: 'Tools + tool invocation: purpose, conditions, params, fallback & orchestration',
    guideZh: ['核心职能清单', '工具清单与用途说明', '调用条件（何时用哪个）', '参数规范与入参校验', '异常降级与重试策略', '多工具编排逻辑'],
    guideEn: ['Core duties', 'Tool catalog & purpose', 'Invocation conditions', 'Parameter specs', 'Fallback & retry strategy', 'Multi-tool orchestration'],
    phZh: '## 核心职能\n1. … 2. …\n\n## 工具与调用策略\n- 技能 `production-efficiency-analysis`：当需要分析产线效率时调用，入参：产线ID、时间范围\n- 技能 `oee-trend-analysis`：当需要设备 OEE 趋势时调用\n\n## 异常降级\n工具失败时重试 2 次，仍失败则降级为人工提示。\n\n## 编排\n先调用 A 获取数据，再调用 B 生成图表。',
    phEn: '## Core duties\n1. … 2. …\n\n## Tools & strategy\n- Skill `xxx`: invoke when…, params: …\n\n## Fallback\nRetry 2×, then degrade to human prompt.\n\n## Orchestration\nCall A for data, then B for chart.',
  },
  {
    key: 'prompt_output',
    zh: '输出规范', en: 'Output',
    descZh: '输出格式、结构、字段强制约束，含 Few-shot 示例、引用溯源与边界输出',
    descEn: 'Mandatory format, fields, few-shot examples, citation & edge-case outputs',
    guideZh: ['输出格式（JSON/Markdown/表格）', '必填字段与结构', '语言与语气', 'Few-shot 示例样本', '引用与防幻觉要求', '边界输出（无结果/异常）'],
    guideEn: ['Output format (JSON/MD/table)', 'Required fields & structure', 'Language & tone', 'Few-shot examples', 'Citation & anti-hallucination', 'Edge-case outputs'],
    phZh: '## 格式\n输出必须为 Markdown，关键结论用 JSON 代码块。\n\n## 必填字段\n```json\n{"summary": "...", "metrics": [...], "recommendation": "..."}\n```\n\n## 示例\n用户问：…\n输出：…\n\n## 防幻觉\n所有数据须标注来源；无数据时回复"暂无可用数据"。',
    phEn: '## Format\nOutput as Markdown; key results in JSON block.\n\n## Fields\n```json\n{"summary": "...", "metrics": [...], "recommendation": "..."}\n```\n\n## Example\nQ: …  A: …\n\n## Anti-hallucination\nCite sources; reply "No data available" when empty.',
  },
];

export function localizedLayer(layer, lang) {
  return {
    key: layer.key,
    label: lang === 'en' ? layer.en : layer.zh,
    desc: lang === 'en' ? layer.descEn : layer.descZh,
    guide: lang === 'en' ? layer.guideEn : layer.guideZh,
    placeholder: lang === 'en' ? layer.phEn : layer.phZh,
  };
}

// ─── 智能体类型 · 单一智能体推理架构 ──────────────────────
// 针对单一智能体的推理架构分类（区别于多智能体团队拓扑）:
//   Sequential   顺序型 → 按预设步骤顺序执行，适合流程化任务
//   Reactive     反应型 → 基于感知即时响应，适合实时监控与告警
//   Deliberative 慎思型 → 深度推理权衡后决策，适合复杂分析与规划
export const AGENT_TYPES = [
  { value: 'sequential',   zh: '顺序型', en: 'Sequential',   descZh: '按预设步骤顺序执行，适合流程化任务', descEn: 'Executes predefined steps in order; suited for procedural tasks' },
  { value: 'reactive',     zh: '反应型', en: 'Reactive',     descZh: '基于感知即时响应，适合实时监控与告警', descEn: 'Responds instantly to stimuli; suited for monitoring & alerts' },
  { value: 'deliberative', zh: '慎思型', en: 'Deliberative', descZh: '深度推理权衡后决策，适合复杂分析与规划', descEn: 'Deliberates before deciding; suited for complex analysis & planning' },
];

export function localizedAgentType(type, lang) {
  const t = AGENT_TYPES.find((x) => x.value === type) || AGENT_TYPES[0];
  return { ...t, label: lang === 'en' ? t.en : t.zh, desc: lang === 'en' ? t.descEn : t.descZh };
}

// ─── 多智能体协作 · 团队拓扑 (ADK Workflow Agent) ─────────────
// 多智能体团队的结构模式（区别于单一智能体类型）:
//   standalone 独立运行 → 单一智能体独立完成任务
//   sequence  顺序协作  → 多智能体按序接力处理
//   loop      循环协作  → 多智能体循环迭代优化
//   parallel  并行协作  → 多智能体并行处理后聚合
// Root Agent 通过 sub_agents 挂载子代理，ADK 自动生成同名委派工具实现动态调度。
export const TOPOLOGIES = [
  { value: 'standalone', zh: '独立运行', en: 'Standalone', descZh: '单一智能体独立完成任务', descEn: 'Single agent completes alone' },
  { value: 'sequence',   zh: '顺序协作', en: 'Sequence',   descZh: '多智能体按序接力处理', descEn: 'Agents run in sequence' },
  { value: 'loop',       zh: '循环协作', en: 'Loop',       descZh: '多智能体循环迭代优化', descEn: 'Agents iterate in a loop' },
  { value: 'parallel',   zh: '并行协作', en: 'Parallel',   descZh: '多智能体并行处理后聚合', descEn: 'Agents work in parallel then aggregate' },
];

// 团队拓扑仅包含 3 种多智能体协作结构（standalone 是独立模式，不属于团队拓扑）
export const TEAM_TOPOLOGIES = TOPOLOGIES.filter((tp) => tp.value !== 'standalone');

// ADK 工作流智能体类名映射（standalone 为 LlmAgent，其余为 Workflow Agent）
export const WORKFLOW_CLASSES = {
  sequence: 'SequentialAgent',
  loop: 'LoopAgent',
  parallel: 'ParallelAgent',
};

const TOPOLOGY_VALUES = TOPOLOGIES.map((tp) => tp.value);

export function normalizeTopology(t) {
  if (TOPOLOGY_VALUES.includes(t)) return t;
  if (t === 'sequential') return 'sequence';
  if (!t) return 'standalone';
  return 'sequence'; // 旧版 hierarchical/debate/supervisor 归入顺序协作
}

export function localizedTopology(topo, lang) {
  const t = TOPOLOGIES.find((x) => x.value === topo) || TOPOLOGIES[0];
  return { ...t, label: lang === 'en' ? t.en : t.zh, desc: lang === 'en' ? t.descEn : t.descZh };
}

// ─── 配置完整度评估 ──────────────────────────────────────────
export const COMPLETENESS_MODULES = [
  { key: 'role',   zh: '角色设定',     en: 'Role Definition',      fields: ['name', 'description', 'agent_type'] },
  { key: 'prompt', zh: '提示词工程',   en: 'Prompt Engineering',   fields: ['prompt_identity', 'prompt_boundary', 'prompt_reasoning', 'prompt_tools', 'prompt_output'] },
  { key: 'skills', zh: '技能配置',     en: 'Skills',               fields: ['skills'] },
  { key: 'collab', zh: '多智能体协作', en: 'Multi-Agent',          fields: ['topology'] },
  { key: 'control', zh: '可控性',      en: 'Controllability',      fields: ['max_call_count', 'max_retries'] },
  { key: 'trace',  zh: '可追溯',       en: 'Traceability',         fields: ['trace_enabled'] },
  { key: 'model',  zh: '模型参数',     en: 'Model Parameters',     fields: ['temperature', 'max_tokens'] },
  { key: 'caps',   zh: '工具',     en: 'Capabilities',         fields: ['capabilities'] },
];

export function calcCompleteness(form, lang) {
  const checklist = COMPLETENESS_MODULES.map((m) => {
    const done = m.fields.every((f) => {
      const v = form[f];
      if (Array.isArray(v)) return v.length > 0;
      if (typeof v === 'boolean') return true;
      return v !== undefined && v !== null && String(v).trim() !== '';
    });
    return { key: m.key, label: lang === 'en' ? m.en : m.zh, done };
  });
  const completed = checklist.filter((c) => c.done).length;
  const pct = Math.round((completed / checklist.length) * 100);
  return { checklist, completed, total: checklist.length, pct };
}

// ─── ADK 配置预览生成 ────────────────────────────────────────
// 将表单数据映射为 ADK 风格的 root_agent 定义，直观展示技能(tools)与子智能体如何集成进架构。
export function buildAdkSpec(form, lang) {
  const en = lang === 'en';
  const name = form.name || 'root_agent';
  const desc = (form.description || '').replace(/\n/g, ' ');
  const aType = localizedAgentType(form.agent_type, lang);

  // 高级编排模式：可组合流程 → SequentialAgent 主干 + 嵌套 LoopAgent / ParallelAgent
  if (form.flow_mode && Array.isArray(form.flow) && form.flow.length) {
    const layers = PROMPT_LAYERS
      .filter((l) => form[l.key] && String(form[l.key]).trim())
      .map((l) => `【${en ? l.en : l.zh}】\n${String(form[l.key]).trim()}`);
    const instruction = layers.length ? layers.join('\n\n') : (en ? '(pending)' : '(待配置)');
    const tools = (form.skills || []).length ? `[${form.skills.join(', ')}]` : '[]';
    const lines = [
      `from zhanlu.adk import LlmAgent, SequentialAgent, LoopAgent, ParallelAgent`,
      ``,
      `root_agent = SequentialAgent(`,
      `    name='${name}',`,
      `    description='${desc}',  # ${en ? 'composable flow' : '可组合协作流程'}`,
      `    instruction="""`,
      instruction,
      `""",`,
      `    tools=${tools},`,
      `    sub_agents=[`,
    ];
    form.flow.forEach((step) => emitFlowStep(step, lines, 2, en));
    lines.push(`    ],`);
    lines.push(`)`);
    return lines.join('\n');
  }

  const topo = form.topology || 'standalone';

  // Standalone: single LLM agent with instruction + tools
  if (topo === 'standalone') {
    const layers = PROMPT_LAYERS
      .filter((l) => form[l.key] && String(form[l.key]).trim())
      .map((l) => `【${en ? l.en : l.zh}】\n${String(form[l.key]).trim()}`);
    const instruction = layers.length ? layers.join('\n\n') : (en ? '(pending)' : '(待配置)');
    const tools = (form.skills || []).length ? `[${form.skills.join(', ')}]` : '[]';
    return [
      `from zhanlu.adk import LlmAgent`,
      ``,
      `root_agent = LlmAgent(`,
      `    model='${ENTERPRISE_MODEL.value}',  # ${en ? 'agent_type' : '智能体类型'}: ${aType.label}`,
      `    name='${name}',`,
      `    description='${desc}',`,
      `    instruction="""`,
      instruction,
      `""",`,
      `    tools=${tools},`,
      `)`,
    ].join('\n');
  }

  // Workflow agents: topology determines the ADK class (SequentialAgent / LoopAgent / ParallelAgent)
  const cls = WORKFLOW_CLASSES[topo] || 'SequentialAgent';
  const topoLabel = localizedTopology(topo, lang).label;
  const subs = form.sub_agents || [];
  const maxIter = form.max_iterations ?? 5;

  const lines = [
    `from zhanlu.adk import ${cls}`,
    ``,
    `root_agent = ${cls}(`,
    `    name='${name}',`,
    `    description='${desc}',  # ${en ? 'topology' : '拓扑'}: ${topoLabel}`,
  ];
  if (topo === 'loop') {
    lines.push(`    max_iterations=${maxIter},  # ${en ? 'max loop iterations' : '最大循环次数'}`);
  }
  lines.push(`    sub_agents=[`);
  subs.forEach((s) => lines.push(`        ${typeof s === 'string' ? s : (s.name || 'unnamed_agent')},`));
  lines.push(`    ],`);
  lines.push(`)`);
  return lines.join('\n');
}

// 高级编排：递归输出流程节点（agent / loop / parallel）
function emitFlowStep(step, lines, indent, en) {
  const pad = ' '.repeat(indent * 4);
  const safe = (s) => String(s || '').replace(/'/g, '');
  if (step.kind === 'loop') {
    lines.push(`${pad}LoopAgent(`);
    lines.push(`${pad}    name='${safe(step.name) || (en ? 'loop' : '循环')}',`);
    lines.push(`${pad}    max_iterations=${step.max_iterations ?? 5},`);
    lines.push(`${pad}    sub_agents=[`);
    (step.flow || []).forEach((s) => emitFlowStep(s, lines, indent + 2, en));
    lines.push(`${pad}    ],`);
    lines.push(`${pad}),`);
  } else if (step.kind === 'parallel') {
    lines.push(`${pad}ParallelAgent(`);
    lines.push(`${pad}    name='${safe(step.name) || (en ? 'parallel' : '并行')}',`);
    lines.push(`${pad}    sub_agents=[`);
    (step.branches || []).forEach((br) => (br || []).forEach((s) => emitFlowStep(s, lines, indent + 2, en)));
    lines.push(`${pad}    ],`);
    lines.push(`${pad}),`);
  } else {
    const nm = safe(step.name) || (en ? 'agent' : '智能体');
    const st = (step.skills || []).length ? `[${step.skills.join(', ')}]` : '[]';
    lines.push(`${pad}LlmAgent(model='${ENTERPRISE_MODEL.value}', name='${nm}', tools=${st}),`);
  }
}