// Workflow graph <-> xyflow conversion + node catalog metadata.

/**
 * TWO different shapes share the same `agent_apps.flow` JSON column and the
 * same `flow_mode=true` flag, and they are NOT interchangeable:
 *
 *   n8n canvas workflow   → { version, nodes: [], edges: [] }   (WorkflowCanvas)
 *   agent-architecture    → [ { kind: 'agent'|'loop'|'parallel', ... } ]
 *                           (AgentConfig "composable flow" mode)
 *
 * `flow_mode` alone cannot tell them apart, so every entry point must branch
 * on the SHAPE. Rendering one shape in the other's editor throws at render
 * time ("flow.forEach is not a function" — AgentConfig/FlowTree over a
 * canvas object) and would persist the wrong shape on save.
 *
 * @param {object | null | undefined} flow raw `agent_apps.flow` value
 * @returns {boolean} true when the value is the n8n canvas workflow shape
 */
export function isWorkflowFlow(flow) {
  return !!flow && typeof flow === 'object' && !Array.isArray(flow) && Array.isArray(flow.nodes);
}

/**
 * The mirror of {@link isWorkflowFlow}: the composable-flow STEP ARRAY shape
 * written by AgentConfig (`[ { kind: 'agent'|'loop'|'parallel', ... } ]`).
 * The canvas must refuse to load one of these — `flowToXY` expects
 * `{ nodes, edges }`, so an agent row opened on `/my-space/workflow/:id`
 * renders an empty/broken canvas and could overwrite the agent's flow on
 * save. Route those rows back to the agent editor.
 *
 * @param {object | null | undefined} flow raw `agent_apps.flow` value
 * @returns {boolean} true when the value is the agent-architecture step array
 */
export function isAgentArchFlow(flow) {
  return Array.isArray(flow);
}

/**
 * Which editor owns this `agent_apps` row — the n8n workflow canvas
 * (`/my-space/workflow/:id`) or the agent config page (`/my-space/agent/:id`)?
 *
 * `agent_apps` is shared by chat agents and Agent Workflows, and the
 * `flow_mode` flag ALONE does not identify a workflow: agents that use the
 * composable-flow mode also set `flow_mode=true`. The `flow` SHAPE is the
 * only reliable signal, so both pages route through this one function:
 *
 *   { version, nodes, edges }  → 'workflow'
 *   [ { kind, ... }, ... ]     → 'agent'    (composable-flow agent)
 *   null / undefined           → falls back to flow_mode (a flow_mode row
 *                                with no graph yet is a fresh workflow)
 *
 * Both pages must use it, or the two guards can disagree and bounce a row
 * back and forth forever. Because the shape branches are mutually exclusive
 * (`isWorkflowFlow` XOR `isAgentArchFlow`) and AgentConfig only ever bounces
 * on 'workflow', the redirect is guaranteed to terminate.
 *
 * @param {{ flow?: unknown, flow_mode?: unknown } | null | undefined} row
 * @returns {'workflow' | 'agent'}
 */
export function editorForRow(row) {
  if (isWorkflowFlow(row?.flow)) return 'workflow';
  if (isAgentArchFlow(row?.flow)) return 'agent';
  return row?.flow_mode ? 'workflow' : 'agent';
}

export const NODE_GROUPS = {
  triggers: { label: { en: 'Triggers', zh: '触发器' }, color: '#10b981' },
  ai: { label: { en: 'AI', zh: 'AI' }, color: '#8b5cf6' },
  logic: { label: { en: 'Logic', zh: '逻辑' }, color: '#f59e0b' },
  output: { label: { en: 'Output', zh: '输出' }, color: '#3b82f6' },
};

