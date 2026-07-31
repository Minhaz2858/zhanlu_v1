import { useState, useEffect, useRef } from 'react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { getAnonymousId } from '@/lib/anonymousId';
import { filterUserAgents } from '@/lib/systemAgents';
import { Wrench, Bot, ChevronDown, Search, Check, X } from 'lucide-react';

export default function InvokePicker({ activeSkill, activeAgent, onSelectSkill, onSelectAgent, onRemoveSkill, onRemoveAgent }) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(null);
  const [skills, setSkills] = useState([]);
  const [agents, setAgents] = useState([]);
  const [q, setQ] = useState('');
  const wrapRef = useRef(null);

  useEffect(() => {
    Promise.all([
      base44.entities.Tool.list('-updated_date', 100),
      base44.auth.me().catch(() => null),
    ])
      .then(([tools, u]) => {
        // The picker shows the user's own skills (so they can slash-invoke
        // a skill they've explicitly added). "Owner" is the real user id
        // when logged in, otherwise the SDK's per-browser anonymous id —
        // the same value the backend stamps on rows created from this
        // browser, so the filter can actually find them. Falling back to
        // `tools` (everyone's skills) when even the anonymous id is
        // missing would be misleading (you'd see other people's picks),
        // so we show an empty list instead.
        const ownerId = u?.id || getAnonymousId();
        setSkills(ownerId ? tools.filter((x) => x.created_by_id === ownerId) : []);
      })
      .catch(() => {});
    base44.entities.AgentApp.list('-updated_date', 100).then(setAgents).catch(() => {});
  }, []);

  useEffect(() => {
    if (!open) return;
    function onDown(e) { if (wrapRef.current && !wrapRef.current.contains(e.target)) { setOpen(null); setQ(''); } }
    function onEsc(e) { if (e.key === 'Escape') { setOpen(null); setQ(''); } }
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onEsc);
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onEsc); };
  }, [open]);

  const ql = q.toLowerCase();
  const filteredSkills = skills.filter((s) => (s.name || '').toLowerCase().includes(ql) || (s.trigger || '').toLowerCase().includes(ql));
  // The agent picker shows the USER's own agents. System agents
  // (general_assistant, agent_builder, skill_agent, automation_agent,
  // power_user) are platform-shipped and managed silently by the
  // runtime — we hide them here so the user only ever sees and
  // picks their own agents. The auto-select path in Chat.jsx
  // still uses general_assistant internally for Ungrouped chats.
  const filteredAgents = filterUserAgents(agents).filter((a) => (a.name || '').toLowerCase().includes(ql));

  function skillToken(s) {
    if (s.trigger) {
      const parts = s.trigger.split('/').filter(Boolean);
      return parts.length ? parts[parts.length - 1] : s.trigger;
    }
    return s.name;
  }  return (
    <div className="flex items-center gap-1.5" ref={wrapRef}>
      <div className="relative">
        <button
          onClick={() => { setOpen(open === 'skill' ? null : 'skill'); setQ(''); }}
          className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs transition-colors ${activeSkill ? 'border-primary/40 bg-primary/5 text-primary' : 'border-border text-muted-foreground hover:text-foreground'}`}
        >
          <Wrench className="h-3 w-3" />
          <span className="max-w-[8rem] truncate">{activeSkill ? `/${skillToken(activeSkill)}` : t.chat.invokeSkill}</span>
          {activeSkill ? (
            <span
              role="button"
              tabIndex={-1}
              onClick={(e) => { e.stopPropagation(); onRemoveSkill?.(); }}
              className="ml-0.5 inline-flex items-center transition-colors hover:text-primary/70"
            >
              <X className="h-3 w-3" />
            </span>
          ) : (
            <ChevronDown className="h-3 w-3" />
          )}
        </button>
        {open === 'skill' && (
          <div className="absolute bottom-full left-0 mb-2 w-60 rounded-lg border border-border bg-popover shadow-lg z-50">
            <div className="border-b border-border p-2">
              <div className="flex items-center gap-1.5 rounded-md bg-secondary/60 px-2 py-1">
                <Search className="h-3 w-3 text-muted-foreground" />
                <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t.toolkit.searchPlaceholder} className="w-full bg-transparent text-xs focus:outline-none" autoFocus />
              </div>
            </div>
            <div className="max-h-52 overflow-y-auto p-1">
              {filteredSkills.length === 0 ? (
                <p className="px-2 py-3 text-center text-xs text-muted-foreground">{t.toolkit.empty}</p>
              ) : filteredSkills.map((s) => {
                const tok = skillToken(s);
                const isActive = activeSkill?.id === s.id;
                return (
                  <button key={s.id} onClick={() => { onSelectSkill(s); setOpen(null); setQ(''); }} className={`flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors hover:bg-secondary/70 ${isActive ? 'text-primary' : 'text-foreground'}`}>
                    <span className="truncate">{s.name}</span>
                    {isActive ? <Check className="h-3 w-3 shrink-0" /> : <span className="shrink-0 font-mono text-[10px] text-muted-foreground">/{tok}</span>}
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>

      <div className="relative">
        <button
          onClick={() => { setOpen(open === 'agent' ? null : 'agent'); setQ(''); }}
          className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs transition-colors ${activeAgent ? 'border-primary/40 bg-primary/10 text-primary' : 'border-border text-muted-foreground hover:text-foreground'}`}
        >
          <Bot className="h-3 w-3" />
          <span className="max-w-[10rem] truncate">{activeAgent ? activeAgent.name : t.chat.invokeAgent}</span>
          {activeAgent ? (
            <span
              role="button"
              tabIndex={-1}
              onClick={(e) => { e.stopPropagation(); onRemoveAgent?.(); }}
              className="ml-0.5 inline-flex items-center transition-colors hover:text-primary/70"
            >
              <X className="h-3 w-3" />
            </span>
          ) : (
            <ChevronDown className="h-3 w-3" />
          )}
        </button>
        {open === 'agent' && (
          <div className="absolute bottom-full left-0 mb-2 w-60 rounded-lg border border-border bg-popover shadow-lg z-50">
            <div className="border-b border-border p-2">
              <div className="flex items-center gap-1.5 rounded-md bg-secondary/60 px-2 py-1">
                <Search className="h-3 w-3 text-muted-foreground" />
                <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t.toolkit.searchPlaceholder} className="w-full bg-transparent text-xs focus:outline-none" autoFocus />
              </div>
            </div>
            <div className="max-h-52 overflow-y-auto p-1">
              {filteredAgents.length === 0 ? (
                <p className="px-2 py-3 text-center text-xs text-muted-foreground">{t.mySpace.empty}{t.mySpace.tabs.agent}</p>
              ) : filteredAgents.map((a) => {
                const isActive = activeAgent?.id === a.id;
                return (
                  <button key={a.id} onClick={() => { onSelectAgent(a); setOpen(null); setQ(''); }} className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors hover:bg-secondary/70 ${isActive ? 'text-primary' : 'text-foreground'}`}>
                    <Bot className="h-3 w-3 shrink-0 text-muted-foreground" />
                    <span className="truncate">{a.name}</span>
                    {isActive && <Check className="ml-auto h-3 w-3 shrink-0" />}
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}