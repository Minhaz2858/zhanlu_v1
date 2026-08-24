import { useState, useEffect, useRef, Fragment } from 'react';
import { useLocation } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { authFetch } from '@/api/authFetch';
import { useLanguage } from '@/lib/LanguageProvider';
import PageHeader from '@/components/PageHeader';
import { Loader2, Check, Globe, MessageSquare, ShieldCheck, Gauge, Cpu, Lock, ScrollText, Database, Activity, Wallet, Key, Server, User, Bot, Workflow, FileText, Network, Plus, Pencil, Trash2, X, Search, Eye, EyeOff, ChevronDown, CheckCircle2, XCircle } from 'lucide-react';
import WorkspaceDataSection from '@/components/settings/WorkspaceDataSection';

const MODELS = ['automatic', 'gpt_5_mini', 'gemini_3_flash', 'claude_sonnet_4_6', 'claude_opus_4_8'];

const SECTIONS_CONFIG = [
  { id: 'account', icon: User, type: 'account' },
  { id: 'i18n', icon: Globe, fields: [
    { type: 'text', key: 'timezone' },
    { type: 'select', key: 'date_format', options: ['YYYY-MM-DD', 'DD/MM/YYYY', 'MM/DD/YYYY'] },
  ]},
  { id: 'chat', icon: MessageSquare, fields: [
    { type: 'number', key: 'context_length' },
    { type: 'select', key: 'default_model', options: MODELS },
    { type: 'number', key: 'session_retention_days' },
    { type: 'toggle', key: 'file_upload_enabled' },
    { type: 'toggle', key: 'voice_interaction_enabled' },
    { type: 'text', key: 'voice_language' },
  ]},
  { id: 'auth', icon: ShieldCheck, fields: [
    { type: 'toggle', key: 'sso_enabled' },
    { type: 'toggle', key: 'mfa_required' },
    { type: 'select', key: 'rbac_mode', options: ['flat', 'hierarchical', 'abac'] },
    { type: 'number', key: 'session_timeout_minutes' },
    { type: 'number', key: 'password_min_length' },
  ]},
  { id: 'quota', icon: Gauge, fields: [
    { type: 'number', key: 'compute_quota' },
    { type: 'number', key: 'kb_storage_limit' },
    { type: 'number', key: 'agent_call_limit' },
    { type: 'number', key: 'task_count_limit' },
    { type: 'number', key: 'gpu_quota' },
    { type: 'number', key: 'api_rate_limit' },
  ]},
  { id: 'model', icon: Cpu, fields: [
    { type: 'select', key: 'fallback_model', options: MODELS },
    { type: 'number', key: 'max_tokens' },
    { type: 'number', key: 'temperature' },
  ]},
  { id: 'security', icon: Lock, fields: [
    { type: 'toggle', key: 'data_masking' },
    { type: 'toggle', key: 'pii_detection' },
    { type: 'toggle', key: 'encryption_enabled' },
    { type: 'text', key: 'third_party_whitelist' },
    { type: 'text', key: 'ip_whitelist' },
  ]},
  { id: 'compliance', icon: ScrollText, fields: [
    { type: 'toggle', key: 'audit_logging' },
    { type: 'number', key: 'audit_retention_days' },
    { type: 'select', key: 'compliance_mode', options: ['standard', 'strict', 'regulated'] },
    { type: 'text', key: 'data_residency' },
  ]},
  { id: 'data', icon: Database, fields: [
    { type: 'toggle', key: 'data_quality_check' },
    { type: 'toggle', key: 'lineage_tracking' },
    { type: 'text', key: 'data_retention_policy' },
  ]},
  { id: 'workspace_data', icon: Network, component: 'workspace_data' },
  { id: 'observe', icon: Activity, fields: [
    { type: 'toggle', key: 'monitoring_enabled' },
    { type: 'select', key: 'log_level', options: ['debug', 'info', 'warn', 'error'] },
    { type: 'toggle', key: 'tracing_enabled' },
    { type: 'toggle', key: 'alerting_enabled' },
  ]},
  { id: 'cost', icon: Wallet, fields: [
    { type: 'number', key: 'monthly_budget' },
    { type: 'number', key: 'cost_alert_threshold' },
    { type: 'toggle', key: 'usage_analytics' },
  ]},
  { id: 'api', icon: Key, fields: [
    { type: 'number', key: 'api_key_rotation_days' },
    { type: 'text', key: 'webhook_url' },
    { type: 'toggle', key: 'sdk_access_enabled' },
  ]},
  { id: 'infra', icon: Server, fields: [
    { type: 'text', key: 'cluster_region' },
    { type: 'toggle', key: 'auto_scaling' },
    { type: 'text', key: 'maintenance_window' },
    { type: 'text', key: 'backup_schedule' },
  ]},
];