export const NODE_DEFS = [
  { type: 'trigger_chat', group: 'triggers', label: { en: 'Chat Trigger', zh: '聊天触发器' }, icon: '⚡',
    config: {}, required: [], description: { en: 'Fires when a user chats with this workflow', zh: '用户与此工作流对话时触发' } },
  { type: 'trigger_manual', group: 'triggers', label: { en: 'Manual Trigger', zh: '手动触发器' }, icon: '▶',
    config: {}, required: [], description: { en: 'Fires when you run the workflow from the canvas', zh: '从画布手动运行工作流时触发' } },
  { type: 'trigger_schedule', group: 'triggers', label: { en: 'Schedule Trigger', zh: '定时触发器' }, icon: '⏰',
    config: { cron: '0 9 * * *', input: '' }, required: ['cron'],
    description: { en: 'Fires on a cron schedule (published only)', zh: '按 cron 表达式定时触发（仅已发布）' } },
  { type: 'trigger_webhook', group: 'triggers', label: { en: 'Webhook Trigger', zh: 'Webhook 触发器' }, icon: '🔗',
    config: { token: '', method: 'POST', input_field: 'input' }, required: ['token'],
    description: { en: 'Fires when an HTTP request hits your webhook URL', zh: '当 HTTP 请求访问 Webhook 地址时触发' } },
  { type: 'llm', group: 'ai', label: { en: 'LLM', zh: '大模型' }, icon: '◈',
    config: { prompt: '', model: 'automatic', temperature: 0.3 }, required: ['prompt'],
    description: { en: 'Call the LLM with a prompt (supports {{inputs.<node>.<field>}})', zh: '调用大模型（支持 {{inputs.<节点>.<字段>}}）' } },
  { type: 'agent', group: 'ai', label: { en: 'Agent', zh: '智能体' }, icon: '🤖',
    config: { agent_id: '', input_template: '' }, required: ['agent_id'],
    description: { en: 'Invoke an existing agent as a sub-agent', zh: '调用已有智能体作为子智能体' } },
  { type: 'tool', group: 'ai', label: { en: 'Tool', zh: '工具' }, icon: '🔧',
    config: { tool_id: '', input_template: '' }, required: ['tool_id'],
    description: { en: 'Run a Toolkit skill', zh: '执行工具包技能' } },
  { type: 'kb_search', group: 'ai', label: { en: 'KB Search', zh: '知识库检索' }, icon: '📚',
    config: { kb_id: '', query: '', top_k: 5 }, required: ['kb_id'],
    description: { en: 'Retrieve chunks from a knowledge base', zh: '从知识库检索内容' } },
  { type: 'db_query', group: 'data', label: { en: 'Database Query', zh: '数据库查询' }, icon: '🗄️',
    config: { kb_id: '', mode: 'sql', sql: '', question: '', limit: 50 }, required: ['kb_id'],
    description: { en: 'Run SQL or a natural-language question against a connected database', zh: '对已连接的数据库执行 SQL 或自然语言查询' } },
  { type: 'mcp_tool', group: 'data', label: { en: 'MCP Tool', zh: 'MCP 工具' }, icon: '🔌',
    config: { server_id: '', tool_name: '', arguments: '' }, required: ['server_id', 'tool_name'],
    description: { en: 'Call a tool on a connected MCP server (Feishu, Drive, GitHub, …)', zh: '调用已连接 MCP 服务器上的工具' } },
  { type: 'http_request', group: 'data', label: { en: 'HTTP Request', zh: 'HTTP 请求' }, icon: '🌐',
    config: { method: 'GET', url: '', headers: [], body: '', auth_type: 'none', bearer_token: '', timeout: 15, response_format: 'auto' }, required: ['url'],
    description: { en: 'Call a REST API', zh: '调用 REST API' } },
  { type: 'code', group: 'data', label: { en: 'Code', zh: '代码' }, icon: '⌨️',
    config: { language: 'python', code: '', timeout: 10 }, required: ['code'],
    description: { en: 'Run Python to transform the input item', zh: '运行 Python 转换输入数据' } },
  { type: 'set_fields', group: 'data', label: { en: 'Set Fields', zh: '设置字段' }, icon: '✏️',
    config: { fields: [] }, required: [],
    description: { en: 'Add or overwrite fields on each item', zh: '为每个数据项添加或覆盖字段' } },
  { type: 'split_out', group: 'data', label: { en: 'Split Out', zh: '拆分' }, icon: '✂️',
    config: { field: 'items', item_field: 'item' }, required: [],
    description: { en: 'Expand an array field into one item per element', zh: '将数组字段拆分为多条数据' } },
  { type: 'remove_duplicates', group: 'data', label: { en: 'Remove Duplicates', zh: '去重' }, icon: '🧹',
    config: { field: 'id' }, required: [],
    description: { en: 'Remove items with duplicate values for a key', zh: '按字段去重' } },
  { type: 'condition', group: 'logic', label: { en: 'Condition', zh: '条件分支' }, icon: '🔀',
    config: { field: '', op: 'eq', value: '', on_true: 'true', on_false: 'false' }, required: ['field'],
    description: { en: 'Route to the true or false branch based on upstream output', zh: '根据上游输出路由到真/假分支' } },
  { type: 'switch', group: 'logic', label: { en: 'Switch', zh: '多分支' }, icon: '🔀',
    config: { cases: [], fallback_label: 'default' }, required: [],
    description: { en: 'Route to the first matching case (n8n Switch)', zh: '路由到第一个匹配的分支（n8n Switch）' } },
  { type: 'output', group: 'output', label: { en: 'Output', zh: '输出' }, icon: '➤',
    config: { format: 'markdown' }, required: [],
    description: { en: 'Final answer returned to the user', zh: '返回给用户的最终回答' } },
];

