import { useState } from 'react';
import { ChevronDown, ChevronRight, Pencil, X, Check, RefreshCw, Shield, ShieldAlert, ShieldCheck, Database, Wrench, Brain, ScrollText, FileOutput, BarChart3 } from 'lucide-react';

const FIELD_META = {
  manifest_json: { label: 'Mission & Manifest', icon: ScrollText, color: 'text-amber-500' },
  data_bindings: { label: 'Data Bindings', icon: Database, color: 'text-blue-500' },
  skill_bindings: { label: 'Skill Bindings', icon: Wrench, color: 'text-purple-500' },
  memory_scope: { label: 'Memory Scope', icon: Brain, color: 'text-green-500' },
  policy_profile: { label: 'Policy Profile', icon: Shield, color: 'text-red-500' },
  output_contract: { label: 'Output Contract', icon: FileOutput, color: 'text-cyan-500' },
  evaluation_profile: { label: 'Evaluation Profile', icon: BarChart3, color: 'text-orange-500' },
};

function JsonField({ fieldKey, value, onChange, readOnly }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const meta = FIELD_META[fieldKey] || { label: fieldKey, icon: ScrollText, color: 'text-muted-foreground' };
  const Icon = meta.icon;

  const isEmpty = value === null || value === undefined || (typeof value === 'object' && Object.keys(value).length === 0 && !Array.isArray(value)) || value === '';

  function handleEdit() {
    setDraft(isEmpty ? '' : JSON.stringify(value, null, 2));
    setEditing(true);
  }

  function handleSave() {
    try {
      const parsed = draft.trim() ? JSON.parse(draft) : null;
      onChange(parsed);
      setEditing(false);
    } catch {
      // keep editing, let user fix JSON
    }
  }

  function handleCancel() {
    setEditing(false);
  }

  function badgeForMemoryScope(v) {
    if (v === 'app_shared') return <span className="rounded-full bg-green-500/10 px-2 py-0.5 text-[10px] font-medium text-green-400">App Shared</span>;
    if (v === 'user_private') return <span className="rounded-full bg-blue-500/10 px-2 py-0.5 text-[10px] font-medium text-blue-400">User Private</span>;
    if (v === 'conversation_only') return <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium text-amber-400">Conversation Only</span>;
    return <span className="rounded-full bg-secondary px-2 py-0.5 text-[10px] font-medium text-muted-foreground">{v || 'Not set'}</span>;
  }

  function badgeForRiskTier(v) {
    if (v === 'high') return <span className="inline-flex items-center gap-1"><ShieldAlert className="h-3 w-3 text-red-500" /><span className="text-red-400">High</span></span>;
    if (v === 'medium') return <span className="inline-flex items-center gap-1"><Shield className="h-3 w-3 text-amber-500" /><span className="text-amber-400">Medium</span></span>;
    if (v === 'low') return <span className="inline-flex items-center gap-1"><ShieldCheck className="h-3 w-3 text-green-500" /><span className="text-green-400">Low</span></span>;
    return <span className="text-muted-foreground">{v || 'Not set'}</span>;
  }

  return (
    <div className="rounded-lg border border-border bg-secondary/20 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Icon className={`h-3.5 w-3.5 shrink-0 ${meta.color}`} />
          <span className="text-xs font-medium text-foreground">{meta.label}</span>
        </div>
        {!readOnly && !editing && (
          <button onClick={handleEdit} className="shrink-0 rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors">
            <Pencil className="h-3 w-3" />
          </button>
        )}
        {editing && (
          <div className="flex items-center gap-1 shrink-0">
            <button onClick={handleSave} className="rounded p-1 text-green-500 hover:bg-green-500/10 transition-colors"><Check className="h-3 w-3" /></button>
            <button onClick={handleCancel} className="rounded p-1 text-muted-foreground hover:bg-secondary transition-colors"><X className="h-3 w-3" /></button>
          </div>
        )}
      </div>

      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className="mt-2 w-full rounded border border-border bg-background px-3 py-2 font-mono text-[11px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/40"
          rows={6}
          spellCheck={false}
        />
      ) : isEmpty ? (
        <div className="mt-1.5 text-[11px] italic text-muted-foreground">Not configured</div>
      ) : fieldKey === 'manifest_json' && typeof value === 'object' ? (
        <div className="mt-2 space-y-1.5">
          {value.mission && <div className="text-[11px] text-foreground line-clamp-2">{value.mission}</div>}
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            {value.agent_name && <span className="font-medium text-foreground/70">{value.agent_name}</span>}
            {value.version && <span className="text-muted-foreground">v{value.version}</span>}
            {value.risk_tier && badgeForRiskTier(value.risk_tier)}
          </div>
          {value.task_scope && value.task_scope.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {(value.task_scope || []).map((s, i) => (
                <span key={i} className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] text-foreground">{s}</span>
              ))}
            </div>
          )}
        </div>
      ) : fieldKey === 'policy_profile' && typeof value === 'object' ? (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          {value.risk_tier && badgeForRiskTier(value.risk_tier)}
          {value.requires_confirmation !== undefined && (
            <span>{value.requires_confirmation ? 'Confirmation required' : 'Auto-execute'}</span>
          )}
          {value.max_concurrent_calls && <span>Max {value.max_concurrent_calls} concurrent</span>}
          {value.rate_limit_per_minute && <span>{value.rate_limit_per_minute}/min</span>}
        </div>
      ) : fieldKey === 'data_bindings' && Array.isArray(value) ? (
        <div className="mt-1.5">
          {value.length === 0 ? (
            <span className="text-[11px] text-muted-foreground">No data sources bound</span>
          ) : (
            <div className="flex flex-wrap gap-1">
              {value.map((b, i) => (
                <span key={i} className="rounded-full bg-blue-500/10 px-2 py-0.5 text-[10px] text-blue-400">
                  {b.knowledge_base_id || `Source ${i + 1}`}
                </span>
              ))}
            </div>
          )}
        </div>
      ) : fieldKey === 'skill_bindings' && Array.isArray(value) ? (
        <div className="mt-1.5">
          {value.length === 0 ? (
            <span className="text-[11px] text-muted-foreground">No skills bound</span>
          ) : (
            <div className="flex flex-wrap gap-1">
              {value.map((b, i) => (
                <span key={i} className="rounded-full bg-purple-500/10 px-2 py-0.5 text-[10px] text-purple-400">
                  {b.skill_name || `Skill ${i + 1}`}
                </span>
              ))}
            </div>
          )}
        </div>
      ) : fieldKey === 'memory_scope' ? (
        <div className="mt-1.5">{badgeForMemoryScope(value)}</div>
      ) : fieldKey === 'output_contract' && typeof value === 'object' ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          {value.allowed_artifact_types && (
            <span>{(value.allowed_artifact_types || []).join(', ')}</span>
          )}
          {value.must_include_sources && <span className="text-green-400">Sources required</span>}
          {value.max_response_length && <span>Max {value.max_response_length} chars</span>}
        </div>
      ) : fieldKey === 'evaluation_profile' && typeof value === 'object' ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          {value.trace_replay_enabled && <span className="text-green-400">Trace replay on</span>}
          {value.expected_accuracy !== undefined && <span>Accuracy ≥ {Math.round(value.expected_accuracy * 100)}%</span>}
          {value.grounding_checks && value.grounding_checks.length > 0 && (
            <span>{(value.grounding_checks || []).join(', ')}</span>
          )}
        </div>
      ) : (
        <pre className="mt-1.5 overflow-x-auto font-mono text-[10px] text-muted-foreground whitespace-pre-wrap break-words max-h-20 overflow-y-auto">
          {typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
        </pre>
      )}
    </div>
  );
}

