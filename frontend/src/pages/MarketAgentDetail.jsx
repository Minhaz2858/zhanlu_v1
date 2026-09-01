import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useTranslate } from '@/lib/useTranslate';
import PageHeader from '@/components/PageHeader';
import { Button } from '@/components/ui/button';
import { Star, Users, Loader2, ArrowRight, Tag, Lock, Copy, Check } from 'lucide-react';
import { toast } from '@/components/ui/use-toast';
import { generateAgentPrompts, recommendSkills } from '@/lib/generateAgentPrompts';
import { CAD_TOOL_CONFIG } from '@/lib/launchMarketAgent';

import { formatShortDateTime } from '@/lib/time';

export default function MarketAgentDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { t, lang } = useLanguage();
  const [agent, setAgent] = useState(null);
  const [related, setRelated] = useState([]);
  const [loading, setLoading] = useState(true);
  const [cloning, setCloning] = useState(false);
  const [cloned, setCloned] = useState(false);

  useEffect(() => { load(); }, [id]);
  async function load() {
    try {
      const a = await base44.entities.MarketAgent.get(id);
      setAgent(a);
      const all = await base44.entities.MarketAgent.list('-updated_date', 200);
      setRelated(all.filter((x) => x.category === a.category && x.id !== a.id).slice(0, 4));
    } catch { setAgent(null); }
    finally { setLoading(false); }
  }

  async function clone() {
    setCloning(true);
    try {
      let skills = [];
      try {
        const tools = await base44.entities.Tool.list();
        skills = recommendSkills(agent, tools);
      } catch { /* skills optional */ }
      const newAgent = await base44.entities.AgentApp.create({
        name: agent.name,
        description: agent.description,
        capabilities: agent.capabilities || [],
        model: 'automatic',
        status: 'active',
        data_read: true,
        agent_type: 'sequential',
        topology: 'standalone',
        skills,
        ...generateAgentPrompts(agent),
      });
      // The CAD Agent must always carry its Fusion toolset — the generic
      // clone flow does NOT copy tool_config, so a cloned CAD Agent would
      // silently lose its Fusion tools; restore them here. Idempotent.
      if (agent.name === 'CAD Agent') {
        try {
          await base44.entities.AgentApp.update(newAgent.id, { tool_config: { ...CAD_TOOL_CONFIG } });
        } catch { /* best-effort — the seeded copy already has it */ }
      }
      setCloned(true);
      toast({ title: t.agentConfig.cloned, description: agent.name });
      setTimeout(() => navigate(`/my-space/agent/${newAgent.id}`), 600);
    } catch (e) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' });
      setCloning(false);
    }
  }

  const translate = useTranslate(
    [agent?.name, agent?.description, ...((agent?.capabilities || []).filter(Boolean)), ...related.flatMap((r) => [r.name, r.description])].filter(Boolean),
    lang
  );

  if (loading) return <div className="flex h-full items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>;
  if (!agent) return <div className="px-8 py-8"><PageHeader title={t.detail.notFound} /></div>;

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <PageHeader
        title={translate(agent.name)}
        subtitle={t.market.categories[agent.category]}
        action={
          <Button onClick={clone} disabled={cloning || cloned} size="sm" className="gap-2">
            {cloning ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : cloned ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
            {cloning ? t.agentConfig.cloning : cloned ? t.agentConfig.cloned : t.agentConfig.clone}
            {!cloned && <ArrowRight className="h-3.5 w-3.5" />}
          </Button>
        }
      />

      <div className="mb-6 flex items-center gap-2 rounded-lg border border-border bg-secondary/40 px-4 py-2.5 text-xs text-muted-foreground">
        <Lock className="h-3.5 w-3.5 shrink-0 text-primary" />
        <span>{t.agentConfig.readOnlyHint}</span>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Section title={t.detail.basicInfo}>
            <div className="mb-4 flex flex-wrap items-center gap-4">
              <span className="inline-flex items-center gap-1 text-sm text-foreground"><Star className="h-4 w-4 fill-primary text-primary" /> {agent.rating}</span>
              <span className="inline-flex items-center gap-1 text-sm text-muted-foreground"><Users className="h-4 w-4" /> {agent.subscribers} {t.market.subscribers}</span>
              <span className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-xs text-muted-foreground"><Lock className="h-3 w-3" /> {t.agentConfig.readOnly}</span>
            </div>
            <p className="text-sm leading-relaxed text-foreground">{translate(agent.description)}</p>
          </Section>

          <Section title={t.detail.capabilities}>
            {agent.capabilities && agent.capabilities.length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {agent.capabilities.map((c, i) => (
                  <span key={i} className="inline-flex items-center gap-1 rounded-md bg-secondary px-2.5 py-1 text-xs text-foreground"><Tag className="h-3 w-3 text-muted-foreground" /> {translate(c)}</span>
                ))}
              </div>
            ) : <p className="text-sm text-muted-foreground">{t.detail.noCapabilities}</p>}
          </Section>
        </div>

        <div className="space-y-6">
          <Section title={t.detail.relatedAgents}>
            {related.length > 0 ? (
              <div className="space-y-2">
                {related.map((r) => (
                  <button key={r.id} onClick={() => navigate(`/market/${r.id}`)} className="flex w-full items-center gap-2 rounded-lg border border-border px-3 py-2.5 text-left transition-colors hover:bg-secondary">
                    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-secondary text-xs font-medium text-muted-foreground">{r.name.charAt(0)}</div>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm text-foreground">{translate(r.name)}</div>
                      <div className="truncate text-xs text-muted-foreground">{translate(r.description)}</div>
                    </div>
                    <ArrowRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  </button>
                ))}
              </div>
            ) : <p className="text-sm text-muted-foreground">{t.detail.noCapabilities}</p>}
          </Section>

          <Section title={t.detail.metadata}>
            <div className="space-y-3 text-sm">
              <Row label={t.detail.createdDate} value={formatShortDateTime(agent.created_date)} />
              <div className="flex items-center justify-between border-t border-border pt-3">
                <span className="text-muted-foreground">ID</span>
                <span className="font-mono text-xs text-muted-foreground">{agent.id.slice(-8)}</span>
              </div>
            </div>
          </Section>
        </div>
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