export const NODE_DEF_MAP = Object.fromEntries(NODE_DEFS.map((d) => [d.type, d]));

export function nodeLabel(type, lang) {
  const def = NODE_DEF_MAP[type];
  if (!def) return type;
  return lang === 'zh' ? def.label.zh : def.label.en;
}

// Fallback metadata (used before the registry cache is populated).
export function nodeTypeDef(type) {
  return NODE_DEF_MAP[type] || null;
}

// flow JSON -> xyflow
export function flowToXY(flow) {
  const nodes = (flow?.nodes || []).map((n) => ({
    id: n.id,
    type: n.type,
    position: n.position || { x: 0, y: 0 },
    data: { label: n.data?.label || n.type, config: n.data?.config || {} },
  }));
  // xyflow requires a unique id per edge — flows persisted without ids
  // (e.g. AI-builder saves that bypassed normalization) would otherwise
  // render as unconnected nodes. Assign stable e1..eN ids on load.
  const edges = (flow?.edges || []).map((e, i) => ({
    id: e.id || `e${i + 1}`,
    source: e.source,
    sourceHandle: e.sourceHandle || null,
    target: e.target,
    targetHandle: e.targetHandle || null,
  }));
  return { nodes, edges };
}

// xyflow -> flow JSON
export function xyToFlow(nodes, edges, settings) {
  return {
    version: 1,
    settings: settings || {},
    nodes: nodes.map((n) => ({
      id: n.id,
      type: n.type,
      position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
      data: { label: n.data.label || NODE_DEF_MAP[n.type]?.label.en || n.type, config: n.data.config || {} },
    })),
    edges: edges.map((e, i) => ({
      id: e.id || `e${i + 1}`,
      source: e.source,
      sourceHandle: e.sourceHandle || null,
      target: e.target,
      targetHandle: e.targetHandle || null,
    })),
  };
}

export function defaultNode(type, position) {
  const def = NODE_DEF_MAP[type];
  const config = def ? JSON.parse(JSON.stringify(def.config)) : {};
  return {
    id: `n_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`,
    type,
    position,
    data: { label: def ? def.label.en : type, config },
  };
}

// Per-node validation message for the canvas (mirrors backend REQUIRED_CONFIG).
export function nodeConfigError(type, config) {
  const def = NODE_DEF_MAP[type];
  if (!def) return `Unknown node type: ${type}`;
  const cfg = config || {};
  for (const field of def.required) {
    const val = cfg[field];
    const empty = val === undefined || val === null || val === '' ||
      (Array.isArray(val) && val.length === 0);
    if (empty) return `Missing required config: ${field}`;
  }
  return null;
}

/**
 * Build a pasted graph from a clipboard snapshot: fresh ids, position offset,
 * internal edges remapped, and {{inputs.<oldId>.}} expression refs rewritten
 * to the pasted ids (references to nodes NOT in the clipboard stay as-is).
 */
export function buildPasteGraph(clipboard, offset = { x: 24, y: 24 }) {
  const nodes = clipboard.nodes || [];
  const edges = clipboard.edges || [];
  const idMap = {};
  const stamp = () => `${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
  const newNodes = nodes.map((n) => {
    const newId = `n_${stamp()}`;
    idMap[n.id] = newId;
    return {
      ...n,
      id: newId,
      position: { x: (n.position?.x || 0) + offset.x, y: (n.position?.y || 0) + offset.y },
      selected: false,
      data: remapExpressions(n.data, idMap),
    };
  });
  const newEdges = edges
    .filter((e) => idMap[e.source] && idMap[e.target])
    .map((e) => ({ ...e, id: `e_${stamp()}`, source: idMap[e.source], target: idMap[e.target], selected: false }));
  return { nodes: newNodes, edges: newEdges };
}

function remapExpressions(value, idMap) {
  if (typeof value === 'string') {
    return value.replace(/\{\{inputs\.([A-Za-z0-9_]+)\./g, (m, id) => (idMap[id] ? `{{inputs.${idMap[id]}.` : m));
  }
  if (Array.isArray(value)) return value.map((v) => remapExpressions(v, idMap));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, remapExpressions(v, idMap)]));
  }
  return value;
}
