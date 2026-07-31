import { useState, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { Bot } from 'lucide-react';

function getOwnerId() {
  try {
    const user = JSON.parse(localStorage.getItem('base44_user') || 'null');
    if (user && user.id) return user.id;
  } catch { /* noop */ }
  const anon = localStorage.getItem('base44_anonymous_id');
  return anon || null;
}

export default function ExistingAgentsChips({ onError }) {
  const { t } = useLanguage();
  const [agents, setAgents] = useState([]);

  useEffect(() => {
    let cancelled = false;
    const owner = getOwnerId();
    const filter = owner ? { created_by_id: owner } : {};
    base44.entities.AgentApp.list(filter)
      .then((list) => {
        if (cancelled) return;
        setAgents((list || []).slice(0, 8));
      })
      .catch((e) => {
        if (cancelled) return;
        if (typeof onError === 'function') onError(e);
      });
    return () => { cancelled = true; };
  }, [onError]);

  if (!agents.length) return null;

  return (
    <div className="mb-4">
      <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {t.agentBuilder.existingAgents}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {agents.map((a) => (
          <button
            key={a.id}
            type="button"
            onClick={() => { window.location.href = '/agent-builder?edit=' + encodeURIComponent(a.id); }}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-1 text-[11px] text-foreground transition hover:border-primary/40 hover:bg-secondary/60"
          >
            <Bot className="h-3 w-3 shrink-0 text-primary" />
            <span className="max-w-[140px] truncate">{a.name}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