export default function HarnessAgentSections({ form, update }) {
  const [expanded, setExpanded] = useState(false);

  const fields = ['manifest_json', 'data_bindings', 'skill_bindings', 'memory_scope', 'policy_profile', 'output_contract', 'evaluation_profile'];

  const filledCount = fields.filter((k) => {
    const v = form[k];
    return v !== null && v !== undefined && v !== ''
      && !(typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 0)
      && !(Array.isArray(v) && v.length === 0);
  }).length;

  function handleFieldChange(key, value) {
    update({ [key]: value });
  }

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center gap-2 px-4 py-3 text-left transition-colors hover:bg-secondary/40"
      >
        <Shield className="h-4 w-4 text-primary" />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-foreground">Harness Agent Profile</div>
          <div className="text-[11px] text-muted-foreground">
            Layer 3 Enterprise Harness · {filledCount}/{fields.length} fields configured
          </div>
        </div>
        <div className="flex items-center gap-2">
          {filledCount === fields.length ? (
            <span className="rounded-full bg-green-500/10 px-2 py-0.5 text-[10px] font-medium text-green-400">Complete</span>
          ) : filledCount >= 4 ? (
            <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium text-amber-400">Partial</span>
          ) : (
            <span className="rounded-full bg-red-500/10 px-2 py-0.5 text-[10px] font-medium text-red-400">Empty</span>
          )}
          {expanded ? <ChevronDown className="h-4 w-4 text-muted-foreground" /> : <ChevronRight className="h-4 w-4 text-muted-foreground" />}
        </div>
      </button>
      {expanded && (
        <div className="border-t border-border px-4 py-3 space-y-3">
          <div className="grid grid-cols-1 gap-3">
            {fields.map((key) => (
              <JsonField
                key={key}
                fieldKey={key}
                value={form[key]}
                onChange={(v) => handleFieldChange(key, v)}
                readOnly={false}
              />
            ))}
          </div>
          <div className="flex items-center justify-end pt-1">
            <button
              onClick={() => {
                // Trigger re-derive: mark fields as empty so backend fills them
                const patch = {};
                fields.forEach((k) => { patch[k] = null; });
                update(patch);
              }}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-[11px] font-medium text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
              title="Clear harness fields so the backend re-derives them from the 5-layer prompt + skills on next save"
            >
              <RefreshCw className="h-3 w-3" />
              Re-derive from current config
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
