/**
 * 战颅系统 · 可组合流程逻辑 (Composable Flow)
 * 高级编排模式下的流程树操作：增删改查、嵌套循环/并行块。
 * 流程节点结构:
 *   agent    → 叶子节点，携带完整智能体配置
 *   loop     → 循环块，含 flow(顺序体) + max_iterations
 *   parallel → 并行块，含 branches(多分支，每分支为顺序流)
 * 路径寻址:
 *   []            → 根节点
 *   [i]           → 根 flow 第 i 步
 *   [i, j]        → (root.flow[i] 为 loop) 其 flow 第 j 步
 *   [i, b, j]     → (root.flow[i] 为 parallel) 第 b 分支第 j 步
 *   更深嵌套按上述规则递归
 */
import { ENTERPRISE_MODEL } from '@/lib/agentArchitecture';

export function uid() {
  return crypto?.randomUUID?.() || `id-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

const DEFAULT_AGENT_FIELDS = {
  description: '', model: ENTERPRISE_MODEL.value, agent_type: 'sequential',
  prompt_identity: '', prompt_boundary: '', prompt_reasoning: '', prompt_tools: '', prompt_output: '',
  skills: [], capabilities: [],
  max_call_count: 50, max_retries: 3, data_read: true, data_write: false, human_fallback: false,
  trace_enabled: true, log_level: 'info',
  temperature: 0.7, top_p: 1, max_tokens: 4096, status: 'active',
};

export function makeStep(kind, index, lang) {
  const isEn = lang === 'en';
  const id = uid();
  if (kind === 'loop') {
    return { id, kind: 'loop', name: isEn ? `Loop ${index}` : `循环块 ${index}`, max_iterations: 5, flow: [] };
  }
  if (kind === 'parallel') {
    return { id, kind: 'parallel', name: isEn ? `Parallel ${index}` : `并行块 ${index}`, branches: [[]] };
  }
  return { id, kind: 'agent', name: isEn ? `Agent ${index}` : `智能体 ${index}`, ...DEFAULT_AGENT_FIELDS };
}

export function getFlowStep(root, path) {
  if (!path || path.length === 0) return root;
  let node = root;
  let i = 0;
  while (i < path.length && node) {
    if (node.branches) {
      const bIdx = path[i];
      const sIdx = path[i + 1];
      node = node.branches?.[bIdx]?.[sIdx];
      i += 2;
    } else if (node.flow) {
      node = node.flow[path[i]];
      i += 1;
    } else {
      break;
    }
  }
  return node;
}

function patchIn(container, path, patch) {
  if (path.length === 0) return { ...container, ...patch };
  if (container.branches) {
    const [bIdx, sIdx, ...rest] = path;
    const branches = container.branches.map((br, b) =>
      b === bIdx ? br.map((s, sIdx2) => (sIdx2 === sIdx ? patchIn(s, rest, patch) : s)) : br
    );
    return { ...container, branches };
  }
  const [idx, ...rest] = path;
  const flow = container.flow.map((s, i) => (i === idx ? patchIn(s, rest, patch) : s));
  return { ...container, flow };
}

export function updateFlowStep(root, path, patch) {
  return patchIn(root, path, patch);
}

function removeFrom(container, path) {
  if (path.length === 0) return container;
  if (container.branches) {
    const [bIdx, sIdx, ...rest] = path;
    if (rest.length === 0) {
      const branches = container.branches.map((br, b) => (b === bIdx ? br.filter((_, i) => i !== sIdx) : br));
      return { ...container, branches };
    }
    const branches = container.branches.map((br, b) =>
      b === bIdx ? br.map((s, sIdx2) => (sIdx2 === sIdx ? removeFrom(s, rest) : s)) : br
    );
    return { ...container, branches };
  }
  const [idx, ...rest] = path;
  if (rest.length === 0) return { ...container, flow: container.flow.filter((_, i) => i !== idx) };
  const flow = container.flow.map((s, i) => (i === idx ? removeFrom(s, rest) : s));
  return { ...container, flow };
}

export function removeFlowStep(root, path) {
  return removeFrom(root, path);
}

export function addToFlow(root, containerPath, kind, lang) {
  const container = getFlowStep(root, containerPath);
  const flow = [...(container?.flow || []), makeStep(kind, (container?.flow?.length || 0) + 1, lang)];
  return updateFlowStep(root, containerPath, { flow });
}

export function addToBranch(root, parallelPath, branchIdx, kind, lang) {
  const parallel = getFlowStep(root, parallelPath);
  const branches = (parallel?.branches || []).map((br, b) =>
    b === branchIdx ? [...br, makeStep(kind, br.length + 1, lang)] : br
  );
  return updateFlowStep(root, parallelPath, { branches });
}

export function addBranch(root, parallelPath) {
  const parallel = getFlowStep(root, parallelPath);
  const branches = [...(parallel?.branches || []), []];
  return updateFlowStep(root, parallelPath, { branches });
}

export function removeBranch(root, parallelPath, branchIdx) {
  const parallel = getFlowStep(root, parallelPath);
  const branches = (parallel?.branches || []).filter((_, b) => b !== branchIdx);
  return updateFlowStep(root, parallelPath, { branches });
}

export function flowStats(root) {
  let agents = 0, loops = 0, parallels = 0;
  function walk(node) {
    if (!node) return;
    if (node.flow) node.flow.forEach(walk);
    if (node.branches) node.branches.forEach((br) => br.forEach(walk));
    if (node.kind === 'agent') agents++;
    else if (node.kind === 'loop') loops++;
    else if (node.kind === 'parallel') parallels++;
  }
  walk(root);
  return { agents, loops, parallels };
}