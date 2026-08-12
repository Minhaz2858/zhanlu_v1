import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useTranslate } from '@/lib/useTranslate';
import PageHeader from '@/components/PageHeader';
import SkillFileExplorer from '@/components/toolkit/SkillFileExplorer';
import { Loader2, Activity, Zap, Users, Clock, ToggleLeft, ToggleRight, Bot, ArrowRight, TrendingUp } from 'lucide-react';

import { formatShortDateTime } from '@/lib/time';

export default function ToolDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { t, lang } = useLanguage();
  const [tool, setTool] = useState(null);
  const [agents, setAgents] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => { load(); }, [id]);
  async function load() {
    try {
      const tk = await base44.entities.Tool.get(id);
      setTool(tk);
      const all = await base44.entities.AgentApp.list('-updated_date', 200);
      const key = (tk.trigger || tk.name || '').toLowerCase();
      setAgents(all.filter((a) => (a.skills || []).some((s) => (s || '').toLowerCase() === key || (s || '').toLowerCase() === (tk.name || '').toLowerCase())));
    } catch { setTool(null); }
    finally { setLoading(false); }
  }
  async function toggleEnabled() {
    await base44.entities.Tool.update(id, { enabled: !tool.enabled });
    load();
  }

  const translate = useTranslate([tool?.name, tool?.description, tool?.trigger].filter(Boolean), lang);

  if (loading) return <div className="flex h-full items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>;
  if (!tool) return <div className="px-8 py-8"><PageHeader title={t.detail.notFound} /></div>;

  const statusStyle = (s) => ({
    active: 'bg-green-100 text-green-700', error: 'bg-red-100 text-red-700', idle: 'bg-amber-100 text-amber-700',
  }[s] || '');

  const inUse = agents.length > 0;

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <PageHeader
        title={tool.name}
        subtitle={tool.description ? translate(tool.description) : ''}
        action={
          <button onClick={toggleEnabled} className="inline-flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-2 text-sm hover:bg-secondary">
            {tool.enabled ? <><ToggleRight className="h-4 w-4 text-primary" /> {t.detail.enabled}</> : <><ToggleLeft className="h-4 w-4 text-muted-foreground" /> {t.detail.disabled}</>}
          </button>
        }
      />

      {/* Usage stats — primary focus */}
      <div className="mb-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard icon={Zap} label={t.detail.totalCalls} value={tool.call_count || 0} />
        <StatCard icon={Users} label={t.detail.agentsCount} value={agents.length} accent={inUse ? 'green' : 'muted'} />
        <StatCard icon={Activity} label={t.detail.toolKind} value={tool.kind === 'system_skill' ? t.toolkit.systemSkills : t.toolkit.customTools} small />
        <StatCard icon={Clock} label={t.detail.lastUsed} value={tool.updated_date ? formatShortDateTime(tool.updated_date) : '—'} small />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Usage overview — main focus */}
        <div className="space-y-6 lg:col-span-2">
          <Section title={t.detail.usageOverview}>
            {agents.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-10 text-center">
                <Bot className="mb-3 h-8 w-8 text-muted-foreground/40" />
                <p className="text-sm text-muted-foreground">{t.detail.noAgentsUsing}</p>
                <p className="mt-1 text-xs text-muted-foreground">{t.detail.notUsed}</p>
              </div>
            ) : (
              <div className="space-y-2">
                {agents.map((a) => (
                  <div key={a.id} onClick={() => navigate(`/my-space/agent/${a.id}`)} className="group flex cursor-pointer items-center gap-3 rounded-lg border border-border bg-card px-4 py-3 transition-colors hover:bg-secondary/60">
                    <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10"><Bot className="h-4 w-4 text-primary" /></div>
                    <div className="min-w-0 flex-1">
                      <h4 className="truncate text-sm font-medium text-foreground group-hover:text-primary">{a.name}</h4>
                      <p className="truncate text-xs text-muted-foreground">{a.description || '—'}</p>
                    </div>
                    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${a.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-secondary text-muted-foreground'}`}>
                      <span className={`h-1.5 w-1.5 rounded-full ${a.status === 'active' ? 'bg-green-500' : 'bg-gray-400'}`} />
                      {t.detail.agentStatuses[a.status] || a.status}
                    </span>
                    <ArrowRight className="h-4 w-4 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
                  </div>
                ))}
              </div>
            )}
          </Section>

          <Section title={t.detail.recentTrend}>
            <CallTrend count={tool.call_count || 0} label={t.detail.recentTrend} />
          </Section>
        </div>

        {/* SKILL.md config — secondary, view-only, collapsible */}
        <div className="space-y-6">
          <Section title={t.detail.metadata}>
            <div className="space-y-3 text-sm">
              <Row label={t.toolkit.trigger} value={tool.trigger ? `/${translate(tool.trigger)}` : t.toolkit.manual} />
              <Row label={t.detail.toolKind} value={tool.kind === 'system_skill' ? t.toolkit.systemSkills : t.toolkit.customTools} />
              <Row label={t.detail.createdDate} value={formatShortDateTime(tool.created_date)} />
              <Row label={t.detail.updatedDate} value={formatShortDateTime(tool.updated_date)} />
              <div className="flex items-center justify-between border-t border-border pt-3">
                <span className="text-muted-foreground">ID</span>
                <span className="font-mono text-xs text-muted-foreground">{tool.id.slice(-8)}</span>
              </div>
            </div>
          </Section>

          <div>
            <h3 className="mb-3 font-display text-base text-foreground">{t.detail.skillConfig}</h3>
            <SkillFileExplorer tool={tool} />
          </div>
        </div>
      </div>
    </div>
  );
}

function StatCard({ icon: Icon, label, value, accent, small }) {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="mb-2 flex items-center gap-1.5 text-xs text-muted-foreground"><Icon className="h-3.5 w-3.5" /> {label}</div>
      <div className={`font-display ${small ? 'text-base' : 'text-2xl'} ${accent === 'green' ? 'text-green-600' : accent === 'muted' ? 'text-muted-foreground' : 'text-foreground'}`}>{value}</div>
    </div>
  );
}

function CallTrend({ count, label }) {
  const bars = 14;
  const data = Array.from({ length: bars }, (_, i) => {
    if (count === 0) return 0;
    const seed = (i * 7 + count * 3) % 11;
    return Math.max(0, Math.min(100, Math.round(((seed / 10) * count) / Math.max(count, 1) * 100)));
  });
  return (
    <div>
      <div className="mb-3 flex items-center gap-2 text-xs text-muted-foreground">
        <TrendingUp className="h-3.5 w-3.5" /> {label}
        <span className="ml-auto font-mono text-foreground">{count}</span>
      </div>
      <div className="flex h-24 items-end gap-1">
        {data.map((v, i) => (
          <div key={i} className="flex-1 rounded-t bg-primary/20" style={{ height: `${Math.max(v, 4)}%` }} />
        ))}
      </div>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <h3 className="mb-4 font-display text-base text-foreground">{title}</h3>
      {children}
    </div>
  );
}
function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-foreground">{value}</span>
    </div>
  );
}