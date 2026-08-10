import { useState, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useTranslate } from '@/lib/useTranslate';
import { filterUserAgents } from '@/lib/systemAgents';
import { Loader2, Play, Folder, Bot, Building2 } from 'lucide-react';

/**
 * AgentsView — renders the Agents tab in MySpace.
 * Splits agents into COMPANY AGENTS (admin-assigned or company-type)
 * and MY AGENTS (personally created).
 */
export default function AgentsView({ scope = null }) {
  const { t, lang } = useLanguage();
  const navigate = useNavigate();
  const [agents, setAgents] = useState([]);
  const [loading, setLoading] = useState(true);

  // Collect all display texts for pre-translation (name + description).
  const agentTexts = useMemo(
    () => agents.flatMap((a) => [a.name, a.title, a.description, a.summary].filter(Boolean)),
    [agents]
  );
  const tr = useTranslate(agentTexts, lang);

  useEffect(() => { load(); }, []);

  async function load() {
    setLoading(true);
    try {
      // Use base44 SDK so appId + JWT are injected automatically.
      let data = await base44.entities.AgentApp.list('-updated_date', 200);
      if (!Array.isArray(data)) data = data.data || data.items || [];
      data = filterUserAgents(data);
      setAgents(data);
    } finally {
      setLoading(false);
    }
  }

  // Split: company (resource_type='company' OR shared with me) vs personal
  const companyAgents = agents.filter(
    (a) => a.resource_type === 'company' || a.is_shared_with_me
  );
  const myAgents = agents.filter(
    (a) => !(a.resource_type === 'company' || a.is_shared_with_me)
  );

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* ── COMPANY AGENTS ── */}
      {scope !== 'personal' && (
        <>
          <SectionHeader
            icon={Building2}
            title="Company Agents"
            badge="Admin configured"
            lang={lang}
          />
          {companyAgents.length === 0 ? (
            <EmptyState
              message={lang === 'en' ? 'No company agents assigned yet.' : '暂无公司分配的智能体。'}
              hint={lang === 'en' ? 'Your admin can assign shared agents for you to use.' : '管理员可以为您分配共享智能体。'}
              lang={lang}
            />
          ) : (
            <AgentGrid agents={companyAgents} tr={tr} t={t} navigate={navigate} />
          )}
        </>
      )}

      {/* ── MY AGENTS ── */}
      {scope !== 'company' && (
        <>
          <SectionHeader
            icon={Bot}
            title="My Agents"
            badge="Created by me"
            lang={lang}
          />
          {myAgents.length === 0 ? (
            <EmptyState
              message={t.mySpace.empty + (t.mySpace.tabs.agent || 'Agents')}
              hint={t.mySpace.emptyHint}
              lang={lang}
            />
          ) : (
            <AgentGrid agents={myAgents} tr={tr} t={t} navigate={navigate} />
          )}
        </>
      )}
    </div>
  );
}


function SectionHeader({ icon: Icon, title, badge, lang }) {
  return (
    <div className="flex items-center gap-2 border-b border-border pb-2">
      <Icon className="h-4 w-4 text-muted-foreground" />
      <h3 className="text-sm font-medium text-foreground">{title}</h3>
      <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
        {badge}
      </span>
    </div>
  );
}


function EmptyState({ message, hint, lang }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border py-12 text-center">
      <Bot className="mb-3 h-8 w-8 text-muted-foreground/40" />
      <p className="text-sm text-muted-foreground">{message}</p>
      <p className="mt-1 text-xs text-muted-foreground">{hint}</p>
    </div>
  );
}


function AgentGrid({ agents, tr, t, navigate }) {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
      {agents.map((it) => (
        <div
          key={it.id}
          onClick={() => navigate(`/my-space/agent/${it.id}`)}
          className="group flex cursor-pointer flex-col rounded-xl border border-border bg-card p-5 transition-shadow hover:shadow-sm"
        >
          <div className="mb-2 flex items-start gap-2">
            <Bot className="mt-0.5 h-4 w-4 text-primary" />
            <h3 className="min-w-0 flex-1 break-words font-display text-base text-foreground group-hover:text-primary">
              {tr(it.name || it.title)}
            </h3>
            {it.is_shared_with_me && (
              <span className="shrink-0 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
                Shared
              </span>
            )}
          </div>
          <p className="mb-3 flex-1 text-xs text-muted-foreground">
            {(it.description || it.summary) ? tr(it.description || it.summary) : '—'}
          </p>
          <div className="mt-3 flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
            <button
              onClick={() => navigate(`/?agent=${it.id}`)}
              className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary"
            >
              <Play className="h-3 w-3" /> {t.common.run}
            </button>
            <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
              <Folder className="h-3 w-3" />
              {it.project && it.project !== 'global' ? it.project : (t.automation?.ungrouped || 'Other')}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}
