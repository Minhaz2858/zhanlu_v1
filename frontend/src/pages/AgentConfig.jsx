import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useAuth } from '@/lib/AuthContext';
import { useTranslate } from '@/lib/useTranslate';
import PageHeader from '@/components/PageHeader';
import { Button } from '@/components/ui/button';
import { Save, Play, Trash2, Loader2, ChevronRight, Copy, Users } from 'lucide-react';
import DuplicateAgentDialog from '@/components/agent/DuplicateAgentDialog';
import ResourceAccessDialog from '@/components/ResourceAccessDialog';
import AgentTeamTree from '@/components/agent/AgentTeamTree';
import FlowTree from '@/components/agent/FlowTree';
import BlockConfig from '@/components/agent/BlockConfig';
import ConfigOverview from '@/components/agent/ConfigOverview';
import RoleSection from '@/components/agent/RoleSection';
import PromptSection from '@/components/agent/PromptSection';
import SkillsSection from '@/components/agent/SkillsSection';
import CapabilitiesSection from '@/components/agent/CapabilitiesSection';
import DataSourcesSection from '@/components/agent/DataSourcesSection';
import HarnessAgentSections from '@/components/agent/HarnessAgentSections';
import FusionBridgeSection from '@/components/agent/FusionBridgeSection';
import { normalizeTopology } from '@/lib/agentArchitecture';
import {
  getFlowStep, updateFlowStep, removeFlowStep,
  addToFlow, addToBranch, addBranch, removeBranch,
} from '@/lib/agentFlow';

const DEFAULT_FORM = {
  name: '', description: '', model: 'enterprise', agent_type: 'sequential',
  prompt_identity: '', prompt_boundary: '', prompt_reasoning: '', prompt_tools: '', prompt_output: '',
  skills: [], knowledge_bases: [], topology: 'standalone', sub_agents: [],
  flow_mode: false, flow: [],
  max_call_count: 50, max_retries: 3, max_iterations: 5, data_read: true, data_write: false, human_fallback: false,
  trace_enabled: true, log_level: 'info',
  temperature: 0.7, top_p: 1, max_tokens: 4096,
  capabilities: [], status: 'active',
};

import { coerceStringArray } from '@/lib/jsonArray';

