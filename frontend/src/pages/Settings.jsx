import { useState, useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import PageHeader from '@/components/PageHeader';
import { Loader2, Check, Globe, MessageSquare, ShieldCheck, Gauge, Cpu, Lock, ScrollText, Database, Activity, Wallet, Key, Server, User, Bot, Workflow, FileText, Network } from 'lucide-react';
import WorkspaceDataSection from '@/components/settings/WorkspaceDataSection';

const MODELS = ['automatic', 'gpt_5_mini', 'gemini_3_flash', 'claude_sonnet_4_6', 'claude_opus_4_8'];

const SECTIONS_CONFIG = [
  { id: 'account', icon: User, type: 'account' },
  { id: 'i18n', icon: Globe, fields: [
    { type: 'select', key: 'language', options: ['zh', 'en'] },
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
              <div className="rounded-xl border border-border bg-card p-6">
                <div className="space-y-4">
                  {current.fields.map((f) => (
                    <FieldRenderer key={f.key} field={f} settings={settings} update={update} label={label} optLabel={optLabel} />
                  ))}
                </div>
              </div>
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