function LlmModelDialog({ editing, initialForm, providerKey, onProviderChange, onClose, onSave, saving, saveError, cat }) {
  const [form, setForm] = useState(initialForm);
  const [showProviderPicker, setShowProviderPicker] = useState(false);
  const [search, setSearch] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [dialogTest, setDialogTest] = useState({ running: false, result: null });
  const pickerRef = useRef(null);

  useEffect(() => { setForm(initialForm); }, [initialForm]);

  // Probe the endpoint with the CURRENT (unsaved) form values so admins can
  // validate a fresh base_url / api_key before saving.
  async function runDialogTest() {
    setDialogTest({ running: true, result: null });
    try {
      const res = await authFetch('/api/llm/models/test-connection', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: form.name || null,
          base_url: form.base_url,
          api_key: form.api_key || null,
          model_id: form.model_id,
          provider: form.provider || null,
        }),
      });
      let result;
      try { result = await res.json(); } catch { result = null; }
      if (!res.ok) result = { ok: false, error: result?.detail || `HTTP ${res.status}` };
      setDialogTest({ running: false, result });
    } catch (err) {
      setDialogTest({ running: false, result: { ok: false, error: err?.message || 'Network error' } });
    }
  }
  useEffect(() => {
    function onClick(e) { if (pickerRef.current && !pickerRef.current.contains(e.target)) setShowProviderPicker(false); }
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, []);

  const preset = PROVIDER_PRESETS[providerKey];
  const isCustom = providerKey === 'custom';
  const filteredProviders = Object.entries(PROVIDER_PRESETS).filter(([_, p]) =>
    p.label.toLowerCase().includes(search.toLowerCase())
  );

  const canSave = form.model_id && (!isCustom || form.base_url);
  const canTest = form.model_id && form.base_url;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-2xl border border-border bg-card p-6 shadow-2xl">
        <div className="mb-5 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <h3 className="font-display text-lg text-foreground">{editing ? cat.editModel : cat.addModel}</h3>
            <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-300">
              OpenAI-compatible API required
            </span>
          </div>
          <button onClick={onClose} className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"><X className="h-4 w-4" /></button>
        </div>

        <div className="space-y-4">
          {/* Provider Picker */}
          <div>
            <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-muted-foreground">{cat.provider}</label>
            <div ref={pickerRef} className="relative">
              <button
                onClick={() => setShowProviderPicker(!showProviderPicker)}
                className="flex w-full items-center gap-2 rounded-lg border border-border bg-background px-3 py-2.5 text-left text-sm transition-colors hover:border-primary/40 focus:outline-none focus:ring-2 focus:ring-primary/20"
              >
                {preset ? (
                  <>
                    <span className="flex h-6 w-6 items-center justify-center rounded-md text-xs font-bold text-white" style={{ backgroundColor: preset.color }}>{preset.icon}</span>
                    <span className="flex-1">{preset.label}</span>
                  </>
                ) : (
                  <span className="text-muted-foreground">Select provider…</span>
                )}
                <ChevronDown className={`h-4 w-4 text-muted-foreground transition-transform ${showProviderPicker ? 'rotate-180' : ''}`} />
              </button>

              {showProviderPicker && (
                <div className="absolute z-20 mt-1 w-full rounded-xl border border-border bg-card shadow-lg">
                  <div className="border-b border-border p-2">
                    <div className="relative">
                      <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                      <input
                        autoFocus
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        placeholder="Search provider"
                        className="w-full rounded-lg border border-border bg-background py-1.5 pl-8 pr-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20"
                      />
                    </div>
                  </div>
                  <div className="max-h-64 overflow-auto p-1">
                    {filteredProviders.map(([key, p]) => (
                      <button
                        key={key}
                        onClick={() => { onProviderChange(key); setShowProviderPicker(false); setSearch(''); }}
                        className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition-colors ${providerKey === key ? 'bg-primary/10 text-primary' : 'hover:bg-muted'}`}
                      >
                        <span className="flex h-6 w-6 items-center justify-center rounded-md text-xs font-bold text-white" style={{ backgroundColor: p.color }}>{p.icon}</span>
                        <span className="flex-1">{p.label}</span>
                        {providerKey === key && <Check className="h-3.5 w-3.5" />}
                      </button>
                    ))}
                    {filteredProviders.length === 0 && (
                      <div className="px-3 py-4 text-center text-xs text-muted-foreground">No providers found</div>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Base URL — pre-filled from provider preset, always editable */}
          <div>
            <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-muted-foreground">Base URL</label>
            <input
              value={form.base_url}
              onChange={(e) => setForm({ ...form, base_url: e.target.value })}
              placeholder="https://api.example.com/v1"
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20"
            />
          </div>

          {/* API Key — optional; hidden when Private Deployment is checked */}
          {!form.is_private && (
          <div>
            <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-muted-foreground">API Key <span className="font-normal normal-case tracking-normal">(optional)</span></label>
            <div className="relative">
              <input
                type={showKey ? 'text' : 'password'}
                value={form.api_key}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                placeholder="Enter your API key"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 pr-10 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20"
              />
              <button
                type="button"
                onClick={() => setShowKey(!showKey)}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-muted-foreground hover:text-foreground"
              >
                {showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
            <p className="mt-1 text-[11px] text-muted-foreground">Optional. Leave empty for self-hosted / keyless endpoints (Ollama, vLLM, internal clusters). When provided, the key is encrypted at rest and never sent to the browser.</p>
          </div>
          )}

          {/* Model Name */}
          <div>
          <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-muted-foreground">Model Name</label>
          <input
            value={form.model_id}
            onChange={(e) => setForm({ ...form, model_id: e.target.value, name: e.target.value })}
            placeholder="Enter model ID"
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20"
          />
          </div>

          {/* Model Capabilities — per-model limits so users can add any model */}
          <div className="rounded-xl border border-border bg-muted/30 p-4">
            <p className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Model Capabilities</p>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="mb-1.5 block text-xs font-medium text-foreground">Max Output Tokens</label>
                <input
                  type="number"
                  min="1"
                  value={form.max_output_tokens ?? ''}
                  onChange={(e) => setForm({ ...form, max_output_tokens: e.target.value === '' ? null : Number(e.target.value) })}
                  placeholder="2048"
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20"
                />
              </div>
              <div>
                <label className="mb-1.5 block text-xs font-medium text-foreground">Context Window</label>
                <input
                  type="number"
                  min="1"
                  value={form.context_window ?? ''}
                  onChange={(e) => setForm({ ...form, context_window: e.target.value === '' ? null : Number(e.target.value) })}
                  placeholder="16384"
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20"
                />
              </div>
            </div>
            <p className="mt-2 text-[11px] text-muted-foreground">
              Leave empty to use the provider default / global cap. Set these for self-hosted models
              (e.g. vLLM) whose real context window is smaller than the model card claims.
            </p>
            <label className="mt-3 inline-flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={form.supports_structured_tool_calls}
                onChange={(e) => setForm({ ...form, supports_structured_tool_calls: e.target.checked })}
                className="h-4 w-4 rounded border-border text-primary focus:ring-primary"
              />
              <span className="text-sm text-muted-foreground">Supports Structured Tool Calls</span>
            </label>
            <p className="mt-1 text-[11px] text-muted-foreground">
              Uncheck for vLLM served WITHOUT <code>--enable-auto-tool-choice</code> — tool calls arrive
              as XML in the message text and are recovered automatically.
            </p>
          </div>

          {/* Toggles */}
          <div className="flex flex-wrap items-center gap-4 pt-1">
            <label className="inline-flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={form.is_private}
                onChange={(e) => setForm({ ...form, is_private: e.target.checked })}
                className="h-4 w-4 rounded border-border text-primary focus:ring-primary"
              />
              <span className="text-sm text-muted-foreground">Private Deployment</span>
            </label>
            <label className="inline-flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={form.is_default}
                onChange={(e) => setForm({ ...form, is_default: e.target.checked })}
                className="h-4 w-4 rounded border-border text-primary focus:ring-primary"
              />
              <span className="text-sm text-muted-foreground">Set as Default</span>
            </label>
            <label className="inline-flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
                className="h-4 w-4 rounded border-border text-primary focus:ring-primary"
              />
              <span className="text-sm text-muted-foreground">Enabled</span>
            </label>
          </div>
        </div>

        {saveError && (
          <div className="mt-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300">
            <strong className="font-medium">{cat.errorTitle || 'Save failed'}:</strong> {saveError}
          </div>
        )}

        {(dialogTest.running || dialogTest.result) && (
          <div className="mt-4">
            {dialogTest.running ? (
              <div className="flex items-center gap-2 rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />{cat.testing}
              </div>
            ) : (
              <TestResultPanel
                result={dialogTest.result}
                cat={cat}
                onDismiss={() => setDialogTest({ running: false, result: null })}
              />
            )}
          </div>
        )}

        <div className="mt-6 flex items-center justify-between gap-2">
          <button
            onClick={runDialogTest}
            disabled={dialogTest.running || !canTest}
            title={cat.test}
            className="inline-flex items-center gap-2 rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
          >
            <Activity className="h-4 w-4" />{cat.test}
          </button>
          <div className="flex gap-2">
            <button onClick={onClose} className="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground hover:bg-muted">{cat.cancel}</button>
            <button onClick={() => onSave(form)} disabled={saving || !canSave} className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90">
              {saving ? <><Loader2 className="h-4 w-4 animate-spin" />{cat.saving}</> : cat.save}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function TestResultPanel({ result, cat, onDismiss }) {
  const ok = result?.ok;
  return (
    <div className={`flex flex-wrap items-start gap-x-4 gap-y-1 rounded-lg border px-3 py-2 text-xs ${ok ? 'border-green-200 bg-green-50 text-green-800 dark:border-green-800 dark:bg-green-900/20 dark:text-green-300' : 'border-red-200 bg-red-50 text-red-800 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300'}`}>
      {ok ? <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 font-medium">
          <span>{ok ? cat.testOk : cat.testFailed}</span>
          {result?.status_code != null && <span className="opacity-70">HTTP {result.status_code}</span>}
          {result?.latency_ms != null && <span className="opacity-70">{cat.testLatency} {result.latency_ms}ms</span>}
        </div>
        {result?.response_text ? (
          <p className="font-mono break-all opacity-90">{cat.testResponse}: {result.response_text}</p>
        ) : result?.error ? (
          <p className="break-all opacity-90">{cat.testError}: {result.error}</p>
        ) : null}
      </div>
      {onDismiss && (
        <button onClick={onDismiss} className="rounded p-0.5 opacity-70 hover:opacity-100"><X className="h-3.5 w-3.5" /></button>
      )}
    </div>
  );
}

export default function Settings() {
  const { t } = useLanguage();
  const [settings, setSettings] = useState(null);
  const [active, setActive] = useState('account');
  const [user, setUser] = useState(null);
  const [stats, setStats] = useState({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const location = useLocation();

  useEffect(() => { load(); }, []);

  // Honor the deep-link hash (e.g. /settings#cost) so a failed-run
  // recovery CTA can land the user on the right section.
  useEffect(() => {
    if (!location) return;
    const hash = location.hash ? location.hash.replace('#', '') : '';
    if (hash && SECTIONS_CONFIG.some((s) => s.id === hash)) {
      setActive(hash);
    }
  }, [location]);
  async function load() {
    try {
      let u = null;
      try { u = await base44.auth.me(); } catch { /* public app */ }
      setUser(u);
      const [list, sessions, agents, tasks, files] = await Promise.all([
        base44.entities.UserSetting.list('', 1),
        base44.entities.ChatSession.list('', 1).then((r) => r.length).catch(() => 0),
        base44.entities.AgentApp.list('', 1).then((r) => r.length).catch(() => 0),
        base44.entities.AutomationTask.list('', 1).then((r) => r.length).catch(() => 0),
        base44.entities.UserFile.list('', 1).then((r) => r.length).catch(() => 0),
      ]);
      if (list.length > 0) setSettings(list[0]);
      else setSettings(await base44.entities.UserSetting.create({}));
      setStats({ sessions, agents, tasks, files });
    } finally { setLoading(false); }
  }

  function update(field, value) { setSettings((prev) => ({ ...prev, [field]: value })); }
  async function save() {
    setSaving(true);
    try {
      const { id, created_date, updated_date, created_by_id, ...payload } = settings;
      await base44.entities.UserSetting.update(id, payload);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally { setSaving(false); }
  }

  if (loading) return <div className="flex h-full items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>;
  if (!settings) return null;

  const current = SECTIONS_CONFIG.find((s) => s.id === active);
  const label = (k) => t.settings.fieldLabels[k] || k;
  const optLabel = (v) => t.settings.options[v] || v;

  return (
    <div className="flex h-full">
      <div className="w-52 shrink-0 overflow-y-auto border-r border-border bg-sidebar/40 px-3 py-6">
        <div className="mb-4 px-3">
          <h2 className="font-display text-sm tracking-tight text-foreground">{t.settings.center}</h2>
          <p className="mt-0.5 text-[11px] text-muted-foreground">{SECTIONS_CONFIG.length} {t.settings.sectionsCount}</p>
        </div>
        <nav className="space-y-0.5">
          {SECTIONS_CONFIG.map((s) => (
            <button key={s.id} onClick={() => setActive(s.id)} className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors ${active === s.id ? 'bg-sidebar-accent text-accent-foreground font-medium' : 'text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground'}`}>
              <s.icon className="h-4 w-4" />
              {t.settings.sectionLabels[s.id]}
            </button>
          ))}
        </nav>
      </div>

      <div className="flex flex-1 flex-col">
        <div className="flex-1 overflow-y-auto px-8 py-8">
          <PageHeader title={t.settings.sectionLabels[current.id]} subtitle={t.settings.sectionDescs[current.id]} />
          <div className="max-w-2xl">
            {current.type === 'account' ? (
              <AccountSection user={user} stats={stats} />
            ) : current.component === 'workspace_data' ? (
              <WorkspaceDataSection />
            ) : (
              <>
                {current.fields && current.fields.length > 0 && (
                  <div className="rounded-xl border border-border bg-card p-6 mb-6">
                    <div className="space-y-4">
                      {current.fields.map((f) => (
                        <FieldRenderer key={f.key} field={f} settings={settings} update={update} label={label} optLabel={optLabel} />
                      ))}
                    </div>
                  </div>
                )}
                {current.id === 'model' && <LlmModelsSection user={user} />}
              </>
            )}
          </div>
        </div>

        <div className="border-t border-border bg-card/80 px-8 py-3 backdrop-blur">
          <div className="flex max-w-2xl items-center justify-end">
            <button onClick={save} disabled={saving} className="inline-flex items-center gap-2 rounded-lg bg-primary px-5 py-2.5 text-sm font-medium text-primary-foreground disabled:opacity-50">
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : saved ? <Check className="h-4 w-4" /> : null}
              {saving ? t.common.saving : saved ? t.common.saved : t.common.save}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function AccountSection({ user, stats }) {
  const { t } = useLanguage();
  const name = user?.full_name || user?.email?.split('@')[0] || t.common.guest;
  const initial = name.charAt(0).toUpperCase();
  const statCards = [
    { label: t.settings.account.sessions, value: stats.sessions ?? 0, icon: Workflow },
    { label: t.settings.account.agents, value: stats.agents ?? 0, icon: Bot },
    { label: t.settings.account.tasks, value: stats.tasks ?? 0, icon: Database },
    { label: t.settings.account.files, value: stats.files ?? 0, icon: FileText },
  ];
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-5 rounded-xl border border-border bg-card p-6">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-primary text-2xl font-medium text-primary-foreground">{initial}</div>
        <div>
          <h2 className="font-display text-2xl text-foreground">{name}</h2>
          <p className="text-sm text-muted-foreground">{user?.email || t.common.notLoggedIn}</p>
          <p className="mt-1 text-xs text-muted-foreground">{t.settings.account.role}：{user?.role === 'admin' ? t.common.admin : t.common.user}</p>
          {Array.isArray(user?.role_descriptions) && user.role_descriptions.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {user.role_descriptions.map((r, i) => (
                <span key={`${r}-${i}`} className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-xs font-medium text-secondary-foreground">
                  {r}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
      <div>
        <h3 className="mb-3 font-display text-lg text-foreground">{t.settings.account.resourceUsage}</h3>
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          {statCards.map((s) => (
            <div key={s.label} className="rounded-xl border border-border bg-card p-5">
              <s.icon className="mb-3 h-5 w-5 text-primary" />
              <div className="font-display text-2xl text-foreground">{s.value}</div>
              <div className="text-xs text-muted-foreground">{s.label}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function FieldRenderer({ field, settings, update, label, optLabel }) {
  if (field.type === 'select') {
    return (
      <Row label={label(field.key)}>
        <select value={settings[field.key] ?? ''} onChange={(e) => update(field.key, e.target.value)} className="w-56 rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30">
          {field.options.map((o) => <option key={o} value={o}>{optLabel(o)}</option>)}
        </select>
      </Row>
    );
  }
  if (field.type === 'text') {
    return (
      <Row label={label(field.key)}>
        <input value={settings[field.key] ?? ''} onChange={(e) => update(field.key, e.target.value)} className="w-56 rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30" />
      </Row>
    );
  }
  if (field.type === 'number') {
    return (
      <Row label={label(field.key)}>
        <input type="number" value={settings[field.key] ?? 0} onChange={(e) => update(field.key, Number(e.target.value))} className="w-40 rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30" />
      </Row>
    );
  }
  return (
    <Row label={label(field.key)}>
      <button onClick={() => update(field.key, !settings[field.key])} className={`relative h-6 w-11 rounded-full transition-colors ${settings[field.key] ? 'bg-primary' : 'bg-border'}`}>
        <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${settings[field.key] ? 'left-[22px]' : 'left-0.5'}`} />
      </button>
    </Row>
  );
}

function Row({ label, children }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <label className="text-sm text-foreground">{label}</label>
      {children}
    </div>
  );
}

// ── LLM Model Catalog Section (admin only) ──────────────────────────────

// Preset providers — selecting one auto-fills base_url. The Model Name is
// always a free-text input (see LlmModelDialog), so users type any model ID.
const PROVIDER_PRESETS = {
  deepseek: {
    label: 'DeepSeek',
    baseUrl: 'https://api.deepseek.com/v1',
    isPrivate: false,
    color: '#4F6EF7',
    icon: '🐋',
  },
  openai: {
    label: 'OpenAI',
    baseUrl: 'https://api.openai.com/v1',
    isPrivate: false,
    color: '#10A37F',
    icon: '◉',
  },
  anthropic: {
    label: 'Anthropic',
    baseUrl: 'https://api.anthropic.com/v1',
    isPrivate: false,
    color: '#D4A574',
    icon: '◈',
  },
  google: {
    label: 'Google Gemini',
    baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai',
    isPrivate: false,
    color: '#4285F4',
    icon: '◇',
  },
  moonshot: {
    label: 'Moonshot Kimi',
    baseUrl: 'https://api.moonshot.cn/v1',
    isPrivate: false,
    color: '#0066FF',
    icon: 'K',
  },
  qwen: {
    label: 'Qwen',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    isPrivate: false,
    color: '#7B1FA2',
    icon: 'Q',
  },
  zhipu: {
    label: 'Zhipu GLM',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    isPrivate: false,
    color: '#1A73E8',
    icon: 'Z',
  },
  ollama: {
    label: 'Ollama',
    baseUrl: 'http://localhost:11434/v1',
    isPrivate: true,
    color: '#FF6B35',
    icon: '🦙',
  },
  custom: {
    label: 'Custom',
    baseUrl: '',
    isPrivate: true,
    color: '#6B7280',
    icon: '+',
  },
};

function LlmModelsSection({ user }) {
  const { t } = useLanguage();
  const [models, setModels] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showDialog, setShowDialog] = useState(false);
  const [editing, setEditing] = useState(null);
  const [providerKey, setProviderKey] = useState('custom'); // currently selected preset
  const [form, setForm] = useState({ name: '', model_id: '', provider: '', base_url: '', api_key: '', is_private: false, is_default: false, enabled: true });
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(null);
  const [featureEnabled, setFeatureEnabled] = useState(true);
  const [saveError, setSaveError] = useState('');
  const [testState, setTestState] = useState({}); // { [modelId]: { running, result } }

  const isAdmin = user?.role === 'admin';

  useEffect(() => { loadModels(); }, []);

  // When the user picks a provider preset, auto-fill base_url / provider
  // / is_private so they only have to enter the model id + api key.
  function applyProviderPreset(key) {
    const preset = PROVIDER_PRESETS[key];
    if (!preset) return;
    setProviderKey(key);
    setForm((prev) => ({
      ...prev,
      provider: preset.label,
      base_url: preset.baseUrl,
      model_id: '',
      name: '',
      is_private: preset.isPrivate ?? prev.is_private,
    }));
  }

  async function loadModels() {
    setLoading(true);
    // Always check feature status first — the catalog endpoint returns 403
    // when the flag is off, but we want a clear UI instead of an empty list.
    try {
      const statusRes = await authFetch('/api/llm/feature-status');
      if (statusRes.ok) {
        const status = await statusRes.json();
        setFeatureEnabled(status.enabled);
        if (!status.enabled) {
          setModels([]);
          setLoading(false);
          return;
        }
      }
    } catch { /* continue — flag check is best-effort */ }

    try {
      const res = await authFetch('/api/llm/models');
      if (res.ok) setModels(await res.json());
      else if (res.status === 403) {
        setFeatureEnabled(false);
        setModels([]);
      } else setModels([]);
    } catch {
      setModels([]);
    } finally { setLoading(false); }
  }

  function openAdd() {
    setEditing(null);
    setProviderKey('deepseek'); // default to a sensible preset
    setForm({ name: '', model_id: '', provider: 'DeepSeek', base_url: PROVIDER_PRESETS.deepseek.baseUrl, api_key: '', is_private: false, is_default: false, enabled: true, context_window: null, max_output_tokens: null, supports_structured_tool_calls: true });
    setSaveError('');
    setShowDialog(true);
  }

  function openEdit(m) {
    setEditing(m.id);
    // Try to match the model to a preset for the dropdown value
    const matchedKey = Object.entries(PROVIDER_PRESETS).find(([, p]) => p.label === m.provider || p.baseUrl === m.base_url)?.[0] || 'custom';
    setProviderKey(matchedKey);
    setForm({
      name: m.name, model_id: m.model_id, provider: m.provider,
      base_url: m.base_url, api_key: '', is_private: m.is_private,
      is_default: m.is_default, enabled: m.enabled,
      context_window: m.context_window ?? null, max_output_tokens: m.max_output_tokens ?? null,
      supports_structured_tool_calls: m.supports_structured_tool_calls ?? true,
    });
    setSaveError('');
    setShowDialog(true);
  }

  async function handleSave(dialogForm) {
    setSaving(true);
    setSaveError('');
    try {
      const payload = { ...dialogForm };
      if (!payload.api_key) delete payload.api_key;
      const url = editing ? `/api/llm/models/${editing}` : '/api/llm/models';
      const method = editing ? 'PUT' : 'POST';
      const res = await authFetch(url, {
        method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const errBody = await res.json();
          detail = errBody.detail || detail;
        } catch { /* body wasn't JSON */ }
        setSaveError(detail);
        return;  // keep dialog open so user can retry
      }
      setShowDialog(false);
      await loadModels();
    } catch (err) {
      setSaveError(err?.message || 'Network error');
    } finally { setSaving(false); }
  }

  async function handleDelete(id) {
    try {
      await authFetch(`/api/llm/models/${id}`, { method: 'DELETE' });
      setDeleting(null);
      await loadModels();
    } catch { /* handled */ }
  }

  // Probe a saved model entry — the backend decrypts the stored key.
  async function runTest(id) {
    setTestState((prev) => ({ ...prev, [id]: { running: true, result: null } }));
    try {
      const res = await authFetch(`/api/llm/models/${id}/test`, { method: 'POST' });
      let result;
      try { result = await res.json(); } catch { result = null; }
      if (!res.ok) result = { ok: false, error: result?.detail || `HTTP ${res.status}` };
      setTestState((prev) => ({ ...prev, [id]: { running: false, result } }));
    } catch (err) {
      setTestState((prev) => ({ ...prev, [id]: { running: false, result: { ok: false, error: err?.message || 'Network error' } } }));
    }
  }

  if (!isAdmin) {
    return <div className="rounded-xl border border-border bg-card p-6 text-center text-sm text-muted-foreground">{t.settings.or.adminsOnly}</div>;
  }

  if (loading) return <div className="flex h-32 items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>;

  // Feature-disabled state: clear message instead of misleading empty list
  if (!featureEnabled) {
    return (
      <div className="space-y-4">
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-6 dark:border-amber-800 dark:bg-amber-900/20">
          <div className="flex items-start gap-3">
            <Lock className="h-5 w-5 shrink-0 text-amber-600 dark:text-amber-400" />
            <div>
              <h3 className="font-medium text-amber-900 dark:text-amber-200">{t.settings.llmCatalog.disabledTitle || 'Feature disabled'}</h3>
              <p className="mt-1 text-sm text-amber-800 dark:text-amber-300">
                {t.settings.llmCatalog.disabledDesc || 'Hierarchical LLM configuration is currently disabled. Set HIERARCHICAL_LLM_ENABLED=true in your backend environment and restart the container to enable this feature.'}
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const cat = t.settings.llmCatalog;
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground max-w-xl">{cat.desc}</p>
        <button onClick={openAdd} className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"><Plus className="h-4 w-4" />{cat.addModel}</button>
      </div>

      {models.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border bg-card p-10 text-center text-sm text-muted-foreground">{cat.noModels}</div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-card">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-muted/40">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-foreground">{cat.name}</th>
                <th className="px-4 py-3 text-left font-medium text-foreground">{cat.modelId}</th>
                <th className="px-4 py-3 text-left font-medium text-foreground">{cat.provider}</th>
                <th className="px-4 py-3 text-left font-medium text-foreground">{cat.capabilities || 'Capabilities'}</th>
                <th className="px-4 py-3 text-center font-medium text-foreground w-16">{cat.enabled}</th>
                <th className="px-4 py-3 text-right font-medium text-foreground w-24"></th>
              </tr>
            </thead>
            <tbody>
              {models.map((m) => {
                const tst = testState[m.id];
                return (
                  <Fragment key={m.id}>
                    <tr className="border-b border-border last:border-0 hover:bg-muted/20 transition-colors">
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-foreground">{m.name}</span>
                          {m.is_private ? <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">{cat.isPrivate}</span> : null}
                          {m.is_default ? <span className="rounded-full bg-green-100 px-2 py-0.5 text-[10px] font-medium text-green-700 dark:bg-green-900/30 dark:text-green-400">Default</span> : null}
                        </div>
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-muted-foreground">{m.model_id}</td>
                      <td className="px-4 py-3 text-muted-foreground">{m.provider}</td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">
                        {m.context_window ? (
                          <span className="font-mono">{m.context_window.toLocaleString()} ctx</span>
                        ) : (
                          <span className="text-muted-foreground/60">default</span>
                        )}
                        <span className="ml-1 text-muted-foreground/60">
                          · {m.supports_structured_tool_calls ? 'tools' : 'xml-tools'}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-center">{m.enabled ? <div className="mx-auto h-2.5 w-2.5 rounded-full bg-green-500" /> : <div className="mx-auto h-2.5 w-2.5 rounded-full bg-border" />}</td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex items-center justify-end gap-1">
                          <button
                            onClick={() => runTest(m.id)}
                            disabled={tst?.running}
                            title={cat.test}
                            className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-primary disabled:opacity-50"
                          >
                            {tst?.running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Activity className="h-3.5 w-3.5" />}
                          </button>
                          <button onClick={() => openEdit(m)} className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"><Pencil className="h-3.5 w-3.5" /></button>
                          <button onClick={() => setDeleting(m.id)} className="rounded-md p-1.5 text-muted-foreground hover:bg-red-50 hover:text-red-600"><Trash2 className="h-3.5 w-3.5" /></button>
                        </div>
                      </td>
                    </tr>
                    {tst?.result && (
                      <tr className="border-b border-border last:border-0 bg-muted/10">
                        <td colSpan={5} className="px-4 py-2">
                          <TestResultPanel
                            result={tst.result}
                            cat={cat}
                            onDismiss={() => setTestState((prev) => ({ ...prev, [m.id]: null }))}
                          />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Add / Edit Dialog */}
      {showDialog && (
        <LlmModelDialog
          editing={editing}
          initialForm={form}
          providerKey={providerKey}
          onProviderChange={applyProviderPreset}
          onClose={() => setShowDialog(false)}
          onSave={handleSave}
          saving={saving}
          saveError={saveError}
          cat={cat}
        />
      )}

      {/* Delete Confirmation */}
      {deleting && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
          <div className="w-full max-w-sm rounded-2xl border border-border bg-card p-6 shadow-2xl">
            <p className="text-sm text-foreground">{cat.deleteConfirm.replace('{name}', models.find((m) => m.id === deleting)?.name || '')}</p>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={() => setDeleting(null)} className="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground hover:bg-muted">{cat.cancel}</button>
              <button onClick={() => handleDelete(deleting)} className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700">{cat.deleteModel}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, children }) {
  return (
    <div>
      <label className="mb-1 block text-xs font-medium text-muted-foreground">{label}</label>
      {children}
    </div>
  );
}