function uid() {
  return crypto?.randomUUID?.() || `id-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// `normalizeCapabilities` is kept as a thin alias so any other code that
// imported it (or audits referencing it) keeps working, but the actual
// implementation lives in `@/lib/jsonArray` and is also applied to
// `skills`, `knowledge_bases`, and `sub_agents` below — historic rows
// could have stored any of those JSON columns as a bare string.
const normalizeCapabilities = coerceStringArray;

function makeSubAgent(index, lang) {
  return {
    id: uid(),
    name: lang === 'en' ? `Sub-Agent ${index}` : `子智能体 ${index}`,
    description: '', model: 'enterprise', agent_type: 'sequential',
    prompt_identity: '', prompt_boundary: '', prompt_reasoning: '', prompt_tools: '', prompt_output: '',
    skills: [], knowledge_bases: [], topology: 'standalone', sub_agents: [],
    max_call_count: 50, max_retries: 3, max_iterations: 5,
    data_read: true, data_write: false, human_fallback: false,
    trace_enabled: true, log_level: 'info',
    temperature: 0.7, top_p: 1, max_tokens: 4096,
    capabilities: [], status: 'active',
  };
}

function normalizeSub(s, i, lang) {
  if (s && typeof s === 'object') return { ...makeSubAgent(i + 1, lang), ...s, id: s.id || uid() };
  if (typeof s === 'string') return { ...makeSubAgent(i + 1, lang), id: uid(), name: s };
  return makeSubAgent(i + 1, lang);
}

/* ── 简洁模式：sub_agents 路径操作 ── */
function getNodeAtPath(node, path) {
  if (!path || path.length === 0) return node;
  const [idx, ...rest] = path;
  const sub = (node?.sub_agents || [])[idx];
  return sub ? getNodeAtPath(sub, rest) : node;
}
function updateNodeAtPath(node, path, patch) {
  if (path.length === 0) return { ...node, ...patch };
  const [idx, ...rest] = path;
  const subs = node.sub_agents || [];
  return { ...node, sub_agents: subs.map((s, i) => (i === idx ? updateNodeAtPath(s, rest, patch) : s)) };
}
function removeNodeAtPath(node, path) {
  if (path.length === 0) return node;
  if (path.length === 1) return { ...node, sub_agents: (node.sub_agents || []).filter((_, i) => i !== path[0]) };
  const [idx, ...rest] = path;
  return { ...node, sub_agents: (node.sub_agents || []).map((s, i) => (i === idx ? removeNodeAtPath(s, rest) : s)) };
}

// True only when the agent EXPLICITLY carries Fusion 360 tools (e.g. the
// CAD Agent's enabled_tools lists fusion360_* names). The all-tools sentinel
// "*" is deliberately NOT counted — that would surface the Fusion bridge on
// every generic agent, which is noise. The endpoint only matters for agents
// that actually drive Fusion.
function hasFusion360Tools(toolConfig) {
  if (!toolConfig || typeof toolConfig !== 'object') return false;
  const enabled = toolConfig.enabled_tools;
  if (!Array.isArray(enabled)) return false;
  return enabled.some((t) => typeof t === 'string' && t.startsWith('fusion360'));
}

export default function AgentConfig() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { t, lang } = useLanguage();
  const { isAdmin } = useAuth();
  const [agent, setAgent] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [form, setForm] = useState(DEFAULT_FORM);
  const [selectedPath, setSelectedPath] = useState([]);
  const [dupOpen, setDupOpen] = useState(false);
  const [accessDialogOpen, setAccessDialogOpen] = useState(false);

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [id]);

  async function load() {
    try {
      const a = await base44.entities.AgentApp.get(id);
      setAgent(a);
      // The capabilities/skills/knowledge_bases JSON columns are
      // string[] in the model, but legacy rows could have serialized any
      // of these as a bare string from the old Step 1 form. Each section
      // (CapabilitiesSection, SkillsSection, DataSourcesSection) treats
      // its field as `string[]` and uses .map / .filter / .includes —
      // so we normalize on read to avoid an unmount-level TypeError.
      setForm({
        ...DEFAULT_FORM, ...a,
        model: 'enterprise',
        agent_type: a.agent_type || 'sequential',
        temperature: a.temperature ?? 0.7, top_p: a.top_p ?? 1, max_tokens: a.max_tokens ?? 4096,
        max_call_count: a.max_call_count ?? 50, max_retries: a.max_retries ?? 3, max_iterations: a.max_iterations ?? 5,
        topology: normalizeTopology(a.topology), log_level: a.log_level || 'info',
        skills: coerceStringArray(a.skills),
        knowledge_bases: coerceStringArray(a.knowledge_bases),
        // sub_agents is an array of sub-agent OBJECTS, not strings; if
        // the API hands us anything weird, fall back to an empty list.
        sub_agents: Array.isArray(a.sub_agents) ? a.sub_agents : [],
        flow_mode: a.flow_mode || false, flow: a.flow || [],
        capabilities: normalizeCapabilities(a.capabilities),
      });
    } catch { setAgent(null); }
    finally { setLoading(false); }
  }

  async function save() {
    setSaving(true);
    try {
      await base44.entities.AgentApp.update(id, form);
      setAgent({ ...agent, ...form });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally { setSaving(false); }
  }

  // Auto-save a single field without touching the main save button state.
  // Used by sections (e.g. DataSourcesSection) that should persist on each change.
  // The child component already calls update(patch) to sync local state; this
  // function sends the full merged form to the backend in one request.
  async function saveField(patch) {
    try {
      const merged = { ...form, ...patch };
      await base44.entities.AgentApp.update(id, merged);
      setForm(merged);
      setAgent((prev) => (prev ? { ...prev, ...patch } : prev));
    } catch (err) {
      console.error('Auto-save failed:', err);
    }
  }

  async function remove() {
    await base44.entities.AgentApp.delete(id);
    navigate('/my-space');
  }

  const flowMode = !!form.flow_mode;

  useEffect(() => {
    const node = flowMode ? getFlowStep(form, selectedPath) : getNodeAtPath(form, selectedPath);
    if (!node && selectedPath.length > 0) setSelectedPath([]);
    // eslint-disable-next-line
  }, [form, selectedPath, flowMode]);

  // 选中节点 patch（双模式统一入口）
  function update(patch) {
    if (flowMode) setForm((prev) => updateFlowStep(prev, selectedPath, patch));
    else setForm((prev) => updateNodeAtPath(prev, selectedPath, patch));
  }
  function updateRoot(patch) {
    setForm((prev) => ({ ...prev, ...patch }));
  }
  function navigateToSection(key) {
    document.getElementById(`agent-section-${key}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /* ── 简洁模式操作 ── */
  function handleAddSubAgent() {
    const idx = (form.sub_agents || []).length;
    const newSub = makeSubAgent(idx + 1, lang);
    setForm((prev) => ({ ...prev, sub_agents: [...(prev.sub_agents || []), newSub] }));
    setSelectedPath([idx]);
  }
  function handleRemoveNode(path) {
    setForm((prev) => removeNodeAtPath(prev, path));
    setSelectedPath([]);
  }

  /* ── 高级编排操作 ── */
  function handleAddStep(containerPath, kind) {
    setForm((prev) => addToFlow(prev, containerPath, kind, lang));
    const container = getFlowStep(form, containerPath);
    setSelectedPath([...containerPath, (container?.flow?.length || 0)]);
  }
  function handleAddToBranch(parallelPath, branchIdx, kind) {
    setForm((prev) => addToBranch(prev, parallelPath, branchIdx, kind, lang));
    const parallel = getFlowStep(form, parallelPath);
    setSelectedPath([...parallelPath, branchIdx, (parallel?.branches?.[branchIdx]?.length || 0)]);
  }
  function handleAddBranch(parallelPath) {
    setForm((prev) => addBranch(prev, parallelPath));
  }
  function handleRemoveBranch(branchIdx) {
    setForm((prev) => removeBranch(prev, selectedPath, branchIdx));
  }
  function handleRemoveFlow(path) {
    setForm((prev) => removeFlowStep(prev, path));
    setSelectedPath([]);
  }

  const selectedNode = flowMode ? getFlowStep(form, selectedPath) : getNodeAtPath(form, selectedPath);
  const isRoot = selectedPath.length === 0;
  const isBlock = flowMode && selectedNode && (selectedNode.kind === 'loop' || selectedNode.kind === 'parallel');
  // `agent` is the raw response from the API; `agent.capabilities` is a
  // JSON column that historically round-tripped as a comma-separated
  // string for legacy rows. Coerce to an array before calling .filter /
  // .map so this never throws at render time.
  const translate = useTranslate(
    [agent?.name, agent?.description, ...coerceStringArray(agent?.capabilities)].filter(Boolean),
    lang,
  );

  if (loading) return <div className="flex h-full items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>;
  if (!agent) return <div className="px-8 py-8"><PageHeader title={t.detail.notFound} /></div>;

  return (
    <div className="h-full overflow-y-auto px-8 py-8 lg:flex lg:min-h-0 lg:flex-col lg:overflow-hidden">
      <PageHeader
        backTo="/my-space"
        title={translate(form.name) || t.agentConfig.title}
        action={
          <div className="flex gap-2">
            {isAdmin && (
              <Button onClick={() => setAccessDialogOpen(true)} size="sm" variant="outline" className="gap-1.5">
                <Users className="h-3.5 w-3.5" /> {t.agentConfig.manageAccess || 'Access'}
              </Button>
            )}
            <Button onClick={() => navigate(`/?agent=${id}`)} size="sm" className="gap-1.5">
              <Play className="h-3.5 w-3.5" /> {t.agentConfig.runAgent}
            </Button>
            <Button onClick={save} disabled={saving} size="sm" className="gap-1.5">
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
              {saved ? t.agentConfig.saved : t.agentConfig.save}
            </Button>
            <Button onClick={() => setDupOpen(true)} size="sm" className="gap-1.5 rounded-full bg-accent px-4 text-accent-foreground shadow-none hover:bg-accent/80">
              <Copy className="h-3.5 w-3.5" /> {t.agentConfig.duplicate}
            </Button>
            <Button onClick={remove} variant="outline" size="sm" className="text-muted-foreground hover:text-destructive">
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        }
      />
      <div className="grid grid-cols-1 gap-6 lg:min-h-0 lg:flex-1 lg:grid-cols-3">
        <div className="space-y-6 lg:min-h-0 lg:overflow-y-auto lg:pr-2">
          {flowMode ? (
            <FlowTree
              root={form}
              selectedPath={selectedPath}
              onSelect={setSelectedPath}
              onAddStep={handleAddStep}
              onAddToBranch={handleAddToBranch}
              onAddBranch={handleAddBranch}
              onRemove={handleRemoveFlow}
              lang={lang}
              t={t}
            />
          ) : (
            <AgentTeamTree
              node={form}
              selectedPath={selectedPath}
              onSelect={setSelectedPath}
              onAdd={handleAddSubAgent}
              onRemove={handleRemoveNode}
              onUpdateIterations={(n) => updateRoot({ max_iterations: n })}
              lang={lang}
              t={t}
            />
          )}
          {!isBlock && <ConfigOverview form={selectedNode} t={t} onNavigate={navigateToSection} />}
        </div>

        <div className="space-y-6 lg:col-span-2 lg:min-h-0 lg:overflow-y-auto lg:pr-2">
          {/* 面包屑 */}
          <nav className="flex items-center gap-1.5 text-xs">
            <button
              onClick={() => setSelectedPath([])}
              className={`transition-colors ${isRoot ? 'font-medium text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
            >
              {lang === 'en' ? 'Root' : '根代理'} · {form.name || (lang === 'en' ? 'Root Agent' : '根代理')}
            </button>
            {!isRoot && (
              <>
                <ChevronRight className="h-3 w-3 text-muted-foreground/50" />
                <span className="font-medium text-foreground">{selectedNode.name || (isBlock ? (lang === 'en' ? 'Block' : '协作块') : (lang === 'en' ? 'Sub-Agent' : '子智能体'))}</span>
              </>
            )}
          </nav>

          {isBlock ? (
            <BlockConfig
              node={selectedNode}
              update={update}
              onAddBranch={() => handleAddBranch(selectedPath)}
              onRemoveBranch={handleRemoveBranch}
              lang={lang}
              t={t}
            />
          ) : (
            <>
              <div id="agent-section-role" className="scroll-mt-2"><RoleSection form={selectedNode} update={update} t={t} isRoot={isRoot} /></div>
              <div id="agent-section-prompt" className="scroll-mt-2"><PromptSection form={selectedNode} update={update} t={t} /></div>
              <div id="agent-section-skills" className="scroll-mt-2"><SkillsSection form={selectedNode} update={update} t={t} /></div>
              <div id="agent-section-data" className="scroll-mt-2"><DataSourcesSection form={selectedNode} update={update} onChange={saveField} t={t} /></div>
              <div id="agent-section-caps" className="scroll-mt-2"><CapabilitiesSection form={selectedNode} update={update} t={t} /></div>
              {isRoot && hasFusion360Tools(form.tool_config) && (
                <div id="agent-section-fusion" className="scroll-mt-2"><FusionBridgeSection form={selectedNode} update={update} /></div>
              )}
              <div id="agent-section-harness" className="scroll-mt-2"><HarnessAgentSections form={selectedNode} update={update} /></div>
            </>
          )}
        </div>
      </div>
      <DuplicateAgentDialog open={dupOpen} onOpenChange={setDupOpen} agent={form} onDone={(newId) => navigate(`/my-space/agent/${newId}`)} />

      {/* ── Admin: Manage Access dialog ── */}
      {accessDialogOpen && (
        <ResourceAccessDialog
          resourceType="agent"
          resourceId={id}
          resourceName={form.name || `Agent ${id}`}
          onClose={() => setAccessDialogOpen(false)}
        />
      )}
    </div>
  );